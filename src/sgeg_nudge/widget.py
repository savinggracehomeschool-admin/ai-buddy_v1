"""Widget launch endpoint — used by the Canvas custom-JS badge.

When the floating badge opens its iframe it calls:
  GET /widget/launch?user_id=<canvas_numeric_id>&course_id=<optional>&user_name=<name>

Security model
──────────────
Canvas injects window.ENV.current_user.id into every page it serves.
The badge JS reads that value and passes it here.  We verify the request
came from the Canvas domain by checking the Origin / Referer header.
This is appropriate for a single-school deployment — the Canvas admin
controls which JS runs on the domain, so a request from that domain is
equivalent to a trusted source.

For multi-tenant deployments add an HMAC token generated server-side.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import settings
from .db import LTISession, get_session

log = logging.getLogger(__name__)
router = APIRouter(prefix="/widget", tags=["widget"])

_CANVAS_DOMAINS = (
    "savinggraceeducationgroup.beta.instructure.com",   # beta — active testing env
    "savinggraceeducationgroup.instructure.com",        # production
)


def _trusted_origin(request: Request) -> bool:
    """Return True if the request came from our Canvas instance."""
    for header in ("referer", "origin"):
        val = request.headers.get(header, "")
        if any(d in val for d in _CANVAS_DOMAINS):
            return True
    # Allow in dev mode (no Referer when testing locally)
    return settings.lti_dev_mode


@router.get("/launch")
def widget_launch(
    request: Request,
    user_id: str = Query(...),
    user_name: str = Query(default="Student"),
    course_id: str = Query(default=""),
    course_name: str = Query(default=""),
) -> RedirectResponse:
    """Create a session from Canvas ENV data and redirect to the chat UI.

    Called by the floating badge iframe on first open.
    """
    if not _trusted_origin(request):
        log.warning("widget_launch blocked — untrusted origin: %s", request.headers.get("referer"))
        raise HTTPException(status_code=403, detail="Requests must originate from Canvas.")

    from .lti import _fetch_enrollment_scope, _extract_grade_level
    from .db import ACCOUNT_STREAMS

    # Fetch enrollment list using the numeric Canvas user ID
    course_ids, account_ids, launch_acct = _fetch_enrollment_scope(user_id)

    # If a course_id was supplied but not in enrollment list, default to first enrolled
    effective_course = (
        course_id if course_id and course_id in course_ids
        else (course_ids[0] if course_ids else course_id or None)
    )

    grade_level = _extract_grade_level(course_name or "")
    ttl = 2 if (grade_level is not None and grade_level <= 3) else 8
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session_id = str(uuid.uuid4())

    with get_session() as db:
        db.add(LTISession(
            session_id=session_id,
            user_id=user_id,
            course_id=effective_course,
            user_name=user_name,
            roles="http://purl.imsglobal.org/vocab/lis/v2/membership#Learner",
            platform_id=settings.canvas_base_url,
            enrolled_course_ids=json.dumps(course_ids) if course_ids else None,
            enrolled_account_ids=json.dumps(account_ids) if account_ids else None,
            launch_account_id=launch_acct,
            created_at=now,
            expires_at=now + timedelta(hours=ttl),
        ))
        db.commit()

    log.info("widget_launch: user=%s session=%s courses=%s", user_id, session_id, course_ids)
    return RedirectResponse(f"/chat?session={session_id}&panel=1", status_code=302)


@router.get("/frame", response_class=HTMLResponse)
def widget_frame(
    user_id: str = Query(...),
    user_name: str = Query(default="Student"),
    course_id: str = Query(default=""),
) -> HTMLResponse:
    """Thin HTML wrapper that auto-redirects to /widget/launch.

    The badge iframe loads this URL so it gets the right session even if
    the user navigates Canvas (the iframe src stays stable).
    """
    params = f"user_id={user_id}&user_name={user_name}&course_id={course_id}"
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"/>
<meta http-equiv="refresh" content="0;url=/widget/launch?{params}"/>
<style>body{{margin:0;background:#EEF2FF;display:flex;align-items:center;
justify-content:center;height:100vh;font-family:sans-serif;color:#4F46E5}}</style>
</head><body>
<p>Loading AI Buddy…</p>
<script>window.location="/widget/launch?{params}";</script>
</body></html>""")
