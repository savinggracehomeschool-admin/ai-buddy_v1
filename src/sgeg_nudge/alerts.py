"""Out-of-band admin alerts — sent to Dee via Canvas Conversations.

The only out-of-band channel available in the MVP (no email/SMS infra yet),
so we use the same Canvas API we use for nudges. The recipient is
ADMIN_CANVAS_USER_ID in the .env. If unset, alerts are logged but not sent.

Used by:
  - canvas client when consecutive 429s force a hard abort
  - scheduler when a job throws
  - the daily job when too many candidates fail in a row (future)
"""

from __future__ import annotations

import logging
from sgeg_nudge.config import settings

log = logging.getLogger(__name__)

ALERT_SUBJECT_PREFIX = "[SGEG Nudge Engine]"


def send_alert_to_admin(subject: str, body: str) -> bool:
    """Send an alert to the configured admin Canvas user.

    Returns True if Canvas accepted the conversation, False otherwise.
    Never raises — alerts are best-effort; we don't want alert failures
    to cascade into more failures.
    """
    if settings.admin_canvas_user_id is None:
        log.warning(
            "ALERT (no admin user configured) — %s: %s",
            subject, body[:500],
        )
        return False

    full_subject = f"{ALERT_SUBJECT_PREFIX} {subject}"
    log.error("ADMIN ALERT — %s", full_subject)

    # Import here so this module stays importable even if Canvas creds fail
    # (e.g. during early-startup logging configuration).
    try:
        from sgeg_nudge.canvas import CanvasClient
        with CanvasClient() as canvas:
            canvas.send_conversation(
                recipient_ids=[settings.admin_canvas_user_id],
                body=body,
                subject=full_subject,
            )
        return True
    except Exception as exc:
        log.exception("send_alert_to_admin failed: %r", exc)
        return False
