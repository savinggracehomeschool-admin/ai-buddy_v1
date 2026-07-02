"""Claude-powered nudge composer.

Uses Claude Haiku 4.5 with prompt caching on the (large) system prompt — every
nudge call shares the same instructions, so caching gives us real cost +
latency wins from the second call onward.

Composing happens in 'draft' mode: this module never sends. Day 5's Canvas
Conversations layer takes the ComposedNudge and dispatches it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from anthropic import Anthropic

from sgeg_nudge.config import settings
from sgeg_nudge.db import STATUS_PENDING, STATUS_REQUIRES_REVIEW

MODEL = "claude-haiku-4-5-20251001"
CHAT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 200
DIGEST_MAX_TOKENS = 400
CHAT_MAX_TOKENS = 1024   # larger budget for tool-use + rich replies
TEMPERATURE = 0.7
_MAX_TOOL_ITERATIONS = 5  # max Canvas tool calls per chat turn

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NUDGE_PROMPT_PATH = PROJECT_ROOT / "docs" / "02-nudge-prompt.md"
CHAT_PROMPT_PATH = PROJECT_ROOT / "docs" / "04-chat-prompt.md"

Tier = Literal["72h", "24h", "reinforce"]
Language = Literal["en", "af"]


@dataclass(frozen=True)
class ComposedNudge:
    text: str
    status: str  # STATUS_PENDING (ready to send) or STATUS_REQUIRES_REVIEW
    drift_reasons: tuple[str, ...]
    usage_input_tokens: int
    usage_output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


_SYSTEM_PROMPT: str | None = None

ESCALATE_CONTENT   = "[ESCALATE][CONTENT]"
ESCALATE_DISTRESS  = "[ESCALATE][DISTRESS]"
ESCALATE_TECHNICAL = "[ESCALATE][TECHNICAL]"
ESCALATE_OTHER     = "[ESCALATE][OTHER]"
_ESCALATE_TOKENS   = (ESCALATE_CONTENT, ESCALATE_DISTRESS, ESCALATE_TECHNICAL, ESCALATE_OTHER)


def _load_system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = NUDGE_PROMPT_PATH.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT


# Drift heuristics: if any fire, hold the nudge for human review instead of sending.
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}")
# Match grade/mark/score/result only in evaluation contexts.
# Bare "Grade" is fine (it's the year-level: "Grade 2 Mathematics") and triggers
# false positives. Look for possessive ("your grade") or numerical-result patterns.
_GRADE_RE = re.compile(
    r"\b(?:your|the|a) (?:grade|mark|score|result)\b(?!\s+\d)"  # "your grade" not followed by digit
    r"|\bpercentage\b"
    r"|\branking\b"
    r"|\b\d{1,3}\s*%\b"                                          # "85%"
    r"|\b\d+\s*/\s*\d+\s*marks?\b",                              # "85/100 marks"
    re.IGNORECASE,
)
_PARENT_RE = re.compile(r"\b(parent|guardian|moeder|vader|ouer|voog)\b", re.IGNORECASE)
_COMPARISON_RE = re.compile(
    r"\b(everyone else|other learners?|the rest of|ander leerders?)\b",
    re.IGNORECASE,
)


def detect_drift(text: str) -> list[str]:
    """Return a list of reasons the text should be reviewed before sending.

    Empty list means the text passes our safety checks.
    """
    reasons: list[str] = []
    if _URL_RE.search(text):
        reasons.append("contains URL/web reference")
    if _EMAIL_RE.search(text):
        reasons.append("contains email address")
    if _PHONE_RE.search(text):
        reasons.append("contains phone number")
    if _GRADE_RE.search(text):
        reasons.append("references grades/marks/scores")
    if _PARENT_RE.search(text):
        reasons.append("references parents/guardians")
    if _COMPARISON_RE.search(text):
        reasons.append("compares to other learners")
    words = text.split()
    if len(words) > 130:
        reasons.append(f"too long ({len(words)} words; cap ~120)")
    if len(words) < 8:
        reasons.append(f"suspiciously short ({len(words)} words)")
    return reasons


def compose_digest(
    *,
    learner_first_name: str,
    outstanding_items: list[dict],
    language: Language = "en",
    client: Anthropic | None = None,
) -> ComposedNudge:
    """Compose a weekly outstanding-work digest for one learner.

    `outstanding_items` is a list of dicts with keys: course_name,
    assignment_name, due_at_friendly, is_past_due.
    """
    if client is None:
        client = Anthropic(api_key=settings.anthropic_api_key)

    item_lines: list[str] = []
    for item in outstanding_items:
        marker = "past due" if item.get("is_past_due") else "due"
        when = item.get("due_at_friendly") or "soon"
        line = (
            f"- {item.get('course_name', '')}: {item.get('assignment_name', '')} "
            f"({marker} {when})"
        )
        item_lines.append(line)

    user_msg = (
        f"tier: weekly_digest\n"
        f"learner_first_name: {learner_first_name}\n"
        f"language: {language}\n"
        f"outstanding_count: {len(outstanding_items)}\n"
        f"outstanding_items:\n"
        + "\n".join(item_lines)
        + "\n"
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=DIGEST_MAX_TOKENS,
        temperature=TEMPERATURE,
        system=[
            {
                "type": "text",
                "text": _load_system_prompt(),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    reasons = detect_drift(text)
    # Digests are intentionally longer — relax the suspicious-short check,
    # since the "too long" check already protects the upper bound.
    reasons = [r for r in reasons if "suspiciously short" not in r]
    status = STATUS_REQUIRES_REVIEW if reasons else STATUS_PENDING

    usage = resp.usage
    return ComposedNudge(
        text=text,
        status=status,
        drift_reasons=tuple(reasons),
        usage_input_tokens=usage.input_tokens,
        usage_output_tokens=usage.output_tokens,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
    )


def compose_nudge(
    *,
    learner_first_name: str,
    assignment_name: str,
    due_at_friendly: str,
    tier: Tier,
    language: Language = "en",
    client: Anthropic | None = None,
) -> ComposedNudge:
    """Compose a single nudge. Does NOT send; caller decides what to do.

    Returns a ComposedNudge with status='pending' (ready to send) or
    'requires_review' (drift detected — admin should eyeball before sending).
    """
    if client is None:
        client = Anthropic(api_key=settings.anthropic_api_key)

    user_msg = (
        f"tier: {tier}\n"
        f"learner_first_name: {learner_first_name}\n"
        f"assignment_name: {assignment_name}\n"
        f"due_at_friendly: {due_at_friendly}\n"
        f"language: {language}\n"
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=[
            {
                "type": "text",
                "text": _load_system_prompt(),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    reasons = detect_drift(text)
    status = STATUS_REQUIRES_REVIEW if reasons else STATUS_PENDING
    # NB: text is preserved even when status=requires_review so the admin
    # can see what Claude tried to say.

    usage = resp.usage
    return ComposedNudge(
        text=text,
        status=status,
        drift_reasons=tuple(reasons),
        usage_input_tokens=usage.input_tokens,
        usage_output_tokens=usage.output_tokens,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
    )


# ── AI Buddy conversational chat ──────────────────────────────────────────────

@dataclass(frozen=True)
class ChatReply:
    text: str                    # visible reply text (escalation tokens stripped)
    components: list             # structured UI components built from tool results
    escalated: bool
    escalation_reason: str       # 'content' | 'distress' | 'technical' | 'other' | ''
    usage_input_tokens: int
    usage_output_tokens: int


# ── Canvas tool definitions ───────────────────────────────────────────────────

_CANVAS_TOOLS = [
    {
        "name": "get_student_grades",
        "description": (
            "Fetch the student's current grades for their enrolled courses from Canvas. "
            "Call this whenever a student asks about grades, marks, scores, results, or "
            "academic progress. Returns course names with current score and grade letter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "course_id": {
                    "type": "string",
                    "description": (
                        "Optional. Limit to a specific course ID. "
                        "Omit to return grades for all enrolled courses."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_upcoming_assignments",
        "description": (
            "Fetch assignments for the student from Canvas. "
            "Call this when a student asks about tasks, assignments, due dates, "
            "outstanding work, missing work, or what they might have missed. "
            "IMPORTANT: To find missing or overdue assignments, use bucket='missing' — "
            "this is the authoritative Canvas list of past-due unsubmitted work. "
            "Use bucket='upcoming' for work not yet due. Call both if the student "
            "asks generally about outstanding or remaining work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "course_id": {
                    "type": "string",
                    "description": "The course ID to fetch assignments for.",
                },
                "bucket": {
                    "type": "string",
                    "enum": ["upcoming", "missing", "past"],
                    "description": (
                        "'missing' for past-due assignments not yet submitted (overdue/outstanding work). "
                        "'upcoming' for assignments not yet due. "
                        "'past' for all past assignments regardless of submission status."
                    ),
                },
            },
            "required": ["course_id"],
        },
    },
    {
        "name": "get_course_modules",
        "description": (
            "Fetch the modules and all their content items (videos, worksheets, "
            "assignments, pages) from a Canvas course — with direct Canvas URLs. "
            "Call this when a student asks where to find something in the course, "
            "which week or module covers a topic, or how to navigate course content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "course_id": {
                    "type": "string",
                    "description": "The course ID to fetch modules for.",
                },
            },
            "required": ["course_id"],
        },
    },
    {
        "name": "search_canvas_content",
        "description": (
            "Search the local Canvas content index for modules, pages, assignments, "
            "quizzes, and files by keyword. Use this when a student is looking for "
            "specific content ('where is the video about fractions?', 'find the "
            "worksheet on photosynthesis', 'which module has the Term 2 timetable?'). "
            "Returns matching items with their Canvas URLs. "
            "Always call this BEFORE saying content cannot be found."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to search for, e.g. 'letter A video' or 'term 2 timetable'.",
                },
                "item_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional filter. One or more of: 'Page', 'Assignment', "
                        "'Quiz', 'File', 'Discussion'. Omit to search all types."
                    ),
                },
            },
            "required": ["query"],
        },
    },
]

# ── Canvas tool execution ─────────────────────────────────────────────────────

import json as _json
import re as _re
import logging as _logging

_tool_log = _logging.getLogger(__name__)


def _icon_for_type(item_type: str) -> str:
    return {
        "File": "📄",
        "Assignment": "📝",
        "Quiz": "📋",
        "Page": "📖",
        "Discussion": "💬",
        "ExternalUrl": "🔗",
        "ExternalTool": "🛠",
        "SubHeader": "",
    }.get(item_type, "📄")


def _run_canvas_tool(
    tool_name: str,
    tool_input: dict,
    lti_session,           # LTISession — imported lazily to avoid circular deps
) -> tuple[dict, list[dict]]:
    """Execute one Canvas tool call.

    Returns (raw_data_for_claude, ui_components).

    Security guardrails (Phase 1):
    - course_id is validated against lti_session.enrolled_course_ids
    - Cross-student data is impossible — user_id is always taken from the session
    - Sub-account scoping via lti_session.enrolled_account_ids
    """
    from .config import settings
    from .canvas import CanvasClient
    from .db import session_allows_course, session_allows_account

    user_id = str(lti_session.user_id or "")
    session_course_id = str(lti_session.course_id or "")
    canvas_ok = bool(settings.canvas_api_token and settings.canvas_base_url)

    if not canvas_ok:
        return {"error": "Canvas not configured"}, []

    numeric_uid = bool(_re.match(r"^\d+$", user_id))
    course_id = str(tool_input.get("course_id") or session_course_id)
    numeric_cid = bool(_re.match(r"^\d+$", course_id))

    # ── Phase 1 guardrail: enrollment check ───────────────────────────────────
    if course_id and numeric_cid and tool_name != "get_student_grades":
        if not session_allows_course(lti_session, course_id):
            _tool_log.warning(
                "Blocked tool=%s course_id=%s user=%s — not in enrolled_course_ids",
                tool_name, course_id, user_id,
            )
            return {
                "error": "Access denied",
                "reason": (
                    f"You are not enrolled in course {course_id}. "
                    "Please ask about one of your own courses."
                ),
            }, []

    # ── get_student_grades ────────────────────────────────────────────────────
    if tool_name == "get_student_grades":
        if not numeric_uid:
            return {"error": "Non-numeric user_id — grades unavailable in dev mode without a real Canvas user ID."}, []
        try:
            with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
                if tool_input.get("course_id") and numeric_cid:
                    enrollments = c.get_course_enrollment_for_user(int(course_id), int(user_id))
                else:
                    enrollments = c.get_student_enrollments(int(user_id))
                # Fetch course names — Canvas doesn't embed them in the enrollment response
                course_name_map: dict[str, str] = {}
                for enr in enrollments:
                    cid_str = str(enr.get("course_id", ""))
                    if cid_str and cid_str not in course_name_map:
                        try:
                            course_obj = c.get_course(int(cid_str))
                            course_name_map[cid_str] = course_obj.get("name") or f"Course {cid_str}"
                        except Exception:
                            course_name_map[cid_str] = enr.get("sis_course_id") or f"Course {cid_str}"
        except Exception as e:
            _tool_log.warning("get_student_grades failed: %s", e)
            return {"error": str(e)}, []

        grades_data: list[dict] = []
        components: list[dict] = []
        for enr in enrollments:
            g = enr.get("grades") or {}
            cid = str(enr.get("course_id", ""))
            cname = course_name_map.get(cid) or f"Course {cid}"
            score = g.get("current_score")
            grade = g.get("current_grade")
            grades_data.append({"course": cname, "score": score, "grade": grade})
            components.append({
                "type": "grades_card",
                "course_name": cname,
                "course_id": cid,
                "current_score": score,
                "current_grade": grade,
                "final_score": g.get("final_score"),
                "course_url": f"{settings.canvas_base_url}/courses/{cid}/grades",
            })
        return {"grades": grades_data}, components

    # ── get_upcoming_assignments ──────────────────────────────────────────────
    if tool_name == "get_upcoming_assignments":
        # Fall back to first enrolled course when no course_id supplied
        if not numeric_cid:
            import json as _j2
            enrolled = [
                s for s in _j2.loads(getattr(lti_session, "enrolled_course_ids", None) or "[]")
                if _re.match(r"^\d+$", str(s))
            ]
            if not enrolled:
                return {"error": "No enrolled courses found for this student."}, []
            course_id = str(enrolled[0])
            numeric_cid = True
        bucket = tool_input.get("bucket", "upcoming")
        from .student_context import _friendly
        items: list[dict] = []

        if not numeric_uid:
            return {"error": "Cannot verify submissions without a numeric Canvas user ID."}, []

        try:
            with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
                analytics = c.get_student_assignment_analytics(int(course_id), int(user_id))
        except Exception as e:
            _tool_log.warning("get_upcoming_assignments analytics failed: %s", e)
            return {"error": str(e)}, []

        for a in analytics:
            canvas_status = a.get("status", "")
            excused = bool(a.get("excused"))
            submitted = bool((a.get("submission") or {}).get("submitted_at"))

            # skip non-digital (physical hand-in) and excused
            if a.get("non_digital_submission") or excused:
                continue

            overdue  = canvas_status == "missing"
            upcoming = canvas_status in ("unsubmitted", "floating")

            if bucket == "missing" and not overdue:
                continue
            if bucket == "upcoming" and not upcoming:
                continue
            # bucket == "past" not meaningful with analytics; show all unsubmitted
            if submitted and bucket not in ("past",):
                continue

            if excused:
                our_status = "excused"
            elif submitted:
                our_status = "submitted"
            elif overdue:
                our_status = "overdue"
            else:
                our_status = "upcoming"

            aid = a.get("assignment_id") or a.get("id")
            items.append({
                "name": a.get("title", "Assignment"),
                "due_friendly": _friendly(a.get("due_at")),
                "status": our_status,
                "points_possible": a.get("points_possible"),
                "url": f"{settings.canvas_base_url}/courses/{course_id}/assignments/{aid}",
            })

        title_map = {
            "missing": "Missing / Overdue Work",
            "upcoming": "Upcoming Work",
            "past": "Past Assignments",
        }
        component = {
            "type": "assignment_list",
            "title": title_map.get(bucket, "Assignments"),
            "course_id": course_id,
            "items": items,
        }
        return {"assignments": items, "total": len(items)}, [component] if items else []

    # ── get_course_modules ────────────────────────────────────────────────────
    if tool_name == "get_course_modules":
        if not numeric_cid:
            import json as _j3
            enrolled = [
                s for s in _j3.loads(getattr(lti_session, "enrolled_course_ids", None) or "[]")
                if _re.match(r"^\d+$", str(s))
            ]
            if not enrolled:
                return {"error": "No enrolled courses found for this student."}, []
            if len(enrolled) == 1:
                course_id = str(enrolled[0])
                numeric_cid = True
            else:
                # Return course list so Claude can tell the student which courses exist
                return {
                    "enrolled_courses": enrolled,
                    "message": "Student has multiple enrolled courses. Ask which one they want.",
                }, [{
                    "type": "course_picker",
                    "courses": [{"id": cid, "name": f"Course {cid}"} for cid in enrolled],
                }]
        try:
            with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
                # Sub-account guardrail: check the course's account is in scope
                try:
                    course_obj = c.get_course(int(course_id))
                    acct_id = course_obj.get("account_id")
                    if acct_id and not session_allows_account(lti_session, int(acct_id)):
                        _tool_log.warning(
                            "Blocked get_course_modules: account_id=%s not in session scope user=%s",
                            acct_id, user_id,
                        )
                        return {"error": "Access denied", "reason": "Course is outside your enrolled sub-account."}, []
                except Exception:
                    pass  # allow if we can't verify; Canvas API will gate it
                modules = c.list_modules(int(course_id))
        except Exception as e:
            _tool_log.warning("get_course_modules failed: %s", e)
            return {"error": str(e)}, []

        mod_data: list[dict] = []
        components: list[dict] = []
        for mod in modules[:10]:
            try:
                with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
                    raw_items = c.list_module_items(int(course_id), mod["id"])
            except Exception:
                raw_items = []
            item_list = [
                {
                    "title": it.get("title", ""),
                    "type": it.get("type", ""),
                    "url": it.get("html_url", ""),
                }
                for it in raw_items[:15]
            ]
            mod_data.append({"module": mod.get("name", ""), "items": item_list})
            components.append({
                "type": "module_section",
                "module_name": mod.get("name", "Module"),
                "module_id": str(mod.get("id", "")),
                "items": item_list,
            })
        return {"modules": mod_data}, components

    # ── search_canvas_content ─────────────────────────────────────────────────
    if tool_name == "search_canvas_content":
        from .db import search_content, get_session as _get_db_session

        query = tool_input.get("query", "").strip()
        if not query:
            return {"error": "query is required"}, []

        item_types = tool_input.get("item_types") or None
        enrolled_raw2 = getattr(lti_session, "enrolled_course_ids", None) or "[]"
        try:
            enrolled_ids2: list[str] = _json.loads(enrolled_raw2)
        except Exception:
            enrolled_ids2 = []

        if not enrolled_ids2:
            return {"results": [], "message": "No enrolled courses to search."}, []

        try:
            with _get_db_session() as db:
                hits = search_content(
                    db,
                    query=query,
                    enrolled_course_ids=enrolled_ids2,
                    item_types=item_types,
                    grade_level=getattr(lti_session, "grade_level", None),
                    limit=10,
                )
                results = [
                    {
                        "title": h.title,
                        "type": h.item_type,
                        "module": h.module_name,
                        "url": h.canvas_url,
                        "course_id": h.course_id,
                    }
                    for h in hits
                ]
        except Exception as e:
            _tool_log.warning("search_canvas_content failed: %s", e)
            return {"error": str(e)}, []

        components = [
            {
                "type": "content_search_results",
                "query": query,
                "items": [
                    {
                        "title": r["title"],
                        "type": r["type"],
                        "module": r.get("module") or "",
                        "url": r["url"] or "",
                        "icon": _icon_for_type(r["type"]),
                    }
                    for r in results
                ],
            }
        ] if results else []

        return {"results": results, "total": len(results)}, components

    return {"error": f"Unknown tool '{tool_name}'"}, []


# ── Main chat function ────────────────────────────────────────────────────────

_CHAT_PROMPT_CACHE: str | None = None


def _load_chat_prompt_template() -> str:
    global _CHAT_PROMPT_CACHE
    if _CHAT_PROMPT_CACHE is None:
        if not CHAT_PROMPT_PATH.exists():
            raise FileNotFoundError(
                f"System prompt missing from container: {CHAT_PROMPT_PATH}. "
                "Add 'COPY docs/ ./docs/' to the Dockerfile."
            )
        _CHAT_PROMPT_CACHE = CHAT_PROMPT_PATH.read_text(encoding="utf-8")
    return _CHAT_PROMPT_CACHE


def compose_chat_reply(
    *,
    lti_session,                   # LTISession — passed directly so tools can use it
    history: list[dict],           # [{"role": "user"|"assistant", "content": "..."}]
    new_message: str,
    student_context: str = "",     # identity/enrollment block from student_context.py
    client: Anthropic | None = None,
) -> ChatReply:
    """Generate an AI Buddy chat reply using a Canvas tool-use loop.

    Claude decides which Canvas API calls to make (grades, assignments, modules),
    executes them through typed tool functions, builds rich UI components from the
    results, then writes a friendly narrative reply using the live data.

    The system prompt is prompt-cached so repeated turns in the same session only
    pay for the cache-read cost on the system block.
    """
    if client is None:
        client = Anthropic(api_key=settings.anthropic_api_key)

    # Build enrolled-course list for prompt-level self-restriction (Phase 1.3)
    enrolled_raw = getattr(lti_session, "enrolled_course_ids", None) or "[]"
    try:
        enrolled_ids: list[str] = _json.loads(enrolled_raw)
    except Exception:
        enrolled_ids = []
    enrolled_for_prompt = (
        ", ".join(enrolled_ids) if enrolled_ids else "unknown (dev mode)"
    )

    system_text = (
        _load_chat_prompt_template()
        .replace("{student_context}", student_context)
        .replace("{enrolled_course_ids}", enrolled_for_prompt)
    )

    messages: list[dict] = list(history) + [{"role": "user", "content": new_message}]
    all_components: list[dict] = []
    total_in = total_out = 0
    final_text = ""

    for _ in range(_MAX_TOOL_ITERATIONS):
        resp = client.messages.create(
            model=CHAT_MODEL,
            max_tokens=CHAT_MAX_TOKENS,
            temperature=TEMPERATURE,
            tools=_CANVAS_TOOLS,
            system=[{
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=messages,
        )
        usage = resp.usage
        total_in += usage.input_tokens
        total_out += usage.output_tokens

        if resp.stop_reason == "end_turn":
            final_text = "".join(
                b.text for b in resp.content if hasattr(b, "text")
            ).strip()
            break

        if resp.stop_reason == "tool_use":
            tool_results: list[dict] = []
            for block in resp.content:
                if block.type == "tool_use":
                    try:
                        raw_data, components = _run_canvas_tool(
                            block.name, block.input, lti_session
                        )
                        all_components.extend(components)
                    except Exception as exc:
                        _tool_log.exception("Tool %s failed: %s", block.name, exc)
                        raw_data = {"error": str(exc)}

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _json.dumps(raw_data, default=str),
                    })

            # Feed tool results back to Claude and continue
            messages.append({"role": "assistant", "content": list(resp.content)})
            messages.append({"role": "user", "content": tool_results})
        else:
            # Unexpected stop reason — grab whatever text is available
            final_text = "".join(
                b.text for b in resp.content if hasattr(b, "text")
            ).strip()
            break

    # Detect and strip escalation tokens from Claude's final text
    escalated = False
    escalation_reason = ""
    visible = final_text
    for token in _ESCALATE_TOKENS:
        if token in visible:
            escalated = True
            escalation_reason = token.split("][")[1].rstrip("]").lower()
            visible = visible.replace(token, "").strip()
            break

    # Strip any markdown links Claude emitted — the UI renders links via
    # structured components, so plain text should contain no [text](url) noise.
    _MD_LINK = _re.compile(r'\[([^\]]+)\]\(https?://[^\)]+\)')
    visible = _MD_LINK.sub(r'\1', visible).strip()

    return ChatReply(
        text=visible,
        components=all_components,
        escalated=escalated,
        escalation_reason=escalation_reason,
        usage_input_tokens=total_in,
        usage_output_tokens=total_out,
    )
