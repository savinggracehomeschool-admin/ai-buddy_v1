"""AI Buddy Admin Dashboard — /dashboard

Five sections (tab-based SPA):
  1. Analytics    — intent breakdown, volume, top questions, grade/stream split
  2. Tickets      — queue with status workflow + transcript viewer
  3. Safety       — flagged conversations + guardrail hits
  4. Health       — response times, token spend, cache hit rate, API errors
  5. Students     — per-student view: conversations, tickets, usage pattern

Role-based access
─────────────────
  • ADMIN_USER (from .env) → full access, all sub-accounts
  • TEACHER_USER_<n>       → course-scoped (future; skeleton present)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import secrets as _secrets

import jwt as _jwt
from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import settings
from .db import (
    AuditLog,
    CanvasSyncLog,
    ChatMessage,
    ChatTicket,
    LTISession,
    get_session,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_COOKIE = "sgeg_admin"
_ALGO   = "HS256"
_TTL_H  = 8


# ── Cookie-based auth ─────────────────────────────────────────────────────────

def _make_token() -> str:
    return _jwt.encode(
        {"sub": settings.admin_user, "exp": datetime.now(timezone.utc) + timedelta(hours=_TTL_H)},
        settings.lti_secret_key, algorithm=_ALGO,
    )

def _require_admin(request: Request, sgeg_admin: str | None = Cookie(default=None)) -> str:
    try:
        payload = _jwt.decode(sgeg_admin or "", settings.lti_secret_key, algorithms=[_ALGO])
        return payload["sub"]
    except Exception:
        from fastapi.responses import RedirectResponse as _RR
        raise HTTPException(status_code=401, detail="login_required")


# ── Login page ────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    err_html = f'<p class="error">{error}</p>' if error else ""
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>SGEG Education Coach — Login</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  background:#F0F4F7;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(10,34,64,.12);
  padding:40px 36px;width:100%;max-width:380px}}
.logo{{display:flex;align-items:center;gap:12px;margin-bottom:28px}}
.logo svg{{width:44px;height:44px;flex-shrink:0}}
.logo-text h1{{font-size:1rem;font-weight:800;color:#0A2240;line-height:1.2}}
.logo-text p{{font-size:.75rem;color:#6B7280;margin-top:2px}}
label{{display:block;font-size:.82rem;font-weight:600;color:#0A2240;margin-bottom:5px}}
input{{width:100%;border:1.5px solid #E5E7EB;border-radius:8px;padding:10px 12px;
  font-size:.9rem;outline:none;transition:border-color .15s;margin-bottom:14px}}
input:focus{{border-color:#007A87}}
button{{width:100%;padding:11px;background:#0A2240;color:#fff;border:none;
  border-radius:8px;font-size:.9rem;font-weight:700;cursor:pointer;margin-top:4px;
  transition:background .15s}}
button:hover{{background:#1a367e}}
.error{{color:#B91C1C;font-size:.82rem;background:#FEF2F2;border:1px solid #FECACA;
  border-radius:6px;padding:8px 12px;margin-bottom:14px}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <svg viewBox="0 0 44 44" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="22" cy="22" r="21" fill="#0A2240"/>
      <rect x="8" y="10" width="28" height="6" rx="3" fill="#007A87"/>
      <circle cx="16" cy="24" r="3" fill="#F59E0B"/>
      <circle cx="28" cy="24" r="3" fill="#F59E0B"/>
      <path d="M14 32 Q22 37 30 32" stroke="white" stroke-width="2.5" fill="none" stroke-linecap="round"/>
    </svg>
    <div class="logo-text">
      <h1>Education Coach</h1>
      <p>SGEG Admin Dashboard</p>
    </div>
  </div>
  {err_html}
  <form method="post" action="/dashboard/login">
    <label for="username">Username</label>
    <input id="username" name="username" type="text" autocomplete="username" required/>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required/>
    <button type="submit">Sign in</button>
  </form>
</div>
</body>
</html>""")


@router.post("/login")
def login_submit(username: str = Form(...), password: str = Form(...)):
    ok = (
        _secrets.compare_digest(username, settings.admin_user) and
        _secrets.compare_digest(password, settings.admin_password)
    )
    if not ok:
        return RedirectResponse("/dashboard/login?error=Incorrect+username+or+password", status_code=303)
    resp = RedirectResponse("/dashboard/", status_code=303)
    resp.set_cookie(_COOKIE, _make_token(), httponly=True, samesite="lax", max_age=_TTL_H * 3600)
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/dashboard/login", status_code=303)
    resp.delete_cookie(_COOKIE)
    return resp


# ── Analytics API ─────────────────────────────────────────────────────────────

@router.get("/api/analytics/overview")
def analytics_overview(_: str = Depends(_require_admin)):
    """Headline numbers for the stat cards."""
    with get_session() as db:
        total_messages = db.query(ChatMessage).filter(ChatMessage.role == "user").count()
        total_students = db.query(ChatMessage.user_id).filter(ChatMessage.role == "user").distinct().count()
        total_tickets  = db.query(ChatTicket).count()
        open_tickets   = db.query(ChatTicket).filter(ChatTicket.status != "resolved").count()
        flagged        = db.query(ChatMessage).filter(ChatMessage.flagged == True).count()  # noqa
        avg_ms = db.query(
            db.query(ChatMessage.response_time_ms)
            .filter(ChatMessage.role == "assistant", ChatMessage.response_time_ms != None)  # noqa
            .subquery()
        )
        # Simple avg via Python since SQLite has no AVG in this context
        times = [
            r[0] for r in
            db.query(ChatMessage.response_time_ms)
            .filter(ChatMessage.role == "assistant", ChatMessage.response_time_ms != None)  # noqa
            .all()
        ]
        avg_response_ms = int(sum(times) / len(times)) if times else 0
        cache_hits = db.query(ChatMessage).filter(
            ChatMessage.role == "assistant", ChatMessage.cache_hit == True  # noqa
        ).count()
        cache_total = db.query(ChatMessage).filter(ChatMessage.role == "assistant").count()

    return {
        "total_messages":    total_messages,
        "total_students":    total_students,
        "total_tickets":     total_tickets,
        "open_tickets":      open_tickets,
        "flagged_messages":  flagged,
        "avg_response_ms":   avg_response_ms,
        "cache_hit_rate":    round(cache_hits / cache_total * 100, 1) if cache_total else 0,
    }


@router.get("/api/analytics/intents")
def analytics_intents(_: str = Depends(_require_admin)):
    """Intent distribution for doughnut chart."""
    with get_session() as db:
        from sqlalchemy import func
        rows = (
            db.query(ChatMessage.intent, func.count().label("n"))
            .filter(ChatMessage.role == "user", ChatMessage.intent != None)  # noqa
            .group_by(ChatMessage.intent)
            .all()
        )
    return [{"intent": r.intent or "other", "count": r.n} for r in rows]


@router.get("/api/analytics/volume")
def analytics_volume(days: int = Query(default=14), _: str = Depends(_require_admin)):
    """Messages per day for the last N days (line chart)."""
    with get_session() as db:
        from sqlalchemy import func
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        rows = (
            db.query(
                func.date(ChatMessage.created_at).label("day"),
                func.count().label("n"),
            )
            .filter(ChatMessage.role == "user", ChatMessage.created_at >= cutoff)
            .group_by("day")
            .order_by("day")
            .all()
        )
    return [{"day": str(r.day), "count": r.n} for r in rows]


@router.get("/api/analytics/grade-split")
def analytics_grade_split(_: str = Depends(_require_admin)):
    """Message count by grade level."""
    with get_session() as db:
        from sqlalchemy import func
        rows = (
            db.query(ChatMessage.grade_level, func.count().label("n"))
            .filter(ChatMessage.role == "user")
            .group_by(ChatMessage.grade_level)
            .order_by(ChatMessage.grade_level)
            .all()
        )

    def label(g):
        if g is None: return "Unknown"
        if g == 0:    return "Grade R"
        return f"Grade {g}"

    return [{"grade": label(r.grade_level), "count": r.n} for r in rows]


@router.get("/api/analytics/stream-split")
def analytics_stream_split(_: str = Depends(_require_admin)):
    """Message count by curriculum stream."""
    with get_session() as db:
        from sqlalchemy import func
        rows = (
            db.query(ChatMessage.stream, func.count().label("n"))
            .filter(ChatMessage.role == "user")
            .group_by(ChatMessage.stream)
            .all()
        )
    return [{"stream": r.stream or "Unknown", "count": r.n} for r in rows]


@router.get("/api/analytics/top-questions")
def analytics_top_questions(limit: int = Query(default=20), _: str = Depends(_require_admin)):
    """Most frequent student questions (exact message text + count)."""
    with get_session() as db:
        from sqlalchemy import func
        rows = (
            db.query(ChatMessage.content, func.count().label("n"))
            .filter(ChatMessage.role == "user")
            .group_by(ChatMessage.content)
            .order_by(func.count().desc())
            .limit(limit)
            .all()
        )
    return [{"message": r.content[:200], "count": r.n} for r in rows]


# ── Tickets API ───────────────────────────────────────────────────────────────

@router.get("/api/tickets")
def list_tickets(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50),
    _: str = Depends(_require_admin),
):
    with get_session() as db:
        q = db.query(ChatTicket).order_by(ChatTicket.created_at.desc())
        if status:
            q = q.filter(ChatTicket.status == status)
        rows = q.limit(limit).all()
    return [
        {
            "id":           r.id,
            "user_name":    r.user_name,
            "grade_level":  r.grade_level,
            "course_name":  r.course_name,
            "stream":       r.stream,
            "category":     r.category,
            "urgency":      r.urgency,
            "subject":      r.subject,
            "description":  r.description,
            "status":       r.status,
            "assigned_to":  r.assigned_to,
            "created_at":   r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/api/tickets/{ticket_id}/transcript")
def ticket_transcript(ticket_id: int, _: str = Depends(_require_admin)):
    with get_session() as db:
        t = db.get(ChatTicket, ticket_id)
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    try:
        transcript = json.loads(t.transcript or "[]")
    except Exception:
        transcript = []
    return {"ticket_id": ticket_id, "transcript": transcript}


@router.patch("/api/tickets/{ticket_id}")
def update_ticket(
    ticket_id: int,
    status: Optional[str] = Query(default=None),
    assigned_to: Optional[str] = Query(default=None),
    resolution_note: Optional[str] = Query(default=None),
    _: str = Depends(_require_admin),
):
    valid_statuses = {"new", "assigned", "in_progress", "resolved"}
    if status and status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Status must be one of {valid_statuses}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_session() as db:
        t = db.get(ChatTicket, ticket_id)
        if not t:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if status:
            t.status = status
            if status == "resolved":
                t.resolved_at = now
        if assigned_to is not None:
            t.assigned_to = assigned_to
        if resolution_note is not None:
            t.resolution_note = resolution_note
        t.updated_at = now
        db.commit()
    return {"id": ticket_id, "status": t.status}


# ── Safety API ────────────────────────────────────────────────────────────────

@router.get("/api/safety/flags")
def safety_flags(limit: int = Query(default=50), _: str = Depends(_require_admin)):
    with get_session() as db:
        rows = (
            db.query(ChatMessage)
            .filter(ChatMessage.flagged == True)  # noqa
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
            .all()
        )
    return [
        {
            "id":          r.id,
            "user_id":     r.user_id,
            "grade_level": r.grade_level,
            "stream":      r.stream,
            "content":     r.content[:300],
            "flag_reason": r.flag_reason,
            "created_at":  r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/api/safety/guardrail-hits")
def guardrail_hits(limit: int = Query(default=50), _: str = Depends(_require_admin)):
    """Audit log rows where Phase 1 security scoping blocked a request."""
    with get_session() as db:
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.event == "security_block")
            .order_by(AuditLog.ts.desc())
            .limit(limit)
            .all()
        )
    return [
        {"ts": r.ts.isoformat(), "detail": r.detail}
        for r in rows
    ]


@router.get("/api/safety/bot-responses")
def bot_response_sample(limit: int = Query(default=30), _: str = Depends(_require_admin)):
    """Sample of recent bot responses for quality review."""
    with get_session() as db:
        rows = (
            db.query(ChatMessage)
            .filter(ChatMessage.role == "assistant")
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
            .all()
        )
    return [
        {
            "id":              r.id,
            "user_id":         r.user_id,
            "grade_level":     r.grade_level,
            "intent":          r.intent,
            "content":         r.content[:400],
            "escalated":       r.escalated,
            "response_time_ms": r.response_time_ms,
            "created_at":      r.created_at.isoformat(),
        }
        for r in rows
    ]


# ── Health API ────────────────────────────────────────────────────────────────

@router.get("/api/health")
def system_health(_: str = Depends(_require_admin)):
    with get_session() as db:
        from sqlalchemy import func

        # Response time by path
        paths = (
            db.query(ChatMessage.routed_by, func.avg(ChatMessage.response_time_ms).label("avg_ms"))
            .filter(ChatMessage.role == "assistant", ChatMessage.response_time_ms != None)  # noqa
            .group_by(ChatMessage.routed_by)
            .all()
        )

        # Token spend last 7 days
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
        token_rows = (
            db.query(
                func.date(ChatMessage.created_at).label("day"),
                func.sum(ChatMessage.tokens_in + ChatMessage.tokens_out).label("tokens"),
            )
            .filter(ChatMessage.role == "assistant", ChatMessage.created_at >= cutoff)
            .group_by("day")
            .order_by("day")
            .all()
        )

        # Cache stats
        cache_hit  = db.query(ChatMessage).filter(ChatMessage.cache_hit == True, ChatMessage.role == "assistant").count()  # noqa
        cache_total = db.query(ChatMessage).filter(ChatMessage.role == "assistant").count()

        # Last sync
        last_sync = db.query(CanvasSyncLog).order_by(CanvasSyncLog.started_at.desc()).first()
        content_items = db.execute(
            db.query(func.count()).select_from(__import__("sgeg_nudge.db", fromlist=["CanvasContentItem"]).CanvasContentItem).statement
        ).scalar() if False else None  # will fetch below

    from .db import CanvasContentItem as _CCI
    with get_session() as db:
        content_item_count = db.query(_CCI).count()

    return {
        "response_times": [
            {"path": r.routed_by or "unknown", "avg_ms": round(r.avg_ms or 0)}
            for r in paths
        ],
        "token_spend_7d": [
            {"day": str(r.day), "tokens": r.tokens or 0}
            for r in token_rows
        ],
        "cache_hit_rate": round(cache_hit / cache_total * 100, 1) if cache_total else 0,
        "canvas_index": {
            "items":       content_item_count,
            "last_sync":   last_sync.finished_at.isoformat() if last_sync and last_sync.finished_at else None,
            "sync_status": last_sync.status if last_sync else "never_run",
        },
    }


# ── Students API ──────────────────────────────────────────────────────────────

@router.get("/api/students")
def list_students(limit: int = Query(default=50), _: str = Depends(_require_admin)):
    """All distinct students with message count and last seen."""
    with get_session() as db:
        from sqlalchemy import func
        rows = (
            db.query(
                ChatMessage.user_id,
                func.count().label("messages"),
                func.max(ChatMessage.created_at).label("last_seen"),
                ChatMessage.grade_level,
                ChatMessage.stream,
            )
            .filter(ChatMessage.role == "user")
            .group_by(ChatMessage.user_id)
            .order_by(func.max(ChatMessage.created_at).desc())
            .limit(limit)
            .all()
        )

        # Get names from LTI sessions
        user_ids = [r.user_id for r in rows]
        sessions = (
            db.query(LTISession.user_id, LTISession.user_name)
            .filter(LTISession.user_id.in_(user_ids))
            .all()
        )
        name_map = {s.user_id: s.user_name for s in sessions}

    return [
        {
            "user_id":     r.user_id,
            "user_name":   name_map.get(r.user_id, r.user_id),
            "messages":    r.messages,
            "grade_level": r.grade_level,
            "stream":      r.stream,
            "last_seen":   r.last_seen.isoformat() if r.last_seen else None,
        }
        for r in rows
    ]


@router.get("/api/students/{user_id}")
def student_detail(user_id: str, _: str = Depends(_require_admin)):
    """Full history for one student: messages, tickets, intent breakdown."""
    with get_session() as db:
        from sqlalchemy import func

        messages = (
            db.query(ChatMessage)
            .filter(ChatMessage.user_id == user_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(100)
            .all()
        )
        tickets = (
            db.query(ChatTicket)
            .filter(ChatTicket.user_id == user_id)
            .order_by(ChatTicket.created_at.desc())
            .all()
        )
        intent_rows = (
            db.query(ChatMessage.intent, func.count().label("n"))
            .filter(ChatMessage.user_id == user_id, ChatMessage.role == "user")
            .group_by(ChatMessage.intent)
            .all()
        )
        session_row = (
            db.query(LTISession)
            .filter(LTISession.user_id == user_id)
            .order_by(LTISession.created_at.desc())
            .first()
        )

    return {
        "user_id":   user_id,
        "user_name": session_row.user_name if session_row else user_id,
        "grade_level": session_row.grade_level if session_row else None,
        "stream":      session_row.launch_account_id if session_row else None,
        "messages":  [
            {
                "role":    m.role,
                "content": m.content[:300],
                "intent":  m.intent,
                "routed_by": m.routed_by,
                "response_time_ms": m.response_time_ms,
                "escalated": m.escalated,
                "flagged":   m.flagged,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
        "tickets": [
            {
                "id":         t.id,
                "category":   t.category,
                "urgency":    t.urgency,
                "status":     t.status,
                "subject":    t.subject,
                "created_at": t.created_at.isoformat(),
            }
            for t in tickets
        ],
        "intent_breakdown": [
            {"intent": r.intent or "other", "count": r.n}
            for r in intent_rows
        ],
    }


# ── Dashboard HTML SPA ────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard_home(request: Request, sgeg_admin: str | None = Cookie(default=None)):
    try:
        _require_admin(request, sgeg_admin)
    except HTTPException:
        return RedirectResponse("/dashboard/login", status_code=303)
    return HTMLResponse(_DASHBOARD_HTML)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>SGEG Education Coach — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --brand:#0A2240;--brand-pale:#E8F4F6;--accent:#007A87;
  --surface:#fff;--bg:#F0F4F7;--text:#0A2240;--text2:#6B7280;
  --border:#E5E7EB;--radius:12px;
  --green:#059669;--amber:#D97706;--red:#DC2626;
}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
/* Header */
.topbar{background:linear-gradient(135deg,var(--brand),var(--accent));color:#fff;padding:14px 24px;display:flex;align-items:center;justify-content:space-between}
.topbar h1{font-size:1.1rem;font-weight:700}
.topbar span{font-size:0.78rem;opacity:.8}
/* Tabs */
.tabs{display:flex;gap:0;background:#fff;border-bottom:2px solid var(--border);padding:0 24px;overflow-x:auto}
.tab{padding:12px 20px;font-size:0.85rem;font-weight:500;color:var(--text2);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px;white-space:nowrap;transition:color .15s,border-color .15s}
.tab.active{color:var(--brand);border-bottom-color:var(--brand);font-weight:700}
.tab:hover:not(.active){color:var(--text)}
/* Content */
.content{padding:24px;max-width:1200px;margin:0 auto}
.section{display:none}.section.active{display:block}
/* Stat cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}
.card{background:var(--surface);border:1.5px solid var(--border);border-radius:var(--radius);padding:16px}
.card-label{font-size:0.74rem;color:var(--text2);font-weight:500;text-transform:uppercase;letter-spacing:.04em}
.card-value{font-size:1.8rem;font-weight:800;color:var(--text);margin-top:4px;line-height:1}
.card-value.green{color:var(--green)}.card-value.amber{color:var(--amber)}.card-value.red{color:var(--red)}
/* Charts grid */
.charts{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}
.chart-card{background:var(--surface);border:1.5px solid var(--border);border-radius:var(--radius);padding:16px}
.chart-card h3{font-size:0.85rem;font-weight:700;margin-bottom:12px;color:var(--text2)}
.chart-wrap{position:relative;height:220px}
@media(max-width:700px){.charts{grid-template-columns:1fr}}
/* Tables */
.table-wrap{background:var(--surface);border:1.5px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:16px}
.table-wrap h3{padding:12px 16px;font-size:0.85rem;font-weight:700;border-bottom:1px solid var(--border);background:var(--brand-pale);color:var(--brand)}
table{width:100%;border-collapse:collapse;font-size:0.82rem}
th{padding:9px 12px;text-align:left;font-weight:600;color:var(--text2);border-bottom:1px solid var(--border);background:#FAFBFF;font-size:0.75rem;text-transform:uppercase;letter-spacing:.03em}
td{padding:9px 12px;border-bottom:1px solid #F3F4F6;word-break:break-word}
tr:last-child td{border-bottom:none}
tr:hover td{background:#FAFBFF}
/* Badges */
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.72rem;font-weight:600}
.badge-new{background:#FEE2E2;color:#991B1B}
.badge-assigned{background:#FEF3C7;color:#92400E}
.badge-in_progress{background:#DBEAFE;color:#1E40AF}
.badge-resolved{background:#D1FAE5;color:#065F46}
.badge-academic{background:#EDE9FE;color:#5B21B6}
.badge-technical{background:#DBEAFE;color:#1E40AF}
.badge-distress{background:#FEE2E2;color:#991B1B}
.badge-other{background:#F3F4F6;color:#374151}
.badge-urgent{background:#FEE2E2;color:#991B1B}
/* Buttons */
.btn{padding:6px 14px;border-radius:8px;font-size:0.8rem;font-weight:600;cursor:pointer;border:none;transition:background .13s}
.btn-primary{background:var(--brand);color:#fff}.btn-primary:hover{background:var(--accent)}
.btn-sm{padding:4px 10px;font-size:0.74rem}
/* Transcript */
.transcript{max-height:400px;overflow-y:auto;font-size:0.82rem;padding:12px}
.t-msg{padding:6px 0;border-bottom:1px solid #F3F4F6}
.t-role{font-weight:700;font-size:0.73rem;text-transform:uppercase;letter-spacing:.03em;margin-right:6px}
.t-user{color:var(--brand)}.t-assistant{color:var(--green)}
/* Student detail panel */
.student-panel{background:var(--surface);border:1.5px solid var(--border);border-radius:var(--radius);padding:16px;margin-top:16px}
.panel-title{font-size:0.9rem;font-weight:700;margin-bottom:12px;color:var(--text)}
/* Spinner */
.loading{text-align:center;padding:32px;color:var(--text2);font-size:0.85rem}
</style>
</head>
<body>

<div class="topbar">
  <h1>SGEG Education Coach — Dashboard</h1>
  <div style="display:flex;align-items:center;gap:16px">
    <span id="last-updated" style="font-size:.78rem;opacity:.8">Loading…</span>
    <a href="/dashboard/logout" style="font-size:.78rem;color:rgba(255,255,255,.8);text-decoration:none;border:1px solid rgba(255,255,255,.3);padding:4px 12px;border-radius:99px;transition:background .13s" onmouseover="this.style.background='rgba(255,255,255,.15)'" onmouseout="this.style.background=''">Sign out</a>
  </div>
</div>

<div class="tabs">
  <div class="tab active" onclick="showTab('analytics')">📊 Analytics</div>
  <div class="tab" onclick="showTab('tickets')">🎫 Tickets</div>
  <div class="tab" onclick="showTab('safety')">🛡️ Safety</div>
  <div class="tab" onclick="showTab('health')">⚡ Health</div>
  <div class="tab" onclick="showTab('students')">👤 Students</div>
</div>

<div class="content">

  <!-- ── ANALYTICS ───────────────────────────────────────────────────────── -->
  <div id="tab-analytics" class="section active">
    <div class="cards" id="overview-cards"><div class="loading">Loading…</div></div>
    <div class="charts">
      <div class="chart-card"><h3>Intent breakdown</h3><div class="chart-wrap"><canvas id="chart-intents"></canvas></div></div>
      <div class="chart-card"><h3>Messages per day (last 14 days)</h3><div class="chart-wrap"><canvas id="chart-volume"></canvas></div></div>
      <div class="chart-card"><h3>Usage by grade level</h3><div class="chart-wrap"><canvas id="chart-grades"></canvas></div></div>
      <div class="chart-card"><h3>Usage by curriculum stream</h3><div class="chart-wrap"><canvas id="chart-streams"></canvas></div></div>
    </div>
    <div class="table-wrap">
      <h3>Top student questions</h3>
      <table><thead><tr><th>#</th><th>Question</th><th>Count</th></tr></thead>
      <tbody id="top-questions-body"><tr><td colspan="3" class="loading">Loading…</td></tr></tbody></table>
    </div>
  </div>

  <!-- ── TICKETS ─────────────────────────────────────────────────────────── -->
  <div id="tab-tickets" class="section">
    <div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">
      <button class="btn btn-primary btn-sm" onclick="loadTickets()">All</button>
      <button class="btn btn-sm" style="background:#FEE2E2;color:#991B1B" onclick="loadTickets('new')">New</button>
      <button class="btn btn-sm" style="background:#DBEAFE;color:#1E40AF" onclick="loadTickets('in_progress')">In Progress</button>
      <button class="btn btn-sm" style="background:#D1FAE5;color:#065F46" onclick="loadTickets('resolved')">Resolved</button>
    </div>
    <div class="table-wrap">
      <h3>Ticket queue</h3>
      <table><thead><tr><th>ID</th><th>Student</th><th>Grade</th><th>Stream</th><th>Category</th><th>Urgency</th><th>Subject</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead>
      <tbody id="tickets-body"><tr><td colspan="10" class="loading">Loading…</td></tr></tbody></table>
    </div>
    <div id="transcript-panel" style="display:none" class="student-panel">
      <div class="panel-title" id="transcript-title">Transcript</div>
      <div class="transcript" id="transcript-body"></div>
    </div>
  </div>

  <!-- ── SAFETY ──────────────────────────────────────────────────────────── -->
  <div id="tab-safety" class="section">
    <div class="table-wrap">
      <h3>🚨 Flagged conversations</h3>
      <table><thead><tr><th>Student</th><th>Grade</th><th>Stream</th><th>Reason</th><th>Message</th><th>When</th></tr></thead>
      <tbody id="flags-body"><tr><td colspan="6" class="loading">Loading…</td></tr></tbody></table>
    </div>
    <div class="table-wrap">
      <h3>🔒 Security guardrail hits</h3>
      <table><thead><tr><th>When</th><th>Detail</th></tr></thead>
      <tbody id="guardrails-body"><tr><td colspan="2" class="loading">Loading…</td></tr></tbody></table>
    </div>
    <div class="table-wrap">
      <h3>🔍 Bot response review (recent 30)</h3>
      <table><thead><tr><th>Student</th><th>Grade</th><th>Intent</th><th>Response (preview)</th><th>Time (ms)</th><th>When</th></tr></thead>
      <tbody id="responses-body"><tr><td colspan="6" class="loading">Loading…</td></tr></tbody></table>
    </div>
  </div>

  <!-- ── HEALTH ──────────────────────────────────────────────────────────── -->
  <div id="tab-health" class="section">
    <div class="cards" id="health-cards"><div class="loading">Loading…</div></div>
    <div class="charts">
      <div class="chart-card"><h3>Response time by path (avg ms)</h3><div class="chart-wrap"><canvas id="chart-resp-times"></canvas></div></div>
      <div class="chart-card"><h3>Token spend — last 7 days</h3><div class="chart-wrap"><canvas id="chart-tokens"></canvas></div></div>
    </div>
  </div>

  <!-- ── STUDENTS ────────────────────────────────────────────────────────── -->
  <div id="tab-students" class="section">
    <div class="table-wrap">
      <h3>All students (most recent first)</h3>
      <table><thead><tr><th>Name</th><th>Grade</th><th>Stream</th><th>Messages</th><th>Last seen</th><th></th></tr></thead>
      <tbody id="students-body"><tr><td colspan="6" class="loading">Loading…</td></tr></tbody></table>
    </div>
    <div id="student-detail-panel" style="display:none" class="student-panel">
      <div class="panel-title" id="student-detail-title">Student detail</div>
      <div id="student-detail-body"></div>
    </div>
  </div>

</div><!-- /content -->

<script>
const BASE = '/dashboard/api';
let _charts = {};

// ── Tab switching ─────────────────────────────────────────────────────────
function showTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => {
    const names = ['analytics','tickets','safety','health','students'];
    t.classList.toggle('active', names[i] === name);
  });
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');

  if (name === 'analytics' && !_charts['intents']) loadAnalytics();
  if (name === 'tickets')   loadTickets();
  if (name === 'safety')    loadSafety();
  if (name === 'health' && !_charts['resp-times']) loadHealth();
  if (name === 'students')  loadStudents();
}

// ── Fetch helper ──────────────────────────────────────────────────────────
async function api(path) {
  const r = await fetch(BASE + path);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-ZA', {dateStyle:'short', timeStyle:'short'});
}
function gradeLbl(g) {
  if (g === null || g === undefined) return '?';
  if (g === 0) return 'R';
  return g;
}

// ── ANALYTICS ─────────────────────────────────────────────────────────────
async function loadAnalytics() {
  const [ov, intents, volume, grades, streams, topQ] = await Promise.all([
    api('/analytics/overview'),
    api('/analytics/intents'),
    api('/analytics/volume'),
    api('/analytics/grade-split'),
    api('/analytics/stream-split'),
    api('/analytics/top-questions'),
  ]);
  document.getElementById('last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString('en-ZA');

  // Stat cards
  document.getElementById('overview-cards').innerHTML = `
    <div class="card"><div class="card-label">Total messages</div><div class="card-value">${ov.total_messages.toLocaleString()}</div></div>
    <div class="card"><div class="card-label">Students</div><div class="card-value">${ov.total_students.toLocaleString()}</div></div>
    <div class="card"><div class="card-label">Open tickets</div><div class="card-value ${ov.open_tickets>0?'amber':''}">${ov.open_tickets}</div></div>
    <div class="card"><div class="card-label">Flagged</div><div class="card-value ${ov.flagged_messages>0?'red':''}">${ov.flagged_messages}</div></div>
    <div class="card"><div class="card-label">Avg response</div><div class="card-value">${ov.avg_response_ms}ms</div></div>
    <div class="card"><div class="card-label">Cache hit rate</div><div class="card-value green">${ov.cache_hit_rate}%</div></div>
  `;

  const COLORS = ['#4F46E5','#7C3AED','#059669','#D97706','#DC2626','#6B7280'];
  mkDoughnut('chart-intents', intents.map(r=>r.intent), intents.map(r=>r.count), COLORS);
  mkLine('chart-volume', volume.map(r=>r.day.slice(5)), volume.map(r=>r.count));
  mkBar('chart-grades', grades.map(r=>r.grade), grades.map(r=>r.count), '#4F46E5');
  mkBar('chart-streams', streams.map(r=>r.stream), streams.map(r=>r.count), '#7C3AED');

  document.getElementById('top-questions-body').innerHTML =
    topQ.map((r,i) => `<tr><td>${i+1}</td><td>${esc(r.message)}</td><td><b>${r.count}</b></td></tr>`).join('') ||
    '<tr><td colspan="3">No data yet</td></tr>';
}

// ── TICKETS ───────────────────────────────────────────────────────────────
async function loadTickets(status='') {
  const rows = await api('/tickets' + (status ? '?status='+status : ''));
  document.getElementById('tickets-body').innerHTML = rows.length ?
    rows.map(t => `
      <tr>
        <td><b>#${t.id}</b></td>
        <td>${esc(t.user_name)}</td>
        <td>${gradeLbl(t.grade_level)}</td>
        <td>${esc(t.stream||'—')}</td>
        <td><span class="badge badge-${t.category}">${t.category}</span></td>
        <td><span class="badge badge-${t.urgency}">${t.urgency}</span></td>
        <td>${esc(t.subject||t.description?.slice(0,60)||'—')}</td>
        <td><span class="badge badge-${t.status}">${t.status}</span></td>
        <td>${fmtDate(t.created_at)}</td>
        <td>
          <button class="btn btn-primary btn-sm" onclick="viewTranscript(${t.id}, '${esc(t.user_name)}')">Transcript</button>
          ${t.status !== 'resolved' ? `<button class="btn btn-sm" style="background:#D1FAE5;color:#065F46;margin-left:4px" onclick="resolveTicket(${t.id})">Resolve</button>` : ''}
        </td>
      </tr>`).join('') :
    '<tr><td colspan="10" style="text-align:center;padding:20px;color:#9CA3AF">No tickets</td></tr>';
}

async function viewTranscript(id, name) {
  const data = await api('/tickets/'+id+'/transcript');
  document.getElementById('transcript-title').textContent = 'Transcript — ' + name + ' (Ticket #' + id + ')';
  document.getElementById('transcript-body').innerHTML = data.transcript.map(m =>
    `<div class="t-msg"><span class="t-role t-${m.role}">${m.role}</span>${esc(m.content)}</div>`
  ).join('') || '<p style="color:#9CA3AF">No transcript stored.</p>';
  document.getElementById('transcript-panel').style.display = 'block';
  document.getElementById('transcript-panel').scrollIntoView({behavior:'smooth'});
}

async function resolveTicket(id) {
  await fetch(BASE+'/tickets/'+id+'?status=resolved', {method:'PATCH'});
  loadTickets();
}

// ── SAFETY ────────────────────────────────────────────────────────────────
async function loadSafety() {
  const [flags, guardrails, responses] = await Promise.all([
    api('/safety/flags'),
    api('/safety/guardrail-hits'),
    api('/safety/bot-responses'),
  ]);

  document.getElementById('flags-body').innerHTML = flags.length ?
    flags.map(f => `<tr>
      <td>${esc(f.user_id)}</td>
      <td>${gradeLbl(f.grade_level)}</td>
      <td>${esc(f.stream||'—')}</td>
      <td><span class="badge badge-distress">${esc(f.flag_reason)}</span></td>
      <td>${esc(f.content)}</td>
      <td>${fmtDate(f.created_at)}</td>
    </tr>`).join('') :
    '<tr><td colspan="6" style="padding:16px;color:#9CA3AF;text-align:center">No flagged messages ✅</td></tr>';

  document.getElementById('guardrails-body').innerHTML = guardrails.length ?
    guardrails.map(g => `<tr><td>${fmtDate(g.ts)}</td><td>${esc(g.detail)}</td></tr>`).join('') :
    '<tr><td colspan="2" style="padding:16px;color:#9CA3AF;text-align:center">No guardrail hits ✅</td></tr>';

  document.getElementById('responses-body').innerHTML = responses.length ?
    responses.map(r => `<tr>
      <td>${esc(r.user_id)}</td>
      <td>${gradeLbl(r.grade_level)}</td>
      <td>${esc(r.intent||'—')}</td>
      <td>${esc(r.content)}</td>
      <td>${r.response_time_ms||'—'}</td>
      <td>${fmtDate(r.created_at)}</td>
    </tr>`).join('') :
    '<tr><td colspan="6">No data</td></tr>';
}

// ── HEALTH ────────────────────────────────────────────────────────────────
async function loadHealth() {
  const h = await api('/health');

  document.getElementById('health-cards').innerHTML = `
    <div class="card"><div class="card-label">Cache hit rate</div><div class="card-value green">${h.cache_hit_rate}%</div></div>
    <div class="card"><div class="card-label">Content items</div><div class="card-value">${(h.canvas_index.items||0).toLocaleString()}</div></div>
    <div class="card"><div class="card-label">Last sync</div><div class="card-value" style="font-size:0.85rem">${h.canvas_index.sync_status}</div></div>
  `;

  mkBar('chart-resp-times',
    h.response_times.map(r=>r.path||'unknown'),
    h.response_times.map(r=>r.avg_ms),
    '#059669'
  );
  mkLine('chart-tokens',
    h.token_spend_7d.map(r=>r.day.slice(5)),
    h.token_spend_7d.map(r=>r.tokens),
    '#7C3AED'
  );
}

// ── STUDENTS ──────────────────────────────────────────────────────────────
async function loadStudents() {
  const rows = await api('/students');
  document.getElementById('students-body').innerHTML = rows.length ?
    rows.map(s => `<tr>
      <td>${esc(s.user_name)}</td>
      <td>${gradeLbl(s.grade_level)}</td>
      <td>${esc(s.stream||'—')}</td>
      <td>${s.messages}</td>
      <td>${fmtDate(s.last_seen)}</td>
      <td><button class="btn btn-primary btn-sm" onclick="viewStudent('${esc(s.user_id)}', '${esc(s.user_name)}')">View</button></td>
    </tr>`).join('') :
    '<tr><td colspan="6" style="padding:20px;text-align:center;color:#9CA3AF">No students yet</td></tr>';
}

async function viewStudent(userId, name) {
  const d = await api('/students/' + encodeURIComponent(userId));
  document.getElementById('student-detail-title').textContent = '👤 ' + name;
  const intents = d.intent_breakdown.map(r => `<span class="badge badge-other">${r.intent}: ${r.count}</span>`).join(' ');
  const msgs = d.messages.slice(0,20).map(m =>
    `<div class="t-msg"><span class="t-role t-${m.role}">${m.role}</span>${esc(m.content.slice(0,200))}
     <small style="color:#9CA3AF;margin-left:8px">${m.intent||''} • ${m.response_time_ms||'—'}ms</small></div>`
  ).join('');
  const tickets = d.tickets.map(t =>
    `<span class="badge badge-${t.status}">#${t.id} ${t.category} — ${t.status}</span>`
  ).join(' ');

  document.getElementById('student-detail-body').innerHTML = `
    <div style="margin-bottom:12px">${intents}</div>
    ${tickets ? `<div style="margin-bottom:12px">${tickets}</div>` : ''}
    <div class="transcript" style="max-height:300px">${msgs || '<p style="color:#9CA3AF">No messages yet.</p>'}</div>
  `;
  document.getElementById('student-detail-panel').style.display = 'block';
  document.getElementById('student-detail-panel').scrollIntoView({behavior:'smooth'});
}

// ── Chart helpers ─────────────────────────────────────────────────────────
function mkDoughnut(id, labels, data, colors) {
  if (_charts[id]) _charts[id].destroy();
  _charts[id] = new Chart(document.getElementById(id), {
    type: 'doughnut',
    data: { labels, datasets:[{data, backgroundColor:colors, borderWidth:2}] },
    options: { responsive:true, maintainAspectRatio:false, plugins:{legend:{position:'right'}} },
  });
}
function mkLine(id, labels, data, color='#4F46E5') {
  if (_charts[id]) _charts[id].destroy();
  _charts[id] = new Chart(document.getElementById(id), {
    type: 'line',
    data: { labels, datasets:[{data, borderColor:color, backgroundColor:color+'22', fill:true, tension:.4, pointRadius:3}] },
    options: { responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}} },
  });
}
function mkBar(id, labels, data, color='#4F46E5') {
  if (_charts[id]) _charts[id].destroy();
  _charts[id] = new Chart(document.getElementById(id), {
    type: 'bar',
    data: { labels, datasets:[{data, backgroundColor:color+'CC', borderRadius:6}] },
    options: { responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}} },
  });
}

// ── Boot ──────────────────────────────────────────────────────────────────
loadAnalytics();
</script>
</body>
</html>
"""
