"""Daily nudge orchestration — the engine that ties Canvas, Claude, and the DB together.

Three tiers:
  - 72h: assignments due in (24h, 72h] with no submission yet
  - 24h: assignments due in (0, 24h] with no submission yet
  - reinforce: learners who SUBMITTED an assignment we previously nudged about

For each candidate we:
  1. Check the DB dedup (skip if we already sent for this learner+assignment+tier)
  2. Safety re-check: pull the submission status one more time, skip if submitted
  3. Compose with Claude (or hold for review if drift detected)
  4. Send via Canvas Conversations
  5. Audit-log every decision
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from collections import defaultdict

from sgeg_nudge.canvas import CanvasClient
from sgeg_nudge.claude import compose_digest, compose_nudge
from sgeg_nudge.db import (
    Nudge,
    STATUS_FAILED,
    STATUS_REQUIRES_REVIEW,
    STATUS_SENT,
    STATUS_SKIPPED,
    TIER_24H,
    TIER_72H,
    TIER_MISSED,
    TIER_NEW,
    TIER_REINFORCE,
    TIER_WEEKLY_PREFIX,
    already_nudged,
    is_course_enabled,
    is_learner_enabled,
    record_audit,
)

log = logging.getLogger(__name__)
SAST = ZoneInfo("Africa/Johannesburg")

REINFORCE_LOOKBACK_DAYS = 14
MISSED_LOOKBACK_DAYS = 14    # how far back to look for past-due unsubmitted work
NEW_CONTENT_LOOKBACK_DAYS = 7  # an assignment is "new" if it was created in this window
DIGEST_PAST_DUE_DAYS = 14    # weekly digest covers items past-due this many days back
DIGEST_LOOKAHEAD_DAYS = 14   # ... and items due in this many days ahead

# Parent CC by grade band (spec default: Grade R-7 CC parent, 8-12 do not).
# Set to -1 to disable parent CC entirely.
PARENT_CC_MAX_GRADE = 7

# Matches "Grade R" or "Grade N" anywhere in a course name (case-insensitive).
_GRADE_FROM_COURSE_RE = re.compile(r"\bGrade\s+(R|\d{1,2})\b", re.IGNORECASE)


@dataclass(frozen=True)
class Candidate:
    learner_id: int
    learner_first_name: str
    learner_language: str
    course_id: int
    course_name: str
    assignment_id: int
    assignment_name: str
    due_at: datetime | None
    tier: str
    # Populated only for the weekly-digest tier; each item is a small dict
    # with course_name, assignment_name, due_at_friendly, and is_past_due.
    outstanding_items: tuple[dict, ...] = ()


# ----------------------- pure helpers (unit-tested) -----------------------

def _parse_iso8601(s: str | None) -> datetime | None:
    """Parse Canvas's ISO 8601 timestamps; returns None for empty/missing."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _first_name(full_name: str | None) -> str:
    if not full_name or not full_name.strip():
        return "there"
    return full_name.strip().split()[0]


def _language_from_user(user: dict) -> str:
    """Map a Canvas user record's locale to one of our two supported languages."""
    locale = (user.get("locale") or user.get("effective_locale") or "").lower()
    if locale.startswith("af"):
        return "af"
    return "en"


def _friendly_due_at(due_at: datetime | None, language: str) -> str:
    if due_at is None:
        return "soon"
    local = due_at.astimezone(SAST)
    if language == "af":
        weekdays_af = (
            "Maandag", "Dinsdag", "Woensdag", "Donderdag",
            "Vrydag", "Saterdag", "Sondag",
        )
        return f"{weekdays_af[local.weekday()]} om {local.strftime('%H:%M')}"
    # English (South African convention): "Friday at 5:00 PM"
    return local.strftime("%A at %I:%M %p").lstrip("0").replace(" 0", " ")


def _extract_grade(course_name: str | None) -> int | None:
    """Pull the year-level out of a SGEG course name. Grade R is treated as 0.

    Returns None if we can't find a grade marker — caller treats unknown as
    "no parent CC" (the safe / conservative default).
    """
    if not course_name:
        return None
    m = _GRADE_FROM_COURSE_RE.search(course_name)
    if not m:
        return None
    g = m.group(1).upper()
    if g == "R":
        return 0
    try:
        n = int(g)
    except ValueError:
        return None
    if 0 <= n <= 12:
        return n
    return None


def _should_cc_parent(grade: int | None) -> bool:
    """Spec default: Grade R-7 CC parent; 8-12 do not."""
    if grade is None or PARENT_CC_MAX_GRADE < 0:
        return False
    return grade <= PARENT_CC_MAX_GRADE


def _find_parents_for_learner(
    canvas: CanvasClient,
    course_id: int,
    learner_id: int,
) -> list[int]:
    """Return Canvas user ids of observers (parents) linked to this learner in this course."""
    try:
        observers = canvas.list_observer_enrollments(course_id)
    except Exception as exc:
        log.warning("list_observer_enrollments failed for course %s: %r", course_id, exc)
        return []
    parents: list[int] = []
    for obs in observers:
        if obs.get("associated_user_id") == learner_id and obs.get("user_id"):
            try:
                parents.append(int(obs["user_id"]))
            except (TypeError, ValueError):
                continue
    # Dedup while preserving order
    seen: set[int] = set()
    return [p for p in parents if not (p in seen or seen.add(p))]


def _is_submitted(submission: dict) -> bool:
    """True if this submission record indicates the learner has already submitted."""
    state = submission.get("workflow_state")
    if state in ("submitted", "graded", "pending_review"):
        return True
    if submission.get("submitted_at"):
        return True
    return False


# ----------------------- candidate finders -----------------------

def find_candidates_in_window(
    canvas: CanvasClient,
    *,
    tier: str,
    lower_hours: float,
    upper_hours: float,
    now: datetime | None = None,
) -> Iterable[Candidate]:
    """Yield candidates whose assignment due_at is in (lower_hours, upper_hours] from now."""
    now = now or datetime.now(timezone.utc)
    lower = now + timedelta(hours=lower_hours)
    upper = now + timedelta(hours=upper_hours)

    for course in canvas.list_courses():
        course_id = course["id"]
        course_name = course.get("name", "")
        for assignment in canvas.list_upcoming_assignments(course_id):
            due_at = _parse_iso8601(assignment.get("due_at"))
            if due_at is None or not (lower < due_at <= upper):
                continue

            for sub in canvas.list_submissions(course_id, assignment["id"]):
                if _is_submitted(sub):
                    continue
                user = sub.get("user") or {}
                user_id = user.get("id") or sub.get("user_id")
                if not user_id:
                    continue
                yield Candidate(
                    learner_id=int(user_id),
                    learner_first_name=_first_name(user.get("name")),
                    learner_language=_language_from_user(user),
                    course_id=course_id,
                    course_name=course_name,
                    assignment_id=assignment["id"],
                    assignment_name=assignment.get("name", ""),
                    due_at=due_at,
                    tier=tier,
                )


def _apply_config_filter(
    candidates: Iterable[Candidate],
    session: Session | None,
) -> Iterable[Candidate]:
    """If a session is provided, drop candidates whose course or learner is disabled."""
    if session is None:
        yield from candidates
        return
    for c in candidates:
        if not is_course_enabled(session, c.course_id):
            continue
        if not is_learner_enabled(session, c.learner_id):
            continue
        yield c


def find_72h_candidates(
    canvas: CanvasClient,
    session: Session | None = None,
    *,
    now: datetime | None = None,
) -> Iterable[Candidate]:
    return _apply_config_filter(
        find_candidates_in_window(canvas, tier=TIER_72H, lower_hours=24, upper_hours=72, now=now),
        session,
    )


def find_24h_candidates(
    canvas: CanvasClient,
    session: Session | None = None,
    *,
    now: datetime | None = None,
) -> Iterable[Candidate]:
    return _apply_config_filter(
        find_candidates_in_window(canvas, tier=TIER_24H, lower_hours=0, upper_hours=24, now=now),
        session,
    )


def find_new_content_candidates(
    canvas: CanvasClient,
    session: Session | None = None,
    *,
    now: datetime | None = None,
    lookback_days: int = NEW_CONTENT_LOOKBACK_DAYS,
) -> Iterable[Candidate]:
    """Yield candidates for assignments newly posted in a course.

    "New" = assignment.created_at is within the last `lookback_days` AND the
    due_at is still in the future (or unset). Past-due new assignments fall
    through to the missed tier rather than getting a "heads up new work" ping.
    One nudge per (learner, assignment, new) — dedup handles re-runs.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)

    def _new() -> Iterable[Candidate]:
        for course in canvas.list_courses():
            course_id = course["id"]
            course_name = course.get("name", "")
            for assignment in canvas.list_assignments(course_id):
                created_at = _parse_iso8601(assignment.get("created_at"))
                if created_at is None or created_at < cutoff:
                    continue
                due_at = _parse_iso8601(assignment.get("due_at"))
                # Skip if already past due — missed tier covers that case.
                if due_at is not None and due_at < now:
                    continue

                for sub in canvas.list_submissions(course_id, assignment["id"]):
                    if _is_submitted(sub):
                        continue
                    user = sub.get("user") or {}
                    user_id = user.get("id") or sub.get("user_id")
                    if not user_id:
                        continue
                    yield Candidate(
                        learner_id=int(user_id),
                        learner_first_name=_first_name(user.get("name")),
                        learner_language=_language_from_user(user),
                        course_id=course_id,
                        course_name=course_name,
                        assignment_id=assignment["id"],
                        assignment_name=assignment.get("name", ""),
                        due_at=due_at,
                        tier=TIER_NEW,
                    )

    return _apply_config_filter(_new(), session)


def find_missed_candidates(
    canvas: CanvasClient,
    session: Session | None = None,
    *,
    now: datetime | None = None,
    lookback_days: int = MISSED_LOOKBACK_DAYS,
) -> Iterable[Candidate]:
    """Yield candidates with a PAST-due assignment still not submitted.

    Window: (now - lookback_days, now]. One nudge per (learner, assignment, missed)
    via the dedup table, so a learner doesn't get pestered daily about the
    same missed item — one warm nudge and we move on.
    """
    now = now or datetime.now(timezone.utc)
    lower = now - timedelta(days=lookback_days)

    def _missed() -> Iterable[Candidate]:
        for course in canvas.list_courses():
            course_id = course["id"]
            course_name = course.get("name", "")
            for assignment in canvas.list_past_assignments(course_id):
                due_at = _parse_iso8601(assignment.get("due_at"))
                if due_at is None or not (lower < due_at <= now):
                    continue
                for sub in canvas.list_submissions(course_id, assignment["id"]):
                    if _is_submitted(sub):
                        continue
                    user = sub.get("user") or {}
                    user_id = user.get("id") or sub.get("user_id")
                    if not user_id:
                        continue
                    yield Candidate(
                        learner_id=int(user_id),
                        learner_first_name=_first_name(user.get("name")),
                        learner_language=_language_from_user(user),
                        course_id=course_id,
                        course_name=course_name,
                        assignment_id=assignment["id"],
                        assignment_name=assignment.get("name", ""),
                        due_at=due_at,
                        tier=TIER_MISSED,
                    )

    return _apply_config_filter(_missed(), session)


def find_weekly_digest_candidates(
    canvas: CanvasClient,
    session: Session | None = None,
    *,
    now: datetime | None = None,
) -> Iterable[Candidate]:
    """One consolidated nudge per learner per ISO week, listing outstanding work.

    Tier value is week-keyed (`weekly_YYYY-WNN`) so the existing dedup table
    fires exactly once per learner per week. Span: items past-due in the last
    DIGEST_PAST_DUE_DAYS plus items due within DIGEST_LOOKAHEAD_DAYS.
    """
    now = now or datetime.now(timezone.utc)
    past_cutoff = now - timedelta(days=DIGEST_PAST_DUE_DAYS)
    future_cutoff = now + timedelta(days=DIGEST_LOOKAHEAD_DAYS)
    week_key = now.strftime("%G-W%V")
    tier_value = f"{TIER_WEEKLY_PREFIX}{week_key}"

    outstanding_by_learner: dict[int, list[dict]] = defaultdict(list)
    learner_info: dict[int, dict] = {}

    for course in canvas.list_courses():
        course_id = course["id"]
        if session is not None and not is_course_enabled(session, course_id):
            continue
        course_name = course.get("name", "")

        for assignment in canvas.list_assignments(course_id):
            due_at = _parse_iso8601(assignment.get("due_at"))
            if due_at is None:
                continue
            if not (past_cutoff <= due_at <= future_cutoff):
                continue

            for sub in canvas.list_submissions(course_id, assignment["id"]):
                if _is_submitted(sub):
                    continue
                user = sub.get("user") or {}
                user_id = user.get("id") or sub.get("user_id")
                if not user_id:
                    continue
                lang = _language_from_user(user)
                outstanding_by_learner[int(user_id)].append({
                    "course_name": course_name,
                    "assignment_name": assignment.get("name", ""),
                    "due_at_friendly": _friendly_due_at(due_at, lang),
                    "is_past_due": due_at < now,
                })
                learner_info.setdefault(int(user_id), {
                    "name": user.get("name", ""),
                    "language": lang,
                })

    for learner_id, items in outstanding_by_learner.items():
        if session is not None and not is_learner_enabled(session, learner_id):
            continue
        info = learner_info.get(learner_id, {})
        yield Candidate(
            learner_id=learner_id,
            learner_first_name=_first_name(info.get("name")),
            learner_language=info.get("language", "en"),
            course_id=0,                       # digest spans multiple courses
            course_name="",
            assignment_id=0,                   # digest is per-learner-per-week, not per-assignment
            assignment_name="",
            due_at=None,
            tier=tier_value,
            outstanding_items=tuple(items),
        )


def find_reinforce_candidates(
    canvas: CanvasClient,
    session: Session,
    *,
    now: datetime | None = None,
) -> Iterable[Candidate]:
    """Find learners who have NOW submitted an assignment we previously nudged about."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=REINFORCE_LOOKBACK_DAYS)).replace(tzinfo=None)

    sent_nudges = session.scalars(
        select(Nudge).where(
            Nudge.status == STATUS_SENT,
            Nudge.tier.in_((TIER_72H, TIER_24H)),
            Nudge.composed_at >= since,
        )
    ).all()

    seen_pairs: set[tuple[int, int]] = set()
    for n in sent_nudges:
        key = (n.learner_id, n.assignment_id)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)

        if already_nudged(
            session,
            learner_id=n.learner_id,
            assignment_id=n.assignment_id,
            tier=TIER_REINFORCE,
        ):
            continue

        try:
            sub = canvas.get_submission(n.course_id, n.assignment_id, n.learner_id)
        except Exception as exc:
            log.warning("get_submission failed during reinforce scan: %r", exc)
            continue
        if not _is_submitted(sub):
            continue

        yield Candidate(
            learner_id=n.learner_id,
            learner_first_name=_first_name(n.learner_name),
            learner_language="en",
            course_id=n.course_id,
            course_name="",
            assignment_id=n.assignment_id,
            assignment_name="",
            due_at=None,
            tier=TIER_REINFORCE,
        )


# ----------------------- per-candidate processing -----------------------

def _subject_for(candidate: Candidate) -> str:
    if candidate.tier == TIER_REINFORCE:
        return "Thanks for getting that in!"
    if candidate.tier == TIER_24H:
        return f"Due tomorrow: {candidate.assignment_name}"
    if candidate.tier == TIER_MISSED:
        return f"Missed submission: {candidate.assignment_name}"
    if candidate.tier == TIER_NEW:
        return f"New: {candidate.assignment_name}"
    if candidate.tier.startswith(TIER_WEEKLY_PREFIX):
        return "Your week ahead"
    return f"Reminder: {candidate.assignment_name}"


def _record_skipped(session: Session, candidate: Candidate, *, reason: str) -> None:
    row = Nudge(
        learner_id=candidate.learner_id,
        learner_name=candidate.learner_first_name,
        course_id=candidate.course_id,
        assignment_id=candidate.assignment_id,
        tier=candidate.tier,
        status=STATUS_SKIPPED,
        message_text="",
    )
    session.add(row)
    session.flush()
    record_audit(
        session, "nudge_skipped",
        entity_type="nudge", entity_id=row.id, detail=reason,
    )


def process_candidate(
    canvas: CanvasClient,
    session: Session,
    candidate: Candidate,
    *,
    dry_run: bool = False,
) -> str:
    """Run the full pipeline for one candidate. Returns the outcome label."""
    if already_nudged(
        session,
        learner_id=candidate.learner_id,
        assignment_id=candidate.assignment_id,
        tier=candidate.tier,
    ):
        return "dedup_skipped"

    # Safety re-check (spec: never send about an assignment that was just submitted).
    if candidate.tier in (TIER_72H, TIER_24H, TIER_MISSED):
        try:
            sub = canvas.get_submission(
                candidate.course_id, candidate.assignment_id, candidate.learner_id,
            )
            if _is_submitted(sub):
                _record_skipped(session, candidate, reason="already submitted at send time")
                return "submitted_skipped"
        except Exception as exc:
            log.warning("safety re-check failed for learner=%s assignment=%s: %r",
                        candidate.learner_id, candidate.assignment_id, exc)
            # Continue — we prefer to attempt over silently skip on transient errors.

    if candidate.tier.startswith(TIER_WEEKLY_PREFIX):
        composed = compose_digest(
            learner_first_name=candidate.learner_first_name,
            outstanding_items=list(candidate.outstanding_items),
            language=candidate.learner_language,  # type: ignore[arg-type]
        )
    else:
        friendly_due = _friendly_due_at(candidate.due_at, candidate.learner_language)
        composed = compose_nudge(
            learner_first_name=candidate.learner_first_name,
            assignment_name=candidate.assignment_name or "your assignment",
            due_at_friendly=friendly_due,
            tier=candidate.tier,  # type: ignore[arg-type]
            language=candidate.learner_language,  # type: ignore[arg-type]
        )

    row = Nudge(
        learner_id=candidate.learner_id,
        learner_name=candidate.learner_first_name,
        course_id=candidate.course_id,
        assignment_id=candidate.assignment_id,
        tier=candidate.tier,
        status=composed.status,
        message_text=composed.text,
    )
    session.add(row)
    session.flush()

    if composed.status == STATUS_REQUIRES_REVIEW:
        record_audit(
            session, "nudge_held_for_review",
            entity_type="nudge", entity_id=row.id,
            detail=f"drift={composed.drift_reasons}",
        )
        return "held_for_review"

    if dry_run:
        record_audit(
            session, "nudge_dry_run",
            entity_type="nudge", entity_id=row.id, detail=composed.text,
        )
        return "dry_run"

    # Parent CC (spec default: Grade R-7 only). Skip for multi-course tiers.
    recipient_ids: list[int] = [candidate.learner_id]
    parent_ids: list[int] = []
    if not candidate.tier.startswith(TIER_WEEKLY_PREFIX):
        grade = _extract_grade(candidate.course_name)
        if _should_cc_parent(grade):
            parent_ids = _find_parents_for_learner(
                canvas, candidate.course_id, candidate.learner_id,
            )
            recipient_ids.extend(parent_ids)

    try:
        conv = canvas.send_conversation(
            recipient_ids=recipient_ids,
            body=composed.text,
            subject=_subject_for(candidate),
            group_conversation=bool(parent_ids),
        )
        row.canvas_conversation_id = str(conv.get("id"))
        row.status = STATUS_SENT
        row.sent_at = datetime.now(timezone.utc)
        record_audit(
            session, "nudge_sent",
            entity_type="nudge", entity_id=row.id,
            detail=(
                f"conv={row.canvas_conversation_id} "
                f"recipients={recipient_ids} parents_cc={parent_ids or 'none'}"
            ),
        )
        return "sent"
    except Exception as exc:
        row.status = STATUS_FAILED
        row.error_text = repr(exc)
        record_audit(
            session, "nudge_send_failed",
            entity_type="nudge", entity_id=row.id, detail=repr(exc),
        )
        log.exception("send_conversation failed for candidate=%s", candidate)
        return "failed"


# ----------------------- daily job -----------------------

def run_daily_job(
    canvas: CanvasClient,
    session: Session,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, int]:
    """Find candidates across all tiers and process each. Returns outcome counts."""
    record_audit(session, "daily_job_started", detail=f"dry_run={dry_run}")
    session.commit()

    counts: dict[str, int] = {}
    candidates_by_tier = (
        (TIER_NEW, list(find_new_content_candidates(canvas, session, now=now))),
        (TIER_72H, list(find_72h_candidates(canvas, session, now=now))),
        (TIER_24H, list(find_24h_candidates(canvas, session, now=now))),
        (TIER_MISSED, list(find_missed_candidates(canvas, session, now=now))),
        ("weekly", list(find_weekly_digest_candidates(canvas, session, now=now))),
        (TIER_REINFORCE, list(find_reinforce_candidates(canvas, session, now=now))),
    )

    for tier, candidates in candidates_by_tier:
        log.info("tier %s: %d candidate(s)", tier, len(candidates))
        for c in candidates:
            outcome = process_candidate(canvas, session, c, dry_run=dry_run)
            counts[outcome] = counts.get(outcome, 0) + 1
            session.commit()

    # Housekeeping: auto-close tickets that have been open too long without resolution.
    from sgeg_nudge.tickets import close_stale_tickets
    closed_stale = close_stale_tickets(session, now=now)
    if closed_stale:
        log.info("auto-closed %d stale ticket(s)", closed_stale)
        counts["stale_tickets_closed"] = closed_stale

    record_audit(session, "daily_job_completed", detail=str(counts))
    session.commit()
    return counts
