"""Open tickets with the SGEG curriculum team on behalf of learners.

When the auto-replier decides a question needs human handling (content,
distress, off-subject, drift, etc.), instead of staying silent we:
  1. Send a Canvas Conversation to the SGEG curriculum team with the learner's
     question, context, urgency, and conversation link.
  2. Reply to the learner with a brief acknowledgement so they don't feel
     ignored.
  3. Record a Ticket row in the DB for /admin visibility and follow-up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from sgeg_nudge.canvas import CanvasClient
from sgeg_nudge.config import settings
from sgeg_nudge.db import Ticket, record_audit

log = logging.getLogger(__name__)

# Templated acknowledgements — never composed by Claude (less drift risk).
LEARNER_ACK_EN = (
    "Hi {name}, I've passed your question on to the SGEG curriculum team — "
    "they'll come back to you here soon. Hang tight."
)
LEARNER_ACK_AF = (
    "Hi {name}, ek het jou vraag aan die SGEG kurrikulum-span deurgegee — "
    "hulle sal binnekort hier vir jou terugkom. Bly net 'n bietjie."
)

# Reasons we open tickets — used for /admin filtering and ticket subject lines.
REASON_CONTENT = "content"            # academic content / definition / solution
REASON_OFF_SUBJECT = "off_subject"    # asked about a subject not in their enrolment
REASON_DISTRESS = "distress"          # safety / pastoral concern (urgent)
REASON_DRIFT = "drift"                # Claude's reply tripped a safety regex
REASON_OTHER = "other"

URGENT_REASONS = {REASON_DISTRESS}


@dataclass(frozen=True)
class TicketOutcome:
    ticket_id: int
    team_conv_id: str | None
    ack_sent: bool


def _subject_for(reason: str, learner_name: str) -> str:
    prefix = "[URGENT] " if reason in URGENT_REASONS else ""
    label = {
        REASON_CONTENT: "Content question",
        REASON_OFF_SUBJECT: "Off-subject query",
        REASON_DISTRESS: "Pastoral / distress check needed",
        REASON_DRIFT: "Auto-reply drift — please review",
        REASON_OTHER: "Learner question",
    }.get(reason, "Learner question")
    return f"{prefix}{label} from {learner_name}"


def _team_message(
    *,
    learner_name: str,
    learner_id: int,
    question: str,
    context_snippet: str | None,
    reason: str,
    conversation_id: str | None,
) -> str:
    lines = [
        f"A learner question needs the curriculum team's attention.",
        "",
        f"Learner: {learner_name} (Canvas id {learner_id})",
        f"Reason:  {reason}",
    ]
    if conversation_id:
        lines.append(f"Thread:  Canvas conversation #{conversation_id}")
    lines.append("")
    lines.append("Their message:")
    lines.append(f"  > {question.strip()}")
    if context_snippet and context_snippet.strip() != question.strip():
        lines.append("")
        lines.append("Recent context:")
        for chunk in context_snippet.strip().splitlines()[-5:]:
            lines.append(f"  {chunk}")
    lines.append("")
    lines.append(
        "The learner has been told you'll be in touch via the same Canvas "
        "conversation. Replying there will close the loop."
    )
    return "\n".join(lines)


def open_ticket(
    canvas: CanvasClient,
    session: Session,
    *,
    learner_id: int,
    learner_name: str,
    question: str,
    reason: str,
    conversation_id: str | None = None,
    context_snippet: str | None = None,
    language: str = "en",
    dry_run: bool = False,
) -> TicketOutcome:
    """File a ticket with the curriculum team, then ACK the learner.

    Returns a TicketOutcome capturing the DB row id and what got sent.
    Failures in the Canvas legs do not prevent the DB row being saved —
    we'd rather have a ticket record we can replay than swallow the data.
    """
    ticket = Ticket(
        learner_id=learner_id,
        learner_name=learner_name,
        conversation_id=str(conversation_id) if conversation_id else None,
        question=question,
        context_snippet=context_snippet,
        reason=reason,
        urgency="urgent" if reason in URGENT_REASONS else "normal",
        status="open",
    )
    session.add(ticket)
    session.flush()

    team_conv_id: str | None = None
    if not dry_run and settings.curriculum_team_canvas_user_id:
        try:
            body = _team_message(
                learner_name=learner_name,
                learner_id=learner_id,
                question=question,
                context_snippet=context_snippet,
                reason=reason,
                conversation_id=conversation_id,
            )
            resp = canvas.send_conversation(
                recipient_ids=[settings.curriculum_team_canvas_user_id],
                body=body,
                subject=_subject_for(reason, learner_name),
                force_new=True,
            )
            team_conv_id = str(resp.get("id")) if resp.get("id") else None
            ticket.curriculum_team_conv_id = team_conv_id
        except Exception as exc:
            log.exception("Failed to send ticket to curriculum team")
            record_audit(
                session, "ticket_send_to_team_failed",
                entity_type="ticket", entity_id=ticket.id, detail=repr(exc),
            )
    elif not settings.curriculum_team_canvas_user_id:
        log.warning(
            "Ticket %s opened but CURRICULUM_TEAM_CANVAS_USER_ID is not set — "
            "the curriculum team will not be notified. Set it in .env.",
            ticket.id,
        )

    ack_sent = False
    if not dry_run and conversation_id:
        ack_template = LEARNER_ACK_AF if language == "af" else LEARNER_ACK_EN
        ack_body = ack_template.format(name=learner_name)
        try:
            canvas.reply_to_conversation(conversation_id, ack_body)
            ack_sent = True
            ticket.learner_ack_sent = True
        except Exception as exc:
            log.exception("Failed to send learner ack for ticket %s", ticket.id)
            record_audit(
                session, "ticket_learner_ack_failed",
                entity_type="ticket", entity_id=ticket.id, detail=repr(exc),
            )

    record_audit(
        session, "ticket_opened",
        entity_type="ticket", entity_id=ticket.id,
        detail=(
            f"reason={reason} learner={learner_name}({learner_id}) "
            f"team_conv={team_conv_id} ack={ack_sent}"
        ),
    )
    return TicketOutcome(ticket_id=ticket.id, team_conv_id=team_conv_id, ack_sent=ack_sent)


def close_ticket(session: Session, ticket_id: int) -> Ticket | None:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        return None
    ticket.status = "closed"
    ticket.closed_at = datetime.now(timezone.utc)
    record_audit(
        session, "ticket_closed",
        entity_type="ticket", entity_id=ticket.id,
    )
    return ticket


# Default: auto-close anything still open after this many days.
STALE_TICKET_AGE_DAYS = 14


def close_stale_tickets(
    session: Session,
    *,
    age_days: int = STALE_TICKET_AGE_DAYS,
    now: datetime | None = None,
) -> int:
    """Auto-close still-open tickets older than `age_days`.

    Run from the daily job. Closed tickets gain an audit row with
    detail explaining the auto-close so the admin can distinguish from
    human-closed tickets.
    """
    from sqlalchemy import select  # local import to keep top imports tidy
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=age_days)).replace(tzinfo=None)

    stale = list(session.scalars(
        select(Ticket).where(
            Ticket.status == "open",
            Ticket.created_at < cutoff,
        )
    ).all())

    for ticket in stale:
        ticket.status = "closed"
        ticket.closed_at = now
        record_audit(
            session,
            "ticket_auto_closed",
            entity_type="ticket",
            entity_id=ticket.id,
            detail=f"open > {age_days} days with no resolution",
        )
    return len(stale)
