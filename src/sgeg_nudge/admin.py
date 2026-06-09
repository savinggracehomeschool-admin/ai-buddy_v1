"""FastAPI admin view — single page, basic auth, configuration + nudges + audit.

Mounted by `sgeg_nudge.main`. Renders a self-contained HTML page (no external
JS/CSS deps) using SGEG brand colours: navy #1B2B5E, teal #0D9488, gold #F59E0B.

Three sections:
  1. Courses    — enable/disable nudges per course (opt-in, default OFF)
  2. Learners   — opt-out toggles for individual learners (default ON)
  3. Recent nudges + audit log
"""

from __future__ import annotations

import html
import secrets
from typing import Annotated

import jwt as _jwt
from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select

from sgeg_nudge.canvas import CanvasClient
from sgeg_nudge.config import settings
from sgeg_nudge.db import (
    AuditLog,
    CourseConfig,
    LearnerConfig,
    Nudge,
    Ticket,
    get_session,
    init_engine,
    upsert_course_config,
    upsert_learner_config,
)
from sgeg_nudge.tickets import close_ticket

router = APIRouter()

_COOKIE = "sgeg_admin"
_ALGO   = "HS256"


def _verify_admin(
    request: Request,
    sgeg_admin: str | None = Cookie(default=None),
) -> str:
    """Accept the same cookie set by /dashboard/login."""
    try:
        payload = _jwt.decode(sgeg_admin or "", settings.lti_secret_key, algorithms=[_ALGO])
        return payload["sub"]
    except Exception:
        raise HTTPException(status_code=303, headers={"Location": "/dashboard/login"})


_STATUS_COLOURS = {
    "sent": "#0D9488",
    "pending": "#F59E0B",
    "failed": "#DC2626",
    "requires_review": "#7C3AED",
    "skipped": "#6B7280",
}
_TIER_COLOURS = {
    "72h": "#1B2B5E",
    "24h": "#F59E0B",
    "missed": "#DC2626",
    "reinforce": "#0D9488",
}

_TICKET_REASON_COLOURS = {
    "content": "#1B2B5E",
    "off_subject": "#7C3AED",
    "distress": "#DC2626",
    "drift": "#F59E0B",
    "other": "#6B7280",
}

_TICKET_URGENCY_COLOURS = {
    "urgent": "#DC2626",
    "normal": "#6B7280",
}

_TICKET_STATUS_COLOURS = {
    "open": "#0D9488",
    "closed": "#6B7280",
}


def _badge(value: str, colour: str) -> str:
    return (
        f'<span style="background:{colour};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-size:11px;font-weight:600;">{html.escape(value)}</span>'
    )


def _truncate(text: str, n: int = 140) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _toggle_form(action: str, label: str, primary: bool) -> str:
    bg = "#0D9488" if primary else "#6B7280"
    return (
        f'<form method="post" action="{action}" style="display:inline">'
        f'<button type="submit" style="background:{bg};color:#fff;border:none;'
        f'padding:5px 12px;border-radius:4px;font-size:12px;font-weight:600;cursor:pointer;">'
        f"{html.escape(label)}</button></form>"
    )


def _fetch_canvas_courses() -> list[dict]:
    """Pull live courses so we can show them even before they have a CourseConfig row."""
    try:
        with CanvasClient() as canvas:
            return canvas.list_courses()
    except Exception:
        return []


def _conversation_link(canvas_conversation_id: str | None) -> str:
    if not canvas_conversation_id:
        return "—"
    url = f"{settings.canvas_base_url}/conversations/{canvas_conversation_id}"
    return f'<a href="{html.escape(url)}" target="_blank" style="color:#0D9488;">#{html.escape(canvas_conversation_id)}</a>'


def _learner_link(learner_id: int, learner_name: str) -> str:
    """Internal admin link to a learner's journey page."""
    return (
        f'<a href="/admin/learner/{learner_id}" style="color:#1B2B5E;text-decoration:none;'
        f'border-bottom:1px dotted #1B2B5E;">{html.escape(learner_name)}</a>'
        f' <span style="color:#9CA3AF;font-size:11px;">({learner_id})</span>'
    )


def _render(
    canvas_courses: list[dict],
    course_configs: dict[int, CourseConfig],
    learner_configs: list[LearnerConfig],
    tickets: list[Ticket],
    ticket_filter: str,
    nudges: list[Nudge],
    audit: list[AuditLog],
    admin_user: str,
) -> str:
    # Merge Canvas-known courses with DB course_configs, then add any DB-only orphans.
    course_rows = []
    seen_ids: set[int] = set()
    for c in canvas_courses:
        cid = c["id"]
        seen_ids.add(cid)
        cfg = course_configs.get(cid)
        enabled = bool(cfg and cfg.enabled)
        action = "disable" if enabled else "enable"
        label = "Disable" if enabled else "Enable"
        course_rows.append(
            "<tr>"
            f"<td>{cid}</td>"
            f"<td>{html.escape(c.get('name') or '')}</td>"
            f"<td>{_badge('on', '#0D9488') if enabled else _badge('off', '#6B7280')}</td>"
            f"<td>{_toggle_form(f'/admin/course/{cid}/{action}', label, not enabled)}</td>"
            "</tr>"
        )
    for cid, cfg in course_configs.items():
        if cid in seen_ids:
            continue
        enabled = cfg.enabled
        action = "disable" if enabled else "enable"
        label = "Disable" if enabled else "Enable"
        course_rows.append(
            "<tr>"
            f"<td>{cid}</td>"
            f"<td>{html.escape(cfg.name or '(not in Canvas list)')}</td>"
            f"<td>{_badge('on', '#0D9488') if enabled else _badge('off', '#6B7280')}</td>"
            f"<td>{_toggle_form(f'/admin/course/{cid}/{action}', label, not enabled)}</td>"
            "</tr>"
        )

    learner_rows = []
    for l in learner_configs:
        enabled = l.enabled
        action = "disable" if enabled else "enable"
        label = "Re-enable" if not enabled else "Opt out"
        learner_rows.append(
            "<tr>"
            f"<td>{l.learner_id}</td>"
            f"<td>{html.escape(l.name or '')}</td>"
            f"<td>{_badge('on', '#0D9488') if enabled else _badge('opted-out', '#DC2626')}</td>"
            f"<td>{html.escape(l.notes or '')}</td>"
            f"<td>{_toggle_form(f'/admin/learner/{l.learner_id}/{action}', label, not enabled)}</td>"
            "</tr>"
        )

    ticket_rows = []
    for t in tickets:
        reason_badge = _badge(t.reason, _TICKET_REASON_COLOURS.get(t.reason, "#374151"))
        urgency_badge = _badge(t.urgency, _TICKET_URGENCY_COLOURS.get(t.urgency, "#374151"))
        status_badge = _badge(t.status, _TICKET_STATUS_COLOURS.get(t.status, "#374151"))
        created = t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "—"
        learner_link = _conversation_link(t.conversation_id)
        team_link = _conversation_link(t.curriculum_team_conv_id)
        question = html.escape(_truncate(t.question or "", 200))
        actions = ""
        if t.status == "open":
            actions = _toggle_form(f"/admin/ticket/{t.id}/close", "Close", True)
        ticket_rows.append(
            "<tr>"
            f"<td>{t.id}</td>"
            f"<td>{created}</td>"
            f"<td>{_learner_link(t.learner_id, t.learner_name)}</td>"
            f"<td>{reason_badge}</td>"
            f"<td>{urgency_badge}</td>"
            f"<td>{status_badge}</td>"
            f"<td class='msg'>{question}</td>"
            f"<td>{learner_link}</td>"
            f"<td>{team_link}</td>"
            f"<td>{actions}</td>"
            "</tr>"
        )

    def _filter_link(label: str, value: str) -> str:
        active = "background:#1B2B5E;color:#fff;" if ticket_filter == value else "background:#F3F4F6;color:#374151;"
        return (
            f'<a href="/admin?tickets={value}" '
            f'style="text-decoration:none;{active}padding:4px 12px;border-radius:4px;'
            f'font-size:12px;font-weight:600;margin-right:6px;">{html.escape(label)}</a>'
        )

    ticket_filter_bar = (
        '<div style="margin:4px 0 10px;">'
        + _filter_link("Open", "open")
        + _filter_link("Closed", "closed")
        + _filter_link("All", "all")
        + "</div>"
    )

    nudge_rows = []
    for n in nudges:
        status_badge = _badge(n.status, _STATUS_COLOURS.get(n.status, "#374151"))
        tier_badge = _badge(n.tier, _TIER_COLOURS.get(n.tier, "#374151"))
        composed = n.composed_at.strftime("%Y-%m-%d %H:%M") if n.composed_at else "—"
        sent = n.sent_at.strftime("%Y-%m-%d %H:%M") if n.sent_at else "—"
        conv = n.canvas_conversation_id or "—"
        msg = html.escape(_truncate(n.message_text or "", 200))
        nudge_rows.append(
            "<tr>"
            f"<td>{n.id}</td><td>{composed}</td>"
            f"<td>{_learner_link(n.learner_id, n.learner_name)}</td>"
            f"<td>{n.course_id} / {n.assignment_id}</td>"
            f"<td>{tier_badge}</td><td>{status_badge}</td><td>{sent}</td>"
            f"<td>{html.escape(str(conv))}</td><td class='msg'>{msg}</td>"
            "</tr>"
        )

    audit_rows = []
    for a in audit:
        ts = a.ts.strftime("%Y-%m-%d %H:%M:%S") if a.ts else "—"
        ent = f"{a.entity_type or ''}#{a.entity_id}" if a.entity_id else (a.entity_type or "—")
        detail = html.escape(_truncate(a.detail or "", 200))
        audit_rows.append(
            "<tr>"
            f"<td>{a.id}</td><td>{ts}</td>"
            f"<td>{html.escape(a.event)}</td>"
            f"<td>{html.escape(ent)}</td><td class='msg'>{detail}</td>"
            "</tr>"
        )

    opt_out_form = (
        '<form method="post" action="/admin/learner/optout" '
        'style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">'
        '<input type="number" name="learner_id" placeholder="Canvas user id" required '
        'style="padding:6px 10px;border:1px solid #E5E7EB;border-radius:4px;font-size:13px;">'
        '<input type="text" name="name" placeholder="Name (optional)" '
        'style="padding:6px 10px;border:1px solid #E5E7EB;border-radius:4px;font-size:13px;">'
        '<input type="text" name="notes" placeholder="Reason (optional)" '
        'style="padding:6px 10px;border:1px solid #E5E7EB;border-radius:4px;font-size:13px;flex:1;min-width:200px;">'
        '<button type="submit" style="background:#DC2626;color:#fff;border:none;'
        'padding:6px 14px;border-radius:4px;font-size:12px;font-weight:600;cursor:pointer;">Opt out learner</button>'
        '</form>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SGEG Assistant — Admin</title>
<style>
  :root {{ --navy:#1B2B5E; --teal:#0D9488; --gold:#F59E0B; --ink:#111827;
           --muted:#6B7280; --bg:#F9FAFB; --border:#E5E7EB; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
          background:var(--bg); color:var(--ink); margin:0; padding:0; }}
  header {{ background:var(--navy); color:#fff; padding:18px 28px;
            display:flex; align-items:center; justify-content:space-between; }}
  header h1 {{ margin:0; font-size:20px; letter-spacing:0.2px; }}
  header .who {{ font-size:13px; opacity:0.85; }}
  main {{ max-width:1400px; margin:0 auto; padding:24px 28px 60px; }}
  h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:0.08em;
        color:var(--navy); margin:28px 0 10px;
        border-bottom:2px solid var(--teal); padding-bottom:6px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff;
            border:1px solid var(--border); border-radius:6px; overflow:hidden; font-size:13px; }}
  th, td {{ text-align:left; padding:9px 12px; vertical-align:top; }}
  thead {{ background:#F3F4F6; }}
  th {{ font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); }}
  tbody tr {{ border-top:1px solid var(--border); }}
  tbody tr:hover {{ background:#FAFAFA; }}
  td.msg {{ color:var(--muted); max-width:480px; }}
  .empty {{ padding:24px; color:var(--muted); text-align:center; }}
  .pill {{ display:inline-block; background:var(--gold); color:#fff;
            padding:3px 10px; border-radius:12px; font-size:11px; font-weight:600; }}
  .note {{ color:var(--muted); font-size:12px; margin:6px 0 12px; }}
</style>
</head>
<body>
<header>
  <h1>SGEG Assistant <span style="color:var(--gold); font-weight:400;"> · Admin</span></h1>
  <div class="who">signed in as <strong>{html.escape(admin_user)}</strong></div>
</header>
<main>

  <h2>Courses <span class="pill">{len(course_rows)}</span></h2>
  <div class="note">Courses are <strong>opt-in</strong>. The daily job only nudges learners in courses you have explicitly enabled.</div>
  {('<table><thead><tr><th>id</th><th>name</th><th>status</th><th></th></tr></thead><tbody>'
    + ''.join(course_rows) + '</tbody></table>') if course_rows else '<div class="empty">No courses visible to SGEG Assistant.</div>'}

  <h2>Learner opt-outs <span class="pill">{len(learner_rows)}</span></h2>
  <div class="note">Learners are <strong>opted-in by default</strong>. Add specific Canvas user IDs below to exclude them from all nudges.</div>
  {('<table><thead><tr><th>id</th><th>name</th><th>status</th><th>notes</th><th></th></tr></thead><tbody>'
    + ''.join(learner_rows) + '</tbody></table>') if learner_rows else '<div class="empty">No learner opt-outs.</div>'}
  {opt_out_form}

  <h2>Tickets <span class="pill">{len(ticket_rows)}</span></h2>
  <div class="note">Opened automatically when the auto-replier escalates a learner question to the curriculum team. Click a conversation id to open the thread in Canvas.</div>
  {ticket_filter_bar}
  {('<table><thead><tr>'
    '<th>id</th><th>created</th><th>learner</th><th>reason</th><th>urgency</th>'
    '<th>status</th><th>question</th><th>learner thread</th><th>team thread</th><th></th>'
    '</tr></thead><tbody>' + ''.join(ticket_rows) + '</tbody></table>') if ticket_rows else '<div class="empty">No tickets in this filter.</div>'}

  <h2>Recent nudges <span class="pill">{len(nudges)}</span></h2>
  {('<table><thead><tr>'
    '<th>id</th><th>composed</th><th>learner</th><th>course / assignment</th>'
    '<th>tier</th><th>status</th><th>sent</th><th>conv</th><th>message</th>'
    '</tr></thead><tbody>' + ''.join(nudge_rows) + '</tbody></table>') if nudge_rows else '<div class="empty">No nudges yet.</div>'}

  <h2>Recent audit events <span class="pill">{len(audit)}</span></h2>
  {('<table><thead><tr>'
    '<th>id</th><th>ts</th><th>event</th><th>entity</th><th>detail</th>'
    '</tr></thead><tbody>' + ''.join(audit_rows) + '</tbody></table>') if audit_rows else '<div class="empty">No audit events yet.</div>'}
</main>
</body>
</html>"""


@router.get("/admin", response_class=HTMLResponse)
def admin_index(
    admin_user: str = Depends(_verify_admin),
    tickets: str = Query("open", pattern="^(open|closed|all)$"),
) -> HTMLResponse:
    init_engine()
    canvas_courses = _fetch_canvas_courses()
    with get_session() as session:
        course_cfgs = {
            c.course_id: c
            for c in session.scalars(select(CourseConfig)).all()
        }
        learner_cfgs = session.scalars(
            select(LearnerConfig).order_by(LearnerConfig.learner_id)
        ).all()
        ticket_q = select(Ticket).order_by(Ticket.id.desc()).limit(200)
        if tickets in ("open", "closed"):
            ticket_q = ticket_q.where(Ticket.status == tickets)
        ticket_rows = session.scalars(ticket_q).all()
        nudges = session.scalars(
            select(Nudge).order_by(Nudge.id.desc()).limit(100)
        ).all()
        audit = session.scalars(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(100)
        ).all()
    return HTMLResponse(
        _render(
            canvas_courses, course_cfgs, list(learner_cfgs),
            list(ticket_rows), tickets,
            list(nudges), list(audit), admin_user,
        )
    )


# --- toggle handlers --------------------------------------------------------

def _course_name_by_id(course_id: int, canvas_courses: list[dict]) -> str | None:
    for c in canvas_courses:
        if c.get("id") == course_id:
            return c.get("name")
    return None


@router.post("/admin/course/{course_id}/{action}")
def admin_toggle_course(
    course_id: int,
    action: str,
    _admin_user: str = Depends(_verify_admin),
) -> RedirectResponse:
    if action not in {"enable", "disable"}:
        raise HTTPException(status_code=400, detail="action must be 'enable' or 'disable'")
    init_engine()
    canvas_courses = _fetch_canvas_courses()
    name = _course_name_by_id(course_id, canvas_courses)
    with get_session() as session:
        upsert_course_config(
            session, course_id, enabled=(action == "enable"), name=name,
        )
        session.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/learner/{learner_id}/{action}")
def admin_toggle_learner(
    learner_id: int,
    action: str,
    _admin_user: str = Depends(_verify_admin),
) -> RedirectResponse:
    if action not in {"enable", "disable"}:
        raise HTTPException(status_code=400, detail="action must be 'enable' or 'disable'")
    init_engine()
    with get_session() as session:
        upsert_learner_config(session, learner_id, enabled=(action == "enable"))
        session.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/learner/optout")
def admin_optout_learner(
    _admin_user: str = Depends(_verify_admin),
    learner_id: int = Form(...),
    name: str = Form(""),
    notes: str = Form(""),
) -> RedirectResponse:
    init_engine()
    with get_session() as session:
        upsert_learner_config(
            session,
            learner_id,
            enabled=False,
            name=name or None,
            notes=notes or None,
        )
        session.commit()
    return RedirectResponse(url="/admin", status_code=303)


def _render_learner_page(
    learner_id: int,
    canvas_user: dict | None,
    enrolled_courses: list[dict],
    missing_submissions: list[dict],
    nudges: list[Nudge],
    tickets: list[Ticket],
    inbox_events: list[AuditLog],
    learner_cfg: LearnerConfig | None,
    admin_user: str,
) -> str:
    name = (canvas_user or {}).get("name") or (learner_cfg.name if learner_cfg else f"Learner {learner_id}")
    locale = (canvas_user or {}).get("locale") or (canvas_user or {}).get("effective_locale") or "—"
    is_opted_out = learner_cfg is not None and not learner_cfg.enabled

    # ----- stat cards -----
    nudges_sent = sum(1 for n in nudges if n.status == "sent")
    nudges_failed = sum(1 for n in nudges if n.status == "failed")
    tickets_open = sum(1 for t in tickets if t.status == "open")

    def _stat_card(label: str, value: str, accent: str = "#1B2B5E") -> str:
        return (
            f'<div style="background:#fff;border:1px solid #E5E7EB;border-radius:6px;'
            f'padding:14px 18px;min-width:140px;">'
            f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:0.06em;color:#6B7280;">{label}</div>'
            f'<div style="font-size:24px;font-weight:600;color:{accent};margin-top:4px;">{value}</div>'
            f'</div>'
        )

    stat_cards = (
        '<div style="display:flex;gap:12px;flex-wrap:wrap;margin:6px 0 16px;">'
        + _stat_card("Enrolled courses", str(len(enrolled_courses)))
        + _stat_card("Missing submissions", str(len(missing_submissions)), "#DC2626" if missing_submissions else "#0D9488")
        + _stat_card("Nudges sent", str(nudges_sent), "#0D9488")
        + _stat_card("Nudges failed", str(nudges_failed), "#DC2626" if nudges_failed else "#6B7280")
        + _stat_card("Open tickets", str(tickets_open), "#F59E0B" if tickets_open else "#6B7280")
        + "</div>"
    )

    # ----- enrolled courses -----
    course_rows = [
        f"<tr><td>{c.get('id')}</td><td>{html.escape(c.get('name') or '')}</td></tr>"
        for c in enrolled_courses
    ]
    courses_table = (
        '<table><thead><tr><th>id</th><th>name</th></tr></thead><tbody>'
        + ''.join(course_rows) + '</tbody></table>'
        if course_rows else '<div class="empty">No active course enrolments visible.</div>'
    )

    # ----- missing submissions -----
    missing_rows = []
    for m in missing_submissions[:30]:
        course_name = (m.get("course") or {}).get("name") or m.get("course_id", "")
        due_at = m.get("due_at") or "—"
        missing_rows.append(
            "<tr>"
            f"<td>{html.escape(str(course_name))}</td>"
            f"<td>{html.escape(m.get('name') or '')}</td>"
            f"<td>{html.escape(str(due_at))}</td>"
            "</tr>"
        )
    missing_table = (
        '<table><thead><tr><th>course</th><th>assignment</th><th>due</th></tr></thead><tbody>'
        + ''.join(missing_rows) + '</tbody></table>'
        if missing_rows else '<div class="empty">Nothing missing right now.</div>'
    )

    # ----- nudge history -----
    nudge_rows = []
    for n in nudges:
        status_badge = _badge(n.status, _STATUS_COLOURS.get(n.status, "#374151"))
        tier_badge = _badge(n.tier, _TIER_COLOURS.get(n.tier, "#374151"))
        when = n.composed_at.strftime("%Y-%m-%d %H:%M") if n.composed_at else "—"
        msg = html.escape(_truncate(n.message_text or "", 160))
        nudge_rows.append(
            "<tr>"
            f"<td>{n.id}</td><td>{when}</td>"
            f"<td>{tier_badge}</td><td>{status_badge}</td>"
            f"<td class='msg'>{msg}</td>"
            "</tr>"
        )
    nudge_table = (
        '<table><thead><tr><th>id</th><th>when</th><th>tier</th><th>status</th><th>message</th></tr></thead><tbody>'
        + ''.join(nudge_rows) + '</tbody></table>'
        if nudge_rows else '<div class="empty">No nudges sent yet.</div>'
    )

    # ----- ticket history -----
    ticket_rows = []
    for t in tickets:
        reason_badge = _badge(t.reason, _TICKET_REASON_COLOURS.get(t.reason, "#374151"))
        status_badge = _badge(t.status, _TICKET_STATUS_COLOURS.get(t.status, "#374151"))
        when = t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "—"
        q = html.escape(_truncate(t.question or "", 160))
        ticket_rows.append(
            "<tr>"
            f"<td>{t.id}</td><td>{when}</td>"
            f"<td>{reason_badge}</td><td>{status_badge}</td>"
            f"<td class='msg'>{q}</td>"
            f"<td>{_conversation_link(t.conversation_id)}</td>"
            "</tr>"
        )
    ticket_table = (
        '<table><thead><tr><th>id</th><th>when</th><th>reason</th><th>status</th><th>question</th><th>thread</th></tr></thead><tbody>'
        + ''.join(ticket_rows) + '</tbody></table>'
        if ticket_rows else '<div class="empty">No tickets opened.</div>'
    )

    # ----- inbox activity -----
    activity_rows = []
    for a in inbox_events:
        when = a.ts.strftime("%Y-%m-%d %H:%M:%S") if a.ts else "—"
        detail = html.escape(_truncate(a.detail or "", 160))
        activity_rows.append(
            "<tr>"
            f"<td>{when}</td>"
            f"<td>{html.escape(a.event)}</td>"
            f"<td class='msg'>{detail}</td>"
            "</tr>"
        )
    activity_table = (
        '<table><thead><tr><th>when</th><th>event</th><th>detail</th></tr></thead><tbody>'
        + ''.join(activity_rows) + '</tbody></table>'
        if activity_rows else '<div class="empty">No recorded inbox activity for this learner.</div>'
    )

    opted_out_badge = (
        f' {_badge("opted-out", "#DC2626")}' if is_opted_out else ''
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SGEG Assistant — {html.escape(name)}</title>
<style>
  :root {{ --navy:#1B2B5E; --teal:#0D9488; --gold:#F59E0B; --ink:#111827;
           --muted:#6B7280; --bg:#F9FAFB; --border:#E5E7EB; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
          background:var(--bg); color:var(--ink); margin:0; padding:0; }}
  header {{ background:var(--navy); color:#fff; padding:18px 28px;
            display:flex; align-items:center; justify-content:space-between; }}
  header h1 {{ margin:0; font-size:20px; letter-spacing:0.2px; }}
  header .who {{ font-size:13px; opacity:0.85; }}
  header a {{ color:#F59E0B; text-decoration:none; font-size:12px; }}
  main {{ max-width:1400px; margin:0 auto; padding:24px 28px 60px; }}
  h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:0.08em;
        color:var(--navy); margin:28px 0 10px;
        border-bottom:2px solid var(--teal); padding-bottom:6px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff;
            border:1px solid var(--border); border-radius:6px; overflow:hidden; font-size:13px; }}
  th, td {{ text-align:left; padding:9px 12px; vertical-align:top; }}
  thead {{ background:#F3F4F6; }}
  th {{ font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); }}
  tbody tr {{ border-top:1px solid var(--border); }}
  tbody tr:hover {{ background:#FAFAFA; }}
  td.msg {{ color:var(--muted); max-width:480px; }}
  .empty {{ padding:24px; color:var(--muted); text-align:center;
             background:#fff; border:1px solid var(--border); border-radius:6px; }}
  .meta {{ color:var(--muted); font-size:13px; margin:0 0 16px; }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(name)} <span style="color:var(--gold);font-weight:400;"> · learner journey</span>{opted_out_badge}</h1>
  <div class="who"><a href="/admin">← back to admin</a> · signed in as {html.escape(admin_user)}</div>
</header>
<main>
  <p class="meta">Canvas user id: <strong>{learner_id}</strong> · locale: <strong>{html.escape(str(locale))}</strong></p>
  {stat_cards}

  <h2>Enrolled courses</h2>
  {courses_table}

  <h2>Missing submissions</h2>
  {missing_table}

  <h2>Nudge history</h2>
  {nudge_table}

  <h2>Tickets</h2>
  {ticket_table}

  <h2>Recent inbox activity</h2>
  {activity_table}
</main>
</body>
</html>"""


@router.get("/admin/learner/{learner_id}", response_class=HTMLResponse)
def admin_learner(
    learner_id: int,
    admin_user: str = Depends(_verify_admin),
) -> HTMLResponse:
    init_engine()
    canvas_user: dict | None = None
    enrolled_courses: list[dict] = []
    missing: list[dict] = []
    try:
        with CanvasClient() as canvas:
            canvas_user = canvas.lookup_user(learner_id)
            enrolled_courses = canvas.list_courses_for_user(learner_id)
            missing = canvas.list_missing_submissions(learner_id)
    except Exception:
        # Canvas may not know this id (e.g. test data); we still render with DB data.
        pass

    with get_session() as session:
        nudges = list(session.scalars(
            select(Nudge).where(Nudge.learner_id == learner_id).order_by(Nudge.id.desc()).limit(50)
        ).all())
        tickets = list(session.scalars(
            select(Ticket).where(Ticket.learner_id == learner_id).order_by(Ticket.id.desc()).limit(30)
        ).all())
        # Surface inbox events tied to this learner's conversations.
        learner_conv_ids = {t.conversation_id for t in tickets if t.conversation_id}
        activity_q = select(AuditLog).where(AuditLog.event.like("%inbox%") | AuditLog.event.like("reply_%"))
        if learner_conv_ids:
            activity_q = activity_q.where(AuditLog.entity_id.in_(int(c) for c in learner_conv_ids if c.isdigit()))
        inbox_events = list(session.scalars(
            activity_q.order_by(AuditLog.id.desc()).limit(40)
        ).all())
        learner_cfg = session.get(LearnerConfig, learner_id)

    return HTMLResponse(_render_learner_page(
        learner_id, canvas_user, enrolled_courses, missing,
        nudges, tickets, inbox_events, learner_cfg, admin_user,
    ))


@router.post("/admin/ticket/{ticket_id}/close")
def admin_close_ticket(
    ticket_id: int,
    _admin_user: str = Depends(_verify_admin),
) -> RedirectResponse:
    init_engine()
    with get_session() as session:
        ticket = close_ticket(session, ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail="ticket not found")
        session.commit()
    return RedirectResponse(url="/admin", status_code=303)


