"""Inbound message handler — replies to learner Canvas Conversations.

Two-stage:
  1. compose_reply(thread, last_msg)  — Claude decides reply / escalate / noreply
  2. process_inbox(canvas, session)   — polls unread, composes, dispatches, audits

Conservative by design: Claude can output [ESCALATE] or [NOREPLY] tokens to
opt out, and we run the same drift detection we use for nudges.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from anthropic import Anthropic

import re

from datetime import datetime, timedelta, timezone

from sgeg_nudge.canvas import CanvasClient
from sgeg_nudge.claude import MODEL, MAX_TOKENS, TEMPERATURE, compose_digest, detect_drift
from sgeg_nudge.config import settings
from sgeg_nudge.db import record_audit
from sgeg_nudge.tickets import (
    REASON_CONTENT,
    REASON_DISTRESS,
    REASON_DRIFT,
    open_ticket,
)
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPLY_PROMPT_PATH = PROJECT_ROOT / "docs" / "03-reply-prompt.md"

ESCALATE_TOKEN = "[ESCALATE]"
NOREPLY_TOKEN = "[NOREPLY]"
LIST_OUTSTANDING_TOKEN = "[LIST_OUTSTANDING]"
MAX_THREAD_MESSAGES = 10  # cap recent-history sent to Claude

# Window for inbound "what's due" lookups — matches the weekly digest window.
LIST_OUTSTANDING_PAST_DAYS = 14
LIST_OUTSTANDING_FUTURE_DAYS = 14

ReplyKind = Literal["reply", "escalate", "noreply", "list_outstanding"]


@dataclass(frozen=True)
class ReplyAction:
    kind: ReplyKind
    text: str  # reply body when kind == 'reply'; empty otherwise
    drift_reasons: tuple[str, ...]
    usage_input_tokens: int
    usage_output_tokens: int


_REPLY_SYSTEM_PROMPT: str | None = None


def _load_reply_prompt() -> str:
    global _REPLY_SYSTEM_PROMPT
    if _REPLY_SYSTEM_PROMPT is None:
        _REPLY_SYSTEM_PROMPT = REPLY_PROMPT_PATH.read_text(encoding="utf-8")
    return _REPLY_SYSTEM_PROMPT


# -------------------- thread shaping --------------------

def _format_thread_for_claude(
    thread: dict,
    sender_id: int,
    *,
    enrolled_subjects: list[str] | None = None,
) -> str:
    """Render the recent thread messages as Claude-readable history."""
    participants_by_id: dict[int, str] = {
        p["id"]: p.get("name", f"User {p['id']}")
        for p in thread.get("participants", [])
    }
    subject = thread.get("subject") or "(no subject)"

    # Canvas returns messages newest-first; reverse for natural reading order.
    messages = list(reversed(thread.get("messages", [])))[-MAX_THREAD_MESSAGES:]

    lines = [f"Subject: {subject}", ""]
    if enrolled_subjects is not None:
        lines.append("Learner's enrolled subjects:")
        if enrolled_subjects:
            for s in enrolled_subjects:
                lines.append(f"  - {s}")
        else:
            lines.append("  (none — learner has no active course enrolments)")
        lines.append("")
    for msg in messages:
        author = participants_by_id.get(msg.get("author_id"), f"User {msg.get('author_id')}")
        role = "SGEG Assistant (you)" if msg.get("author_id") == sender_id else author
        body = (msg.get("body") or "").strip()
        lines.append(f"[{role}]: {body}")
        lines.append("")
    lines.append("Now compose your single response according to the rules.")
    return "\n".join(lines)


def _last_inbound_message(thread: dict, sender_id: int) -> dict | None:
    """Return the most recent message NOT authored by SGEG Assistant, or None."""
    # Canvas returns newest first.
    for msg in thread.get("messages", []):
        if msg.get("author_id") != sender_id:
            return msg
    return None


def _latest_message_is_ours(thread: dict, sender_id: int) -> bool:
    messages = thread.get("messages", [])
    return bool(messages) and messages[0].get("author_id") == sender_id


# Distress signal — multi-word phrases keep false positives low.
_DISTRESS_RE = re.compile(
    r"\b("
    r"kill (myself|me)|want to die|hate (myself|my life)|hurt myself|"
    r"can't go on|give up on (life|everything|myself)|hopeless|worthless|"
    r"hits me|hit me|abused|abuse me|won't stop|"
    r"crisis|emergency|need help (now|urgent(ly)?)"
    r")\b",
    re.IGNORECASE,
)

# Afrikaans signal words — at least 2 needed to switch to af.
_AF_MARKERS = re.compile(
    r"\b(hallo|hoe|ek|jy|jou|julle|ons|hulle|is|en|maar|nie|kan|gaan|"
    r"werk|skool|asseblief|baie|dankie|opdrag|kursus|onderwerp|hulp)\b",
    re.IGNORECASE,
)


def _classify_reason(message: str, has_drift: bool) -> str:
    if has_drift:
        return REASON_DRIFT
    if _DISTRESS_RE.search(message or ""):
        return REASON_DISTRESS
    return REASON_CONTENT


def _detect_language(text: str) -> str:
    return "af" if len(_AF_MARKERS.findall(text or "")) >= 2 else "en"


def _thread_excerpt(thread: dict, sender_id: int, *, max_messages: int = 4) -> str:
    """A short readable excerpt of the last few messages for the ticket body."""
    participants_by_id: dict[int, str] = {
        p["id"]: p.get("name", f"User {p['id']}")
        for p in thread.get("participants", [])
    }
    msgs = list(reversed(thread.get("messages", [])))[-max_messages:]
    lines = []
    for m in msgs:
        author = participants_by_id.get(m.get("author_id"), f"User {m.get('author_id')}")
        role = "SGEG Assistant" if m.get("author_id") == sender_id else author
        body = (m.get("body") or "").strip().replace("\n", " ")
        lines.append(f"  [{role}]: {body}")
    return "\n".join(lines)


# -------------------- composing --------------------

def compose_reply(
    thread: dict,
    *,
    sender_id: int,
    enrolled_subjects: list[str] | None = None,
    client: Anthropic | None = None,
) -> ReplyAction:
    """Ask Claude to decide reply / escalate / noreply for the given thread.

    `enrolled_subjects` is the list of course names the learner is currently
    taking. Passed into the system prompt so Claude can escalate questions
    about subjects the learner isn't enrolled in.
    """
    if client is None:
        client = Anthropic(api_key=settings.anthropic_api_key)

    user_msg = _format_thread_for_claude(
        thread, sender_id=sender_id, enrolled_subjects=enrolled_subjects,
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=[
            {
                "type": "text",
                "text": _load_reply_prompt(),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    usage = resp.usage

    # Token-based decisions first.
    if LIST_OUTSTANDING_TOKEN in text:
        return ReplyAction(
            kind="list_outstanding",
            text="",
            drift_reasons=(),
            usage_input_tokens=usage.input_tokens,
            usage_output_tokens=usage.output_tokens,
        )
    if ESCALATE_TOKEN in text:
        return ReplyAction(
            kind="escalate",
            text="",
            drift_reasons=(),
            usage_input_tokens=usage.input_tokens,
            usage_output_tokens=usage.output_tokens,
        )
    if NOREPLY_TOKEN in text:
        return ReplyAction(
            kind="noreply",
            text="",
            drift_reasons=(),
            usage_input_tokens=usage.input_tokens,
            usage_output_tokens=usage.output_tokens,
        )

    # Otherwise a real reply — run the same drift checks we use for nudges.
    reasons = detect_drift(text)
    if reasons:
        log.warning("Reply drift detected: %s. Escalating instead of sending.", reasons)
        return ReplyAction(
            kind="escalate",
            text=text,  # preserved so the audit log can show what Claude tried to say
            drift_reasons=tuple(reasons),
            usage_input_tokens=usage.input_tokens,
            usage_output_tokens=usage.output_tokens,
        )

    return ReplyAction(
        kind="reply",
        text=text,
        drift_reasons=(),
        usage_input_tokens=usage.input_tokens,
        usage_output_tokens=usage.output_tokens,
    )


def _gather_outstanding_for_learner(
    canvas: CanvasClient,
    learner_id: int,
    *,
    past_days: int = LIST_OUTSTANDING_PAST_DAYS,
    future_days: int = LIST_OUTSTANDING_FUTURE_DAYS,
    now: datetime | None = None,
) -> list[dict]:
    """Pull outstanding assignments for one learner across their enrolled courses.

    Window: (now - past_days, now + future_days). Submissions checked per
    assignment; submitted items are filtered out. Returns the same dict
    shape compose_digest expects.
    """
    # Local imports so this module doesn't pull nudge.py at import time.
    from sgeg_nudge.nudge import _friendly_due_at, _is_submitted, _parse_iso8601

    now = now or datetime.now(timezone.utc)
    past_cutoff = now - timedelta(days=past_days)
    future_cutoff = now + timedelta(days=future_days)

    items: list[dict] = []
    try:
        enrolled_courses = canvas.list_courses_for_user(learner_id)
    except Exception as exc:
        log.warning("list_courses_for_user(%s) failed: %r", learner_id, exc)
        return items

    for course in enrolled_courses:
        course_id = course["id"]
        course_name = course.get("name", "")
        try:
            assignments = canvas.list_assignments(course_id)
        except Exception as exc:
            log.warning("list_assignments(%s) failed during list-outstanding: %r", course_id, exc)
            continue
        for assignment in assignments:
            due_at = _parse_iso8601(assignment.get("due_at"))
            if due_at is None or not (past_cutoff <= due_at <= future_cutoff):
                continue
            try:
                sub = canvas.get_submission(course_id, assignment["id"], learner_id)
            except Exception:
                continue
            if _is_submitted(sub):
                continue
            items.append({
                "course_name": course_name,
                "assignment_name": assignment.get("name", ""),
                "due_at_friendly": _friendly_due_at(due_at, "en"),
                "is_past_due": due_at < now,
            })
    return items


_CAUGHT_UP_EN = (
    "Hi {name}, great news — you've got nothing outstanding in the next two "
    "weeks. Nice work staying on top of things! If something turns up later, "
    "just reply here and I'll take a look."
)
_CAUGHT_UP_AF = (
    "Hi {name}, goeie nuus — daar is niks uitstaande vir die volgende twee "
    "weke nie. Mooi werk om op koers te bly! As iets later opduik, antwoord "
    "gerus hier en ek sal kyk."
)


def _send_outstanding_list(
    canvas: CanvasClient,
    session,
    *,
    conv_id: int,
    learner_id: int,
    learner_name: str,
    language: str,
    dry_run: bool,
) -> str:
    """Look up the learner's outstanding work and reply with the actual list."""
    items = _gather_outstanding_for_learner(canvas, learner_id)
    first_name = (learner_name or "there").split()[0]

    if not items:
        template = _CAUGHT_UP_AF if language == "af" else _CAUGHT_UP_EN
        body = template.format(name=first_name)
    else:
        composed = compose_digest(
            learner_first_name=first_name,
            outstanding_items=items,
            language=language,  # type: ignore[arg-type]
        )
        body = composed.text

    if dry_run:
        log.info("DRY RUN list_outstanding reply to conv %s: %r", conv_id, body)
        record_audit(
            session, "list_outstanding_dry_run",
            entity_type="conversation", entity_id=conv_id,
            detail=f"items={len(items)} body={body[:300]!r}",
        )
        return "dry_run"

    try:
        canvas.reply_to_conversation(conv_id, body)
        record_audit(
            session, "list_outstanding_replied",
            entity_type="conversation", entity_id=conv_id,
            detail=f"items={len(items)}",
        )
        return "sent"
    except Exception as exc:
        log.exception("list_outstanding send failed for conversation %s", conv_id)
        record_audit(
            session, "list_outstanding_send_failed",
            entity_type="conversation", entity_id=conv_id,
            detail=repr(exc),
        )
        return "failed"


# -------------------- inbox cycle --------------------

def process_inbox(
    canvas: CanvasClient,
    session: Session,
    *,
    dry_run: bool = False,
    limit: int = 50,
) -> dict[str, int]:
    """Process unread inbox once. Returns counts of each outcome."""
    counts = {"seen": 0, "replied": 0, "escalated": 0, "noreply": 0, "skipped": 0, "failed": 0}

    me = canvas.whoami()
    sender_id = me["id"]

    unread = canvas.list_conversations(scope="unread", limit=limit)
    if not unread:
        return counts

    anthropic_client = Anthropic(api_key=settings.anthropic_api_key)

    for summary in unread:
        conv_id = summary["id"]
        thread = canvas.get_conversation(conv_id)

        # If the latest message in the thread is from us, Canvas is showing
        # it as "unread" only because our outgoing message hasn't been opened
        # by the recipient yet — there's nothing here for us to read. Mark it
        # read on our side so we stop re-polling the same thread every cycle.
        if _latest_message_is_ours(thread, sender_id):
            counts["skipped"] += 1
            if not dry_run:
                try:
                    canvas.mark_conversation_read(conv_id)
                except Exception as exc:
                    log.warning("mark_read failed for own-latest %s: %r", conv_id, exc)
            continue

        last_msg = _last_inbound_message(thread, sender_id)
        if not last_msg:
            counts["skipped"] += 1
            continue

        counts["seen"] += 1
        record_audit(
            session,
            "inbox_message_seen",
            entity_type="conversation",
            entity_id=conv_id,
            detail=f"author={last_msg.get('author_id')} body={(last_msg.get('body') or '')[:200]!r}",
        )

        # Look up the learner's enrolled subjects so Claude can refuse to answer
        # about courses the learner isn't enrolled in.
        author_id = last_msg.get("author_id")
        enrolled_subjects: list[str] = []
        if author_id:
            try:
                enrolled = canvas.list_courses_for_user(author_id)
                enrolled_subjects = [c.get("name", "") for c in enrolled if c.get("name")]
            except Exception as exc:
                log.warning("list_courses_for_user(%s) failed: %r", author_id, exc)
                # On failure, fall through with empty list — strict prompt makes the bot
                # escalate any subject-specific questions when enrolment is empty.

        try:
            action = compose_reply(
                thread,
                sender_id=sender_id,
                enrolled_subjects=enrolled_subjects,
                client=anthropic_client,
            )
        except Exception as exc:
            log.exception("compose_reply failed for conversation %s", conv_id)
            record_audit(
                session,
                "reply_compose_failed",
                entity_type="conversation",
                entity_id=conv_id,
                detail=f"{exc!r}",
            )
            counts["failed"] += 1
            continue

        if action.kind == "list_outstanding":
            # Learner asked "what's due / outstanding / coming up" — look up
            # their actual assignments and reply with the real list, not a
            # navigation hint. Reuses the same compose_digest pipeline as the
            # proactive weekly digest.
            learner_name = next(
                (p.get("name") for p in thread.get("participants", [])
                 if p.get("id") == last_msg.get("author_id")),
                "Learner",
            ) or "Learner"
            language = _detect_language((last_msg.get("body") or ""))
            outcome = _send_outstanding_list(
                canvas, session,
                conv_id=conv_id,
                learner_id=int(last_msg.get("author_id") or 0),
                learner_name=learner_name,
                language=language,
                dry_run=dry_run,
            )
            if outcome == "sent" or outcome == "dry_run":
                counts["replied"] += 1
            else:
                counts["failed"] += 1
            if not dry_run:
                try:
                    canvas.mark_conversation_read(conv_id)
                except Exception as exc:
                    log.warning("mark_read after list_outstanding failed for %s: %r", conv_id, exc)
            continue

        if action.kind == "escalate":
            # New flow: file a ticket with the SGEG curriculum team on behalf
            # of the learner, send the learner a brief acknowledgement, then
            # mark the thread read on our side. Replaces the old leave-unread
            # path so learners aren't left wondering.
            is_drift = bool(action.drift_reasons)
            learner_name = next(
                (p.get("name") for p in thread.get("participants", [])
                 if p.get("id") == last_msg.get("author_id")),
                "Learner",
            ) or "Learner"
            raw_q = (last_msg.get("body") or "").strip()
            reason = _classify_reason(raw_q, is_drift)
            language = _detect_language(raw_q)
            context = _thread_excerpt(thread, sender_id)

            try:
                open_ticket(
                    canvas, session,
                    learner_id=int(last_msg.get("author_id") or 0),
                    learner_name=learner_name,
                    question=raw_q,
                    reason=reason,
                    conversation_id=str(conv_id),
                    context_snippet=context,
                    language=language,
                    dry_run=dry_run,
                )
                counts["escalated"] += 1
            except Exception as exc:
                log.exception("open_ticket failed for conv %s", conv_id)
                record_audit(
                    session, "ticket_open_failed",
                    entity_type="conversation", entity_id=conv_id,
                    detail=f"{exc!r} drift={action.drift_reasons}",
                )
                counts["failed"] += 1

            # Mark thread read regardless: we've handled it (ticket on file).
            if not dry_run:
                try:
                    canvas.mark_conversation_read(conv_id)
                except Exception as exc:
                    log.warning("mark_read after ticket open failed for %s: %r", conv_id, exc)

            # Also keep the legacy drift audit so /admin still sees the text.
            if is_drift:
                record_audit(
                    session, "reply_escalated_drift",
                    entity_type="conversation", entity_id=conv_id,
                    detail=f"drift={action.drift_reasons} attempted_text={action.text[:300]!r}",
                )
            continue

        if action.kind == "noreply":
            record_audit(
                session,
                "reply_skipped_noreply",
                entity_type="conversation",
                entity_id=conv_id,
            )
            # Mark read so we don't re-evaluate next cycle.
            if not dry_run:
                try:
                    canvas.mark_conversation_read(conv_id)
                except Exception as exc:
                    log.warning("mark_read failed for %s: %r", conv_id, exc)
            counts["noreply"] += 1
            continue

        # Real reply.
        if dry_run:
            log.info("DRY RUN reply to conv %s: %r", conv_id, action.text)
            record_audit(
                session,
                "reply_dry_run",
                entity_type="conversation",
                entity_id=conv_id,
                detail=action.text,
            )
            counts["replied"] += 1
            continue

        try:
            canvas.reply_to_conversation(conv_id, action.text)
            record_audit(
                session,
                "reply_sent",
                entity_type="conversation",
                entity_id=conv_id,
                detail=action.text,
            )
            counts["replied"] += 1
        except Exception as exc:
            log.exception("reply send failed for conversation %s", conv_id)
            record_audit(
                session,
                "reply_failed",
                entity_type="conversation",
                entity_id=conv_id,
                detail=f"{exc!r}",
            )
            counts["failed"] += 1

    session.commit()
    return counts
