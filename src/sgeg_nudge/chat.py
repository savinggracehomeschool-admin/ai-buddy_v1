"""AI Buddy chat API endpoints.

All endpoints require a valid `session` query-param (UUID from LTI launch).
Passing it as a query param (rather than a cookie) keeps the app iframe-safe —
Canvas iframes block third-party cookies in most browsers.

Routes
──────
GET  /chat              Serve the chat UI (index.html)
GET  /api/chat/session  Return session metadata (student name, course, grade)
POST /api/chat/message  Send a message and get AI Buddy's reply
GET  /api/chat/history  Return last N messages for this session
POST /api/chat/escalate Manual escalation from the UI's "Talk to a Teacher" button
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .claude import compose_chat_reply
from .config import settings
from .db import ChatMessage, ChatTicket, LTISession, get_lti_session, get_session, record_audit, ACCOUNT_STREAMS
from .student_context import build_student_context, fetch_course_overview, _fetch_assignments

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

_STATIC_DIR = Path(__file__).parent / "static"
_MAX_HISTORY_TURNS = 10



# ── Auth helper ───────────────────────────────────────────────────────────────

def _require_session(session_id: str):
    """Return the LTISession or raise 401/403."""
    with get_session() as db:
        lti = get_lti_session(db, session_id)
    if lti is None:
        raise HTTPException(status_code=401, detail="Session not found or expired. Please relaunch from Canvas.")
    return lti


# ── UI endpoint ───────────────────────────────────────────────────────────────

@router.get("/chat", response_class=HTMLResponse)
def chat_ui(session: str = Query(..., description="LTI session ID")) -> HTMLResponse:
    """Serve the AI Buddy chat interface."""
    _require_session(session)  # validate before serving
    html_path = _STATIC_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="Chat UI not found.")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ── REST models ───────────────────────────────────────────────────────────────

class SessionInfo(BaseModel):
    user_name: str
    course_title: str | None
    grade_level: int | None
    grade_label: str


class MessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class MessageResponse(BaseModel):
    reply: str
    components: list = []
    escalated: bool
    escalation_reason: str


class HistoryItem(BaseModel):
    role: str
    content: str
    created_at: str
    escalated: bool


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/chat/session", response_model=SessionInfo)
def session_info(session: str = Query(...)) -> SessionInfo:
    lti = _require_session(session)
    grade = lti.grade_level
    if grade is None:
        label = "Unknown year"
    elif grade == 0:
        label = "Grade R"
    else:
        label = f"Grade {grade}"
    return SessionInfo(
        user_name=lti.user_name,
        course_title=lti.course_title,
        grade_level=grade,
        grade_label=label,
    )


@router.get("/api/chat/alerts")
def get_alerts(session: str = Query(...)) -> dict:
    """Return overdue/unsubmitted assignments for the dismissible alert banner."""
    lti = _require_session(session)
    from .router import handle_due_dates

    grade_level = getattr(lti, "grade_level", None)
    try:
        resp = handle_due_dates(lti, grade_level)
    except Exception:
        return {"count": 0, "missing": []}

    missing = []
    for comp in resp.components:
        if comp.get("type") == "assignment_card" and comp.get("status") == "overdue":
            missing.append(comp)

    return {"count": len(missing), "missing": missing[:5]}


@router.post("/api/chat/message", response_model=MessageResponse)
def send_message(
    body: MessageRequest,
    session: str = Query(...),
) -> MessageResponse:
    lti = _require_session(session)

    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured.")

    from .router import route as _route
    from .claude import ChatReply as _ChatReply

    t_start = time.monotonic()
    intent    = "other"
    routed_by = "full_claude"

    try:
        routed = _route(
            message=body.message,
            lti_session=lti,
            grade_level=lti.grade_level,
            anthropic_api_key=settings.anthropic_api_key,
            shadow_mode=False,
        )
    except Exception as exc:
        logger.exception("Router error — falling through to Claude: %s", exc)
        routed = None

    if routed is not None:
        intent    = routed.intent
        routed_by = routed.routed_by
        reply = _ChatReply(
            text=routed.text,
            components=routed.components,
            escalated=routed.escalated,
            escalation_reason=routed.escalation_reason,
            usage_input_tokens=0,
            usage_output_tokens=0,
        )
    else:
        student_ctx = build_student_context(lti)

        with get_session() as db:
            rows = (
                db.query(ChatMessage)
                .filter(ChatMessage.user_id == lti.user_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(_MAX_HISTORY_TURNS * 2)
                .all()
            )
        rows.reverse()
        history = [{"role": r.role, "content": r.content} for r in rows]

        try:
            reply = compose_chat_reply(
                lti_session=lti,
                student_context=student_ctx,
                history=history,
                new_message=body.message,
            )
            intent = getattr(reply, "intent", "other")
        except Exception as exc:
            logger.exception("Claude chat error: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="AI Buddy is unavailable right now. Please try again shortly.",
            ) from exc

    response_time_ms = int((time.monotonic() - t_start) * 1000)

    # Determine stream from session's launch account
    stream = ACCOUNT_STREAMS.get(lti.launch_account_id or 0)

    # Safety flag: distress escalations
    flagged     = reply.escalated and reply.escalation_reason == "distress"
    flag_reason = "distress_escalation" if flagged else None

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    with get_session() as db:
        # User message (no analytics — it's the question, not the answer)
        db.add(ChatMessage(
            session_id=session,
            user_id=lti.user_id,
            course_id=lti.course_id,
            grade_level=lti.grade_level,
            stream=stream,
            role="user",
            content=body.message,
            created_at=now,
            intent=intent,
        ))
        # Assistant message with full analytics
        db.add(ChatMessage(
            session_id=session,
            user_id=lti.user_id,
            course_id=lti.course_id,
            grade_level=lti.grade_level,
            stream=stream,
            role="assistant",
            content=reply.text,
            created_at=now,
            tokens_in=reply.usage_input_tokens,
            tokens_out=reply.usage_output_tokens,
            escalated=reply.escalated,
            intent=intent,
            routed_by=routed_by,
            response_time_ms=response_time_ms,
            cache_hit=bool(routed and routed.from_cache),
            flagged=flagged,
            flag_reason=flag_reason,
        ))

        if reply.escalated:
            # Build transcript of last 10 turns for the ticket
            recent = (
                db.query(ChatMessage)
                .filter(ChatMessage.user_id == lti.user_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(20)
                .all()
            )
            recent.reverse()
            transcript_data = [
                {"role": r.role, "content": r.content, "ts": r.created_at.isoformat()}
                for r in recent
            ]
            transcript_data.append({
                "role": "user", "content": body.message, "ts": now.isoformat()
            })

            db.add(ChatTicket(
                user_id=lti.user_id,
                user_name=lti.user_name,
                grade_level=lti.grade_level,
                course_id=lti.course_id,
                course_name=lti.course_title,
                stream=stream,
                category=_escalation_category(reply.escalation_reason),
                urgency="urgent" if reply.escalation_reason == "distress" else "normal",
                description=body.message[:500],
                transcript=json.dumps(transcript_data),
                status="new",
                created_at=now,
                updated_at=now,
            ))
            record_audit(
                db, "chat_escalation",
                entity_type="chat_session",
                detail=(
                    f"session={session} user={lti.user_id} "
                    f"reason={reply.escalation_reason} "
                    f"message={body.message[:120]}"
                ),
            )

        db.commit()

    # For technical escalations, submit to external ticketing system and
    # append the ticket reference to the reply so the student sees it
    ticket_ref: str | None = None
    if reply.escalated and reply.escalation_reason == "technical":
        try:
            ticket_ref = _submit_ticket_to_external(
                email=lti.user_email or lti.user_id,
                name=lti.user_name or "Student",
                course=lti.course_title,
                subject="Technical support request",
                description=body.message[:500],
                urgency="normal",
            )
        except Exception:
            logger.warning("Auto-escalation ticket submission failed", exc_info=True)

    reply_text = reply.text
    if ticket_ref:
        reply_text = f"{reply_text}\n\nYour reference number is **{ticket_ref}**."

    return MessageResponse(
        reply=reply_text,
        components=reply.components,
        escalated=reply.escalated,
        escalation_reason=reply.escalation_reason,
    )


def _escalation_category(reason: str) -> str:
    return {
        "content":   "academic",
        "technical": "technical",
        "distress":  "distress",
    }.get(reason, "other")


@router.get("/api/chat/history", response_model=list[HistoryItem])
def chat_history(
    session: str = Query(...),
    limit: int = Query(default=60, le=200),
) -> list[HistoryItem]:
    """Return unified conversation history for this student across all courses.

    Queries by user_id only — one chat thread per student regardless of which
    Canvas course they launched AI Buddy from.
    """
    lti = _require_session(session)
    with get_session() as db:
        rows = (
            db.query(ChatMessage)
            .filter(ChatMessage.user_id == lti.user_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
            .all()
        )
    rows.reverse()
    return [
        HistoryItem(
            role=r.role,
            content=r.content,
            created_at=r.created_at.isoformat(),
            escalated=r.escalated,
        )
        for r in rows
    ]


class EscalateRequest(BaseModel):
    reason: str = Field(default="other", max_length=64)
    message: str = Field(default="", max_length=500)
    subject: str = Field(default="", max_length=120)
    urgency: str = Field(default="normal", max_length=16)  # 'normal' | 'urgent'
    last_page: str = Field(default="", max_length=2000)   # Canvas URL the student was on


@router.post("/api/chat/escalate")
def manual_escalate(
    body: EscalateRequest,
    session: str = Query(...),
) -> dict:
    lti = _require_session(session)
    stream = ACCOUNT_STREAMS.get(lti.launch_account_id or 0)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    with get_session() as db:
        # Pull recent transcript
        recent = (
            db.query(ChatMessage)
            .filter(ChatMessage.user_id == lti.user_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(20).all()
        )
        recent.reverse()
        transcript_data = [
            {"role": r.role, "content": r.content, "ts": r.created_at.isoformat()}
            for r in recent
        ]
        db.add(ChatTicket(
            user_id=lti.user_id,
            sis_user_id=getattr(lti, "sis_user_id", None),
            user_name=lti.user_name,
            user_email=lti.user_email,
            grade_level=lti.grade_level,
            course_id=lti.course_id,
            course_name=lti.course_title,
            stream=stream,
            category=_escalation_category(body.reason),
            urgency=body.urgency,
            subject=body.subject or None,
            description=(body.message or "") + (f"\n\nCanvas page: {body.last_page}" if body.last_page else ""),
            transcript=json.dumps(transcript_data),
            status="new",
            created_at=now,
            updated_at=now,
        ))
        record_audit(
            db, "chat_manual_escalation",
            entity_type="chat_session",
            detail=(
                f"session={session} user={lti.user_id} "
                f"urgency={body.urgency} subject={body.subject[:60]} "
                f"reason={body.reason} message={body.message[:120]}"
            ),
        )
        db.commit()

    # Forward to external ticketing system (mock by default; swap TICKET_API_URL for real)
    ticket_ref = _submit_ticket_to_external(
        email=lti.user_email or lti.user_id,
        name=lti.user_name or "Student",
        sis_user_id=getattr(lti, "sis_user_id", None),
        canvas_user_id=lti.user_id,
        course=lti.course_title,
        subject=body.subject or "Support request",
        description=body.message or "",
        urgency=body.urgency,
        last_page=body.last_page or None,
    )

    msg = (
        f"Your request has been logged urgently — the SGEG support team will follow up as soon as possible. "
        f"Reference: {ticket_ref}"
        if body.urgency == "urgent"
        else f"Your request has been logged — the SGEG support team will follow up with you shortly. "
             f"Reference: {ticket_ref}"
    )
    return {"status": "escalated", "ticket_ref": ticket_ref, "message": msg}


class ExternalTicketPayload(BaseModel):
    email: str
    name: str
    course: str | None = None
    subject: str
    description: str
    urgency: str = "normal"
    ticket_ref: str | None = None


@router.post("/api/mock-tickets/create")
def mock_ticket_create(payload: ExternalTicketPayload) -> dict:
    """Mock external ticketing API — replace URL + auth with real system later.

    Accepts a ticket payload, stores it locally, and returns a fake ticket
    reference. Swap this endpoint for the real ticketing system API when ready.
    """
    import random, string
    ref = "SGT-" + "".join(random.choices(string.digits, k=6))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_session() as db:
        db.add(ChatTicket(
            user_id=payload.email,
            user_name=payload.name,
            course_name=payload.course,
            category="technical",
            urgency=payload.urgency,
            subject=payload.subject,
            description=payload.description,
            status="new",
            created_at=now,
            updated_at=now,
        ))
        db.commit()
    return {
        "status": "created",
        "ticket_ref": ref,
        "message": f"Ticket {ref} created successfully.",
    }


def _submit_ticket_to_external(
    *,
    email: str,
    name: str,
    sis_user_id: str | None = None,
    canvas_user_id: str | None = None,
    course: str | None,
    subject: str,
    description: str,
    urgency: str = "normal",
    last_page: str | None = None,
) -> str:
    """Submit a ticket to the external ticketing API.

    Currently points to the mock endpoint. Replace TICKET_API_URL in .env
    with your real ticketing system URL when ready.
    """
    import urllib.request
    import urllib.error

    # Points at the SGEG Help Desk connector, which creates the ticket in JS Help Desk
    # (the SGEG app then syncs it and the AI replies). Set both in .env.
    ticket_url = (
        os.getenv("TICKET_API_URL")
        or "https://www.savinggraceeducation.co.za/wp-json/sgeg/v1/tickets"
    )
    ticket_key = os.getenv("TICKET_API_KEY", "")

    payload_bytes = json.dumps({
        "email": email,
        "name": name,
        "sis_user_id": sis_user_id,
        "canvas_user_id": canvas_user_id,
        "course": course,
        "subject": subject,
        "description": description,
        "urgency": urgency,
        "last_page": last_page,
    }).encode()

    headers = {"Content-Type": "application/json"}
    if ticket_key:
        headers["Authorization"] = f"Bearer {ticket_key}"
    req = urllib.request.Request(
        ticket_url,
        data=payload_bytes,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("ticket_ref", "SGT-000000")
    except Exception as e:
        logger.warning("External ticket API failed: %s", e)
        return "SGT-000000"


@router.get("/api/canvas/sync/status")
def sync_status(session: str = Query(...)) -> dict:
    """Return the status of the last Canvas content sync."""
    _require_session(session)
    from .sync import get_last_sync
    return get_last_sync()


@router.post("/api/canvas/sync/course/{course_id}")
def sync_course_now(course_id: str, session: str = Query(...)) -> dict:
    """Trigger an on-demand re-sync for one course (e.g. after publishing new content)."""
    lti = _require_session(session)
    from .db import session_allows_course
    if not session_allows_course(lti, course_id):
        raise HTTPException(status_code=403, detail="Course not in your enrolled courses.")
    from .sync import sync_one_course
    return sync_one_course(course_id)


@router.post("/api/chat/logout")
def logout(session: str = Query(...)) -> dict:
    """Phase 5: explicit session logout — invalidates the session immediately."""
    from datetime import timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_session() as db:
        lti = db.get(LTISession, session)
        if lti:
            lti.expires_at = now  # expire immediately
            db.commit()
    return {"status": "logged_out"}


# ── Canvas diagnostic endpoint ────────────────────────────────────────────────

@router.get("/api/canvas/course/{course_id}/explore")
def explore_course(
    course_id: str,
    session: str = Query(...),
) -> dict:
    """Fetch and return every layer of Canvas data for a course.

    Used to verify that AI Buddy is pulling the correct live data before
    relying on it in chat. Shows: course record, modules, module items,
    page content excerpts, and assignment descriptions.

    Requires a valid LTI session (any session works — the course_id is
    passed explicitly so you can inspect any course you have access to).
    """
    lti = _require_session(session)

    from .student_context import fetch_course_overview, _fetch_assignments
    from .config import settings

    result: dict = {
        "canvas_base_url": settings.canvas_base_url,
        "course_id": course_id,
        "session_user": lti.user_name,
    }

    # Course overview (modules, pages, assignments)
    overview = fetch_course_overview(course_id)
    result["course"] = overview.get("course")
    result["modules"] = overview.get("modules", [])
    result["pages_fetched"] = overview.get("pages_fetched", [])
    result["assignments_fetched"] = overview.get("assignments_fetched", [])
    result["error"] = overview.get("error")

    # Student submission data (if numeric user ID)
    import re as _re
    if _re.match(r"^\d+$", lti.user_id or ""):
        result["student_assignments"] = _fetch_assignments(lti.user_id, course_id)
    else:
        result["student_assignments"] = []
        result["note"] = "Dev-mode session: non-numeric user_id, submission data skipped."

    return result


# ── Canvas Theme Badge auth ───────────────────────────────────────────────────
# Called by ai_buddy_badge.js when a student clicks the floating Coach button.
# Validates the Canvas user via the admin API token, then creates a session.

class BadgeSessionRequest(BaseModel):
    canvas_user_id:   str = Field(..., min_length=1, max_length=64)
    canvas_course_id: str = Field(default="", max_length=64)
    canvas_user_name: str = Field(default="", max_length=300)
    canvas_domain:    str = Field(default="", max_length=200)


@router.post("/api/badge/session")
def badge_session(body: BadgeSessionRequest) -> dict:
    """Create a chat session from a Canvas Theme badge click.

    Validates the user against the Canvas API using our admin token,
    then creates an LTISession so the chat UI can load normally.
    """
    import re as _re
    import uuid as _uuid
    from datetime import timedelta
    import httpx as _httpx

    # Validate: user_id must be numeric
    if not _re.match(r"^\d+$", body.canvas_user_id.strip()):
        raise HTTPException(status_code=400, detail="Invalid canvas_user_id")

    # Verify user exists via Canvas API
    try:
        with _httpx.Client(timeout=10) as http:
            resp = http.get(
                f"{settings.canvas_base_url}/api/v1/users/{body.canvas_user_id}/profile",
                headers={"Authorization": f"Bearer {settings.canvas_api_token}"},
            )
        if resp.status_code == 404:
            raise HTTPException(status_code=403, detail="User not found in Canvas")
        resp.raise_for_status()
        profile = resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("badge_session: Canvas profile lookup failed: %s", exc)
        raise HTTPException(status_code=503, detail="Could not verify user with Canvas")

    user_name = (
        profile.get("name")
        or profile.get("short_name")
        or body.canvas_user_name
        or "Student"
    )
    user_email = profile.get("primary_email") or profile.get("login_id") or None

    # Resolve course title if course_id given
    course_title = None
    grade_level  = None
    course_id    = body.canvas_course_id.strip() or None
    if course_id and _re.match(r"^\d+$", course_id):
        try:
            with _httpx.Client(timeout=10) as http:
                cr = http.get(
                    f"{settings.canvas_base_url}/api/v1/courses/{course_id}",
                    headers={"Authorization": f"Bearer {settings.canvas_api_token}"},
                )
            if cr.status_code == 200:
                course_data  = cr.json()
                course_title = course_data.get("name") or course_data.get("course_code")
                from .lti import _extract_grade_level
                grade_level = _extract_grade_level(course_title)
        except Exception as exc:
            logger.debug("badge_session: course lookup failed (non-fatal): %s", exc)

    ttl_hours = 2 if (grade_level is not None and grade_level <= 3) else 8
    now        = datetime.now(timezone.utc).replace(tzinfo=None)
    session_id = str(_uuid.uuid4())

    with get_session() as db:
        row = LTISession(
            session_id=session_id,
            user_id=body.canvas_user_id.strip(),
            course_id=course_id,
            user_name=user_name,
            user_email=user_email,
            roles="Student",
            course_title=course_title,
            grade_level=grade_level,
            platform_id=settings.canvas_base_url,
            created_at=now,
            expires_at=now + timedelta(hours=ttl_hours),
        )
        db.add(row)
        db.commit()

    logger.info("badge_session: created session %s for user %s course %s",
                session_id[:8], body.canvas_user_id, course_id)
    return {"session_id": session_id}
