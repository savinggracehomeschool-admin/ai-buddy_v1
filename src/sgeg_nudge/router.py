"""Intent router — Phase 2.

Routes each student message to the cheapest path that can answer it correctly:

  Quick-action exact match  →  Canvas handler  (0 Claude tokens)
  Haiku micro-classifier    →  Canvas handler  (≈5 tokens classify + 0 compose)
                            →  Full Claude path (tool-use loop)

Data intents (grades, due_dates, module_content) are answered directly from
Canvas API responses rendered into fixed UI components.  No Claude composition
means no hallucination risk for factual data and sub-second responses.

The classifier uses a single Haiku call constrained to one token output.
Worst-case failure mode: routes to full Claude path (slow answer, never wrong).

Canvas responses are cached per (user_id, intent) with TTL:
  grades / assignments  →  5 min
  modules / courses     →  30 min
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)

# ── Intent labels ─────────────────────────────────────────────────────────────

Intent = Literal["grades", "due_dates", "module_content", "curriculum_redirect", "escalation", "other"]

# UI quick-action chips send these exact strings — skip classifier entirely
_EXACT_MATCH: dict[str, Intent] = {
    "What are my grades?":                         "grades",
    "What assignments do I have coming up?":       "due_dates",
    "Show me the course modules and lessons.":     "module_content",
}

# ── Cache ─────────────────────────────────────────────────────────────────────

_cache: dict[str, tuple[object, float]] = {}
_TTL: dict[str, int] = {
    "grades":         300,   # 5 min — grades change infrequently within a session
    "due_dates":      300,
    "module_content": 1800,  # 30 min — course structure is stable
}


def _cache_get(key: str) -> object | None:
    entry = _cache.get(key)
    if not entry:
        return None
    value, expires_at = entry
    if time.time() > expires_at:
        del _cache[key]
        return None
    return value


def _cache_set(key: str, value: object, intent: Intent) -> None:
    ttl = _TTL.get(intent, 300)
    _cache[key] = (value, time.time() + ttl)


def _cache_key(user_id: str, intent: Intent, course_id: str | None) -> str:
    return f"{user_id}:{intent}:{course_id or 'all'}"


# ── Classifier ────────────────────────────────────────────────────────────────

_CLASSIFY_PROMPT = """\
Classify this student message into exactly one of these labels:
grades              - asking about marks, scores, or academic performance
due_dates           - asking about upcoming tasks, assignments, what is due, homework
module_content      - asking where to find a lesson, video, worksheet, or course content
curriculum_redirect - asking for an explanation or definition of a curriculum topic (e.g. "explain photosynthesis", "what is a hypothesis", "how does X work", "define X")
escalation          - reporting a real problem or error: missing content that should be there, can't submit an assignment, Canvas error, something is broken, content is wrong or not loading
other               - general chat, greetings, or anything not in the above

Reply with ONLY the label, nothing else. One word.

Message: {message}"""


def classify_intent(message: str, anthropic_api_key: str) -> Intent:
    """Run Haiku micro-classifier. Returns 'other' on any failure."""
    from anthropic import Anthropic

    # Exact match first — free, instant
    exact = _EXACT_MATCH.get(message.strip())
    if exact:
        return exact  # intent label string, handled below after classification

    try:
        client = Anthropic(api_key=anthropic_api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            temperature=0.0,
            messages=[{
                "role": "user",
                "content": _CLASSIFY_PROMPT.format(message=message[:300]),
            }],
        )
        label = resp.content[0].text.strip().lower()
        valid: set[Intent] = {"grades", "due_dates", "module_content", "curriculum_redirect", "escalation", "other"}
        return label if label in valid else "other"  # type: ignore[return-value]
    except Exception as exc:
        log.warning("classify_intent failed (%s) — routing to full Claude path", exc)
        return "other"


# ── Canvas-direct response builders ──────────────────────────────────────────

@dataclass
class RouterResponse:
    """A complete response produced by the router without Claude composition."""
    text: str
    components: list[dict] = field(default_factory=list)
    escalated: bool = False
    escalation_reason: str = ""
    from_cache: bool = False
    intent: str = "other"          # classifier label for analytics
    routed_by: str = "router_fast" # 'exact_match'|'router_fast'|'router_index'


def _grade_label(grade_level: int | None) -> str:
    if grade_level is None:
        return ""
    if grade_level == 0:
        return "Grade R"
    return f"Grade {grade_level}"


def handle_grades(lti_session, grade_level: int | None) -> RouterResponse:
    """Fetch grades from Canvas and return structured response + components."""
    from .canvas import CanvasClient
    from .config import settings
    from .db import session_allows_course
    import re

    user_id = str(lti_session.user_id or "")
    cache_key = _cache_key(user_id, "grades", None)
    cached = _cache_get(cache_key)
    if cached is not None:
        resp = cached  # type: ignore[assignment]
        resp.from_cache = True  # type: ignore[union-attr]
        return resp  # type: ignore[return-value]

    if not re.match(r"^\d+$", user_id):
        return RouterResponse(
            text="I can't fetch your grades in demo mode — I need your real Canvas user ID.",
            components=[],
        )

    components: list[dict] = []
    grades_summary: list[str] = []

    try:
        with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
            enrollments = c.get_student_enrollments(int(user_id))
            course_name_map: dict[str, str] = {}
            for enr in enrollments:
                cid = str(enr.get("course_id", ""))
                if cid not in course_name_map:
                    try:
                        co = c.get_course(int(cid))
                        course_name_map[cid] = co.get("name") or f"Course {cid}"
                    except Exception:
                        course_name_map[cid] = enr.get("sis_course_id") or f"Course {cid}"

        for enr in enrollments:
            g = enr.get("grades") or {}
            cid = str(enr.get("course_id", ""))
            cname = course_name_map.get(cid, f"Course {cid}")
            score = g.get("current_score")
            grade = g.get("current_grade")
            grades_summary.append(
                f"{cname}: {score}%" if score is not None else f"{cname}: not yet recorded"
            )
            components.append({
                "type": "grades_card",
                "course_name": cname,
                "course_id": cid,
                "current_score": score,
                "current_grade": grade,
                "final_score": g.get("final_score"),
                "course_url": f"{settings.canvas_base_url}/courses/{cid}/grades",
            })
    except Exception as exc:
        log.warning("handle_grades error: %s", exc)
        return RouterResponse(
            text="I couldn't fetch your grades right now. Please try again in a moment.",
        )

    if not components:
        text = "I couldn't find any active grades yet — marks may still be loading."
    elif grade_level is not None and grade_level <= 3:
        # Foundation Phase — very short
        text = "Here are your grades! 😊"
    else:
        text = f"Here are your current grades across {len(components)} course(s)."

    result = RouterResponse(text=text, components=components)
    _cache_set(cache_key, result, "grades")
    return result


def _enrolled_ids(lti_session) -> list[str]:
    """Return the student's enrolled course IDs.

    Priority:
    1. Stored enrolled_course_ids on the session (set at LTI launch)
    2. The session's course_id (launch context)
    3. Live Canvas API fetch (fallback when launch fetch failed)
    """
    import json as _j
    raw = getattr(lti_session, "enrolled_course_ids", None) or "[]"
    try:
        ids = _j.loads(raw)
    except Exception:
        ids = []

    if not ids and lti_session.course_id:
        ids = [str(lti_session.course_id)]

    # Live fallback: stored list is empty and we have a numeric user_id
    if not ids:
        user_id = str(getattr(lti_session, "user_id", "") or "")
        import re as _re
        if _re.match(r"^\d+$", user_id):
            try:
                from .canvas import CanvasClient
                from .config import settings
                with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
                    enrollments = c.get_student_enrollments(int(user_id))
                ids = [str(e["course_id"]) for e in enrollments if e.get("course_id")]
                # Cache result back onto the session so next call is instant
                if ids:
                    import json as _jj
                    lti_session.enrolled_course_ids = _jj.dumps(ids)
            except Exception as exc:
                log.warning("_enrolled_ids live fetch failed for user %s: %s", user_id, exc)

    return [str(i) for i in ids if str(i).isdigit()]


def _course_picker(lti_session, prompt: str) -> RouterResponse:
    """Return a course_picker component so the student can choose without relaunching."""
    from .config import settings
    from .canvas import CanvasClient
    ids = _enrolled_ids(lti_session)
    courses: list[dict] = []
    if ids and settings.canvas_api_token and settings.canvas_base_url:
        try:
            with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
                for cid in ids[:10]:
                    try:
                        co = c.get_course(int(cid))
                        courses.append({
                            "id": cid,
                            "name": co.get("name", f"Course {cid}"),
                            "grade_level": None,
                        })
                    except Exception:
                        courses.append({"id": cid, "name": f"Course {cid}", "grade_level": None})
        except Exception:
            courses = [{"id": i, "name": f"Course {i}", "grade_level": None} for i in ids]
    elif ids:
        courses = [{"id": i, "name": f"Course {i}", "grade_level": None} for i in ids]

    if not courses:
        return RouterResponse(text="I couldn't find any courses for your account. Please contact the SGEG support team.")

    # If a course is already set on the session, use it silently — never ask again
    if lti_session.course_id and str(lti_session.course_id) in [c["id"] for c in courses]:
        return None  # type: ignore[return-value]

    if len(courses) == 1:
        # Only one course — auto-select, no question needed
        lti_session.course_id = courses[0]["id"]
        return None  # type: ignore[return-value]

    # Multiple courses and no course context — ask once, show picker
    return RouterResponse(
        text="Which course is this for?",
        components=[{"type": "course_picker", "courses": courses}],
        intent="other",
        routed_by="router_fast",
    )


def handle_due_dates(lti_session, grade_level: int | None) -> RouterResponse:
    """Fetch unsubmitted assignments using Canvas Analytics API.

    Uses /courses/:id/analytics/users/:uid/assignments which:
    - Only returns PUBLISHED assignments (unpublished never appear)
    - Provides Canvas's own status: missing|unsubmitted|floating|on_time|late|excused
    - floating = no due date + not submitted (e.g. Cambridge exam papers)
    """
    from .canvas import CanvasClient
    from .config import settings
    from .student_context import _friendly
    import re

    user_id = str(lti_session.user_id or "")
    cids    = _enrolled_ids(lti_session)

    if not cids or not re.match(r"^\d+$", user_id):
        return RouterResponse(text="Which course is this for? Tap one of the options below.", components=[])

    cache_key = _cache_key(user_id, "due_dates", ",".join(sorted(cids)))
    cached = _cache_get(cache_key)
    if cached is not None:
        resp = cached  # type: ignore[assignment]
        resp.from_cache = True  # type: ignore[union-attr]
        return resp  # type: ignore[return-value]

    items: list[dict] = []

    try:
        with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
            for course_id in cids:
                try:
                    analytics = c.get_student_assignment_analytics(int(course_id), int(user_id))
                    for a in analytics:
                        status = a.get("status", "")
                        # skip submitted (on_time/late) and excused
                        if status in ("on_time", "late", "excused") or a.get("excused"):
                            continue
                        # skip non-digital submissions (physical hand-in, not trackable)
                        if a.get("non_digital_submission"):
                            continue

                        aid = a.get("assignment_id") or a.get("id")
                        if status == "missing":
                            our_status = "overdue"
                        elif status == "floating" or not a.get("due_at"):
                            our_status = "floating"
                        else:
                            our_status = "upcoming"
                        items.append({
                            "name": a.get("title", "Assignment"),
                            "due_friendly": _friendly(a.get("due_at")),
                            "status": our_status,
                            "points_possible": a.get("points_possible"),
                            "url": f"{settings.canvas_base_url}/courses/{course_id}/assignments/{aid}",
                        })
                except Exception as exc:
                    log.warning("handle_due_dates analytics course=%s: %s", course_id, exc)

    except Exception as exc:
        log.warning("handle_due_dates error: %s", exc)
        return RouterResponse(text="I couldn't load your assignments right now. Please try again.")

    seen: set[str] = set()
    unique = [i for i in items if not (i["name"] in seen or seen.add(i["name"]))]  # type: ignore[func-returns-value]

    overdue_count   = sum(1 for i in unique if i["status"] == "overdue")
    upcoming_count  = sum(1 for i in unique if i["status"] == "upcoming")
    floating_count  = sum(1 for i in unique if i["status"] == "floating")
    unsubmitted_count = upcoming_count + floating_count
    total = len(unique)

    # Cards ordered: overdue → upcoming (has due date) → floating (no due date)
    overdue_items  = [i for i in unique if i["status"] == "overdue"]
    upcoming_items = [i for i in unique if i["status"] == "upcoming"]
    floating_items = [i for i in unique if i["status"] == "floating"]
    ordered = overdue_items + upcoming_items + floating_items
    components = [
        {"type": "assignment_card", **item}
        for item in ordered[:50]
    ]

    if not unique:
        text = "All your assignments are submitted — nothing outstanding right now."
    elif grade_level is not None and grade_level <= 3:
        text = f"You have {total} thing{'s' if total != 1 else ''} to do!"
    elif overdue_count and unsubmitted_count:
        text = (
            f"You have {overdue_count} overdue assignment{'s' if overdue_count != 1 else ''} "
            f"and {unsubmitted_count} not yet submitted. Tap any card to open it in Canvas."
        )
    elif overdue_count:
        text = f"You have {overdue_count} overdue assignment{'s' if overdue_count != 1 else ''} — tap a card to open it in Canvas."
    else:
        text = f"You have {unsubmitted_count} assignment{'s' if unsubmitted_count != 1 else ''} not yet submitted. Tap any card to open it in Canvas."

    result = RouterResponse(text=text, components=components)
    _cache_set(cache_key, result, "due_dates")
    return result


def search_index(
    query: str,
    lti_session,
    grade_level: int | None,
) -> RouterResponse | None:
    """Search the local Canvas content index for a student query.

    Returns a RouterResponse with matching items as module-section components,
    or None if the index has no results (fall through to live Canvas).
    Scoped to enrolled_course_ids — Phase 1 guaranteed.
    """
    import json as _json
    from .db import get_session as _get_session, search_content

    enrolled_raw = getattr(lti_session, "enrolled_course_ids", None) or "[]"
    try:
        enrolled_ids = _json.loads(enrolled_raw)
    except Exception:
        enrolled_ids = []

    if not enrolled_ids:
        return None

    with _get_session() as db:
        results = search_content(db, query, enrolled_ids, grade_level=grade_level, limit=12)

    if not results:
        return None

    # Group by module for display
    by_module: dict[str, dict] = {}
    for item in results:
        key = f"{item.course_id}:{item.module_id or 'top'}"
        if key not in by_module:
            by_module[key] = {
                "type": "module_section",
                "module_name": item.module_name or item.course_name,
                "module_id": item.module_id or "",
                "items": [],
            }
        by_module[key]["items"].append({
            "title": item.title,
            "type": item.item_type,
            "url": item.canvas_url,
        })

    components = list(by_module.values())

    if grade_level is not None and grade_level <= 3:
        text = f"I found {len(results)} thing{'s' if len(results) != 1 else ''}! 📁"
    else:
        text = f"I found {len(results)} matching item{'s' if len(results) != 1 else ''} in your course."

    return RouterResponse(text=text, components=components)


def handle_module_content(lti_session, grade_level: int | None) -> RouterResponse:
    """Fetch course modules grouped by term.

    Groups all modules by term label (handles English 'Term N' and
    Afrikaans 'Kwartaal N'). Each term becomes one card whose items are
    the individual week-modules, each linking directly to the Canvas
    modules page anchored to that module.
    """
    from .canvas import CanvasClient
    from .config import settings
    from .student_context import _extract_term
    import re

    course_id = str(lti_session.course_id or "")
    user_id   = str(lti_session.user_id or "")

    # No course context — show picker instead of blocking
    if not re.match(r"^\d+$", course_id):
        picker = _course_picker(
            lti_session,
            "Which course would you like to browse? Tap one below.",
        )
        if picker is not None:
            return picker
        course_id = str(lti_session.course_id or "")

    cache_key = _cache_key(user_id, "module_content", course_id)
    cached = _cache_get(cache_key)
    if cached is not None:
        resp = cached  # type: ignore[assignment]
        resp.from_cache = True  # type: ignore[union-attr]
        return resp  # type: ignore[return-value]

    max_visible = 5 if (grade_level is not None and grade_level <= 3) else 12

    components: list[dict] = []
    try:
        with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
            modules = c.list_modules(int(course_id))   # all modules, no slice

            # Group modules by term — preserves insertion order (Term 1 first)
            term_order: list[str] = []
            term_groups: dict[str, list] = {}
            for mod in modules:
                term = _extract_term(mod.get("name", "")) or "General"
                if term not in term_groups:
                    term_groups[term] = []
                    term_order.append(term)
                term_groups[term].append(mod)

            base = settings.canvas_base_url.rstrip("/")
            for term_label in term_order:
                term_mods = term_groups[term_label]
                items = [
                    {
                        "title": mod.get("name", ""),
                        "type": "ExternalUrl",
                        "url": f"{base}/courses/{course_id}/modules#{mod['id']}",
                    }
                    for mod in term_mods
                ]
                components.append({
                    "type": "module_section",
                    "module_name": term_label,
                    "module_id": term_label,
                    "items": items,
                    "max_visible": max_visible,
                })

    except Exception as exc:
        log.warning("handle_module_content error: %s", exc)
        return RouterResponse(text="I couldn't load the course content right now. Please try again.")

    if not components:
        text = "I couldn't find any modules in this course yet."
    elif grade_level is not None and grade_level <= 3:
        text = "Here's where to find your lessons!"
    else:
        term_count = len(components)
        text = (
            f"Here's your course content organised by term — "
            f"{term_count} term{'s' if term_count != 1 else ''} available. "
            "Tap any week to open it in Canvas."
        )

    result = RouterResponse(text=text, components=components)
    _cache_set(cache_key, result, "module_content")
    return result


def handle_curriculum_redirect(message: str, lti_session, grade_level: int | None) -> RouterResponse:
    """Curriculum topic question — out of scope, clean one-liner redirect."""
    return RouterResponse(
        text=(
            "That's a curriculum question — your course content is the best place for that! "
            "I'm here to help you navigate Canvas: check your grades, find assignments, "
            "and locate your lessons. Is there something specific in your course you'd like me to find?"
        ),
        intent="curriculum_redirect",
        routed_by="router_fast",
    )


# ── Public router entry point ─────────────────────────────────────────────────

def route(
    message: str,
    lti_session,
    grade_level: int | None,
    anthropic_api_key: str,
    *,
    shadow_mode: bool = False,
) -> RouterResponse | None:
    """Classify the message and return a RouterResponse if a fast handler applies.

    Returns None if the message should go through the full Claude tool-use path.

    shadow_mode=True classifies but always returns None (logging only).
    The plan specifies shadow mode for pre-go-live validation.
    """
    intent = classify_intent(message, anthropic_api_key)
    log.info("router intent=%s shadow=%s message=%.80s", intent, shadow_mode, message)

    if shadow_mode:
        return None  # let existing Claude path answer; just log intent

    if intent == "grades":
        r = handle_grades(lti_session, grade_level)
        r.intent = "grades"; return r
    if intent == "due_dates":
        r = handle_due_dates(lti_session, grade_level)
        r.intent = "due_dates"; return r
    if intent == "module_content":
        indexed = search_index(message, lti_session, grade_level)
        if indexed is not None:
            indexed.intent = "module_content"
            indexed.routed_by = "router_index"
            return indexed
        r = handle_module_content(lti_session, grade_level)
        r.intent = "module_content"; return r
    if intent == "curriculum_redirect":
        r = handle_curriculum_redirect(message, lti_session, grade_level)
        r.intent = "curriculum_redirect"; return r
    if intent == "escalation":
        return RouterResponse(
            text=(
                "It sounds like you're having a problem — the SGEG support team can help. "
                "Tap **Get Support** below and they'll follow up with you shortly."
            ),
            escalated=True,
            escalation_reason="other",
            intent="escalation",
        )
    return None  # "other" → full Claude path
