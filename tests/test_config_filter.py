"""Tests for the per-course / per-learner config filter (Day 8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from sgeg_nudge.db import (
    Base,
    TIER_72H,
    is_course_enabled,
    is_learner_enabled,
    upsert_course_config,
    upsert_learner_config,
)
from sgeg_nudge.nudge import find_72h_candidates


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(engine, expire_on_commit=False, future=True)
    with SessionLocal() as s:
        yield s


def _iso(now: datetime, hours: int) -> str:
    return (now + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _fake_canvas(course_id: int = 1, learner_id: int = 7) -> MagicMock:
    now = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    canvas = MagicMock()
    canvas.list_courses.return_value = [{"id": course_id, "name": "Test"}]
    canvas.list_upcoming_assignments.return_value = [
        {"id": 101, "name": "X", "due_at": _iso(now, 48)},
    ]
    canvas.list_submissions.return_value = [
        {"user_id": learner_id, "workflow_state": "unsubmitted",
         "user": {"id": learner_id, "name": "Test Learner"}},
    ]
    return canvas


# --- defaults ---------------------------------------------------------------

def test_course_disabled_by_default(session: Session) -> None:
    assert is_course_enabled(session, 1) is False


def test_learner_enabled_by_default(session: Session) -> None:
    assert is_learner_enabled(session, 1) is True


# --- upsert behaviour -------------------------------------------------------

def test_upsert_course_config_creates_then_updates(session: Session) -> None:
    cfg = upsert_course_config(session, 1, enabled=True, name="Phonics G2")
    session.commit()
    assert cfg.enabled is True
    assert cfg.name == "Phonics G2"

    cfg2 = upsert_course_config(session, 1, enabled=False)
    session.commit()
    assert cfg2.course_id == cfg.course_id  # same row
    assert cfg2.enabled is False
    assert cfg2.name == "Phonics G2"  # name preserved when not given


def test_upsert_learner_config_opt_out_then_back_in(session: Session) -> None:
    upsert_learner_config(session, 42, enabled=False, name="Test L", notes="Family request")
    session.commit()
    assert is_learner_enabled(session, 42) is False

    upsert_learner_config(session, 42, enabled=True)
    session.commit()
    assert is_learner_enabled(session, 42) is True


# --- end-to-end filter ------------------------------------------------------

def test_default_disabled_course_yields_nothing(session: Session) -> None:
    """Without an explicit enable, no candidates pass the filter."""
    now = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    assert list(find_72h_candidates(_fake_canvas(), session, now=now)) == []


def test_enabled_course_yields_candidate(session: Session) -> None:
    upsert_course_config(session, 1, enabled=True)
    session.commit()
    now = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    candidates = list(find_72h_candidates(_fake_canvas(), session, now=now))
    assert len(candidates) == 1
    assert candidates[0].course_id == 1
    assert candidates[0].learner_id == 7


def test_opted_out_learner_filtered_out(session: Session) -> None:
    upsert_course_config(session, 1, enabled=True)
    upsert_learner_config(session, 7, enabled=False, notes="Pilot opt-out")
    session.commit()
    now = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    assert list(find_72h_candidates(_fake_canvas(), session, now=now)) == []


def test_no_session_means_no_filter(session: Session) -> None:
    """Passing session=None should let candidates through unfiltered (test convenience)."""
    now = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    candidates = list(find_72h_candidates(_fake_canvas(), None, now=now))
    assert len(candidates) == 1
