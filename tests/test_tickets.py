"""Tests for the curriculum-team ticket flow."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from sgeg_nudge.db import AuditLog, Base, Ticket
from sgeg_nudge.tickets import (
    REASON_CONTENT,
    REASON_DISTRESS,
    REASON_DRIFT,
    open_ticket,
)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(engine, expire_on_commit=False, future=True)
    with SessionLocal() as s:
        yield s


def _stub_settings(team_id: int | None) -> SimpleNamespace:
    return SimpleNamespace(curriculum_team_canvas_user_id=team_id)


def test_open_ticket_creates_db_row_and_audit(session: Session) -> None:
    canvas = MagicMock()
    canvas.send_conversation.return_value = {"id": 9001}
    canvas.reply_to_conversation.return_value = {"id": 9002}

    with patch("sgeg_nudge.tickets.settings", _stub_settings(12345)):
        outcome = open_ticket(
            canvas, session,
            learner_id=42, learner_name="Test Learner",
            question="What does blend mean?", reason=REASON_CONTENT,
            conversation_id="1234",
        )
    session.commit()

    row = session.get(Ticket, outcome.ticket_id)
    assert row is not None
    assert row.learner_id == 42
    assert row.question == "What does blend mean?"
    assert row.reason == REASON_CONTENT
    assert row.urgency == "normal"
    assert row.status == "open"
    assert row.curriculum_team_conv_id == "9001"
    assert row.learner_ack_sent is True

    audits = session.query(AuditLog).all()
    assert any(a.event == "ticket_opened" for a in audits)


def test_open_ticket_distress_is_urgent(session: Session) -> None:
    canvas = MagicMock()
    canvas.send_conversation.return_value = {"id": 1}
    canvas.reply_to_conversation.return_value = {"id": 2}

    with patch("sgeg_nudge.tickets.settings", _stub_settings(12345)):
        outcome = open_ticket(
            canvas, session,
            learner_id=42, learner_name="L",
            question="i want to die", reason=REASON_DISTRESS,
            conversation_id="100",
        )
    session.commit()

    row = session.get(Ticket, outcome.ticket_id)
    assert row.urgency == "urgent"

    subject_arg = canvas.send_conversation.call_args.kwargs["subject"]
    assert subject_arg.startswith("[URGENT]"), subject_arg


def test_open_ticket_works_without_team_recipient(session: Session) -> None:
    """If CURRICULUM_TEAM_CANVAS_USER_ID is unset, ticket still lands in DB."""
    canvas = MagicMock()

    with patch("sgeg_nudge.tickets.settings", _stub_settings(None)):
        outcome = open_ticket(
            canvas, session,
            learner_id=42, learner_name="L",
            question="q", reason=REASON_CONTENT,
            conversation_id="100",
        )
    session.commit()

    row = session.get(Ticket, outcome.ticket_id)
    assert row is not None
    assert row.curriculum_team_conv_id is None
    # send_conversation never called (no recipient)
    canvas.send_conversation.assert_not_called()


def test_open_ticket_dry_run_skips_canvas(session: Session) -> None:
    canvas = MagicMock()

    with patch("sgeg_nudge.tickets.settings", _stub_settings(12345)):
        outcome = open_ticket(
            canvas, session,
            learner_id=42, learner_name="L",
            question="q", reason=REASON_DRIFT,
            conversation_id="100",
            dry_run=True,
        )
    session.commit()

    row = session.get(Ticket, outcome.ticket_id)
    assert row is not None
    canvas.send_conversation.assert_not_called()
    canvas.reply_to_conversation.assert_not_called()


def test_close_stale_tickets_only_targets_old_open(session: Session) -> None:
    """Stale = open + created_at < cutoff. Recent open + already-closed are spared."""
    from datetime import datetime, timedelta
    from sgeg_nudge.db import Ticket
    from sgeg_nudge.tickets import REASON_CONTENT, close_stale_tickets

    now = datetime(2026, 5, 22, 10, 0)
    # 1 stale, 1 fresh, 1 already closed
    stale = Ticket(
        learner_id=1, learner_name="A", question="q", reason=REASON_CONTENT,
        status="open", created_at=now - timedelta(days=20),
    )
    fresh = Ticket(
        learner_id=2, learner_name="B", question="q", reason=REASON_CONTENT,
        status="open", created_at=now - timedelta(days=3),
    )
    already_closed = Ticket(
        learner_id=3, learner_name="C", question="q", reason=REASON_CONTENT,
        status="closed", created_at=now - timedelta(days=30),
        closed_at=now - timedelta(days=20),
    )
    session.add_all([stale, fresh, already_closed])
    session.commit()

    closed_count = close_stale_tickets(session, age_days=14, now=datetime(2026, 5, 22, 10, 0, tzinfo=__import__("datetime").timezone.utc))
    session.commit()

    assert closed_count == 1
    assert session.get(Ticket, stale.id).status == "closed"
    assert session.get(Ticket, fresh.id).status == "open"
    assert session.get(Ticket, already_closed.id).status == "closed"


def test_open_ticket_swallows_team_send_failure(session: Session) -> None:
    """Even if Canvas POST to the team fails, the DB row is preserved."""
    canvas = MagicMock()
    canvas.send_conversation.side_effect = RuntimeError("Canvas hiccup")
    canvas.reply_to_conversation.return_value = {"id": 7}

    with patch("sgeg_nudge.tickets.settings", _stub_settings(12345)):
        outcome = open_ticket(
            canvas, session,
            learner_id=42, learner_name="L",
            question="q", reason=REASON_CONTENT,
            conversation_id="100",
        )
    session.commit()

    row = session.get(Ticket, outcome.ticket_id)
    assert row is not None
    assert row.curriculum_team_conv_id is None
    # Learner ack still attempted
    assert row.learner_ack_sent is True
