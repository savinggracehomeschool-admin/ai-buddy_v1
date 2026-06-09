"""Unit tests for the nudge deduplication rule.

The contract: a (learner_id, assignment_id, tier) is "already nudged" if any
row exists in DEDUP_STATUSES. Only STATUS_FAILED rows are retryable.
"""

from __future__ import annotations

from typing import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from sgeg_nudge.db import (
    AuditLog,
    Base,
    Nudge,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REQUIRES_REVIEW,
    STATUS_SENT,
    STATUS_SKIPPED,
    TIER_24H,
    TIER_72H,
    already_nudged,
    record_audit,
)


@pytest.fixture
def session() -> Iterator[Session]:
    """Fresh in-memory SQLite per test — no cross-test pollution."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(engine, expire_on_commit=False, future=True)
    with SessionLocal() as s:
        yield s


def _add_nudge(session: Session, **overrides) -> Nudge:
    defaults = dict(
        learner_id=42,
        learner_name="Test Learner",
        course_id=224,
        assignment_id=1001,
        tier=TIER_72H,
        status=STATUS_SENT,
        message_text="hi",
    )
    defaults.update(overrides)
    n = Nudge(**defaults)
    session.add(n)
    session.commit()
    return n


def test_empty_db_is_not_already_nudged(session: Session) -> None:
    assert not already_nudged(session, learner_id=42, assignment_id=1001, tier=TIER_72H)


@pytest.mark.parametrize(
    "blocking_status",
    [STATUS_SENT, STATUS_PENDING, STATUS_REQUIRES_REVIEW, STATUS_SKIPPED],
)
def test_dedup_blocks_resend(session: Session, blocking_status: str) -> None:
    _add_nudge(session, status=blocking_status)
    assert already_nudged(session, learner_id=42, assignment_id=1001, tier=TIER_72H)


def test_failed_status_does_not_block_retry(session: Session) -> None:
    """A delivery failure should be retryable on the next job run."""
    _add_nudge(session, status=STATUS_FAILED)
    assert not already_nudged(session, learner_id=42, assignment_id=1001, tier=TIER_72H)


def test_72h_and_24h_are_independent(session: Session) -> None:
    """Sending the 72h tier shouldn't silently swallow the 24h tier."""
    _add_nudge(session, tier=TIER_72H, status=STATUS_SENT)
    assert already_nudged(session, learner_id=42, assignment_id=1001, tier=TIER_72H)
    assert not already_nudged(session, learner_id=42, assignment_id=1001, tier=TIER_24H)


def test_different_assignment_independent(session: Session) -> None:
    _add_nudge(session, assignment_id=1001, status=STATUS_SENT)
    assert not already_nudged(session, learner_id=42, assignment_id=1002, tier=TIER_72H)


def test_different_learner_independent(session: Session) -> None:
    _add_nudge(session, learner_id=42, status=STATUS_SENT)
    assert not already_nudged(session, learner_id=43, assignment_id=1001, tier=TIER_72H)


def test_failed_then_sent_dedups(session: Session) -> None:
    """If a retry succeeded after a failure, the slot is now occupied."""
    _add_nudge(session, status=STATUS_FAILED)
    _add_nudge(session, status=STATUS_SENT)
    assert already_nudged(session, learner_id=42, assignment_id=1001, tier=TIER_72H)


def test_audit_log_writes(session: Session) -> None:
    record_audit(session, "daily_job_started", detail="dry run")
    record_audit(
        session,
        "nudge_sent",
        entity_type="nudge",
        entity_id=7,
        detail="conversation_id=abc",
    )
    session.commit()
    rows = session.query(AuditLog).order_by(AuditLog.id).all()
    assert [r.event for r in rows] == ["daily_job_started", "nudge_sent"]
    assert rows[1].entity_id == 7
