"""Tests for the candidate-finder pure helpers + window filtering + drift regex."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from sgeg_nudge.db import TIER_72H
from sgeg_nudge.nudge import (
    Candidate,
    _first_name,
    _friendly_due_at,
    _is_submitted,
    _language_from_user,
    _parse_iso8601,
    find_candidates_in_window,
)


# --- _parse_iso8601 ---------------------------------------------------------

def test_parse_iso8601_z_suffix_returns_aware_datetime() -> None:
    dt = _parse_iso8601("2026-05-21T17:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.hour == 17


def test_parse_iso8601_none_or_empty_returns_none() -> None:
    assert _parse_iso8601(None) is None
    assert _parse_iso8601("") is None


# --- _first_name ------------------------------------------------------------

@pytest.mark.parametrize(
    "full,expected",
    [
        ("Thandi Nkosi", "Thandi"),
        ("  Liam  ", "Liam"),
        ("Aiden", "Aiden"),
        (None, "there"),
        ("", "there"),
        ("   ", "there"),
    ],
)
def test_first_name(full: str | None, expected: str) -> None:
    assert _first_name(full) == expected


# --- _is_submitted ----------------------------------------------------------

@pytest.mark.parametrize(
    "submission,expected",
    [
        ({"workflow_state": "submitted"}, True),
        ({"workflow_state": "graded"}, True),
        ({"workflow_state": "pending_review"}, True),
        ({"workflow_state": "unsubmitted"}, False),
        ({"workflow_state": None}, False),
        ({"submitted_at": "2026-05-21T10:00:00Z"}, True),
        ({"submitted_at": None, "workflow_state": "unsubmitted"}, False),
        ({}, False),
    ],
)
def test_is_submitted(submission: dict, expected: bool) -> None:
    assert _is_submitted(submission) is expected


# --- _language_from_user ----------------------------------------------------

@pytest.mark.parametrize(
    "user,expected",
    [
        ({}, "en"),
        ({"locale": "en"}, "en"),
        ({"locale": "en-ZA"}, "en"),
        ({"locale": "af"}, "af"),
        ({"locale": "af-ZA"}, "af"),
        ({"effective_locale": "af-ZA"}, "af"),
        ({"locale": "", "effective_locale": "af"}, "af"),
    ],
)
def test_language_from_user(user: dict, expected: str) -> None:
    assert _language_from_user(user) == expected


# --- _friendly_due_at -------------------------------------------------------

def test_friendly_due_at_none() -> None:
    assert _friendly_due_at(None, "en") == "soon"


def test_friendly_due_at_afrikaans_format() -> None:
    # 2026-05-22 17:00 UTC = Friday 19:00 SAST
    dt = datetime(2026, 5, 22, 17, 0, tzinfo=timezone.utc)
    text = _friendly_due_at(dt, "af")
    assert text.startswith("Vrydag om")


def test_friendly_due_at_english_includes_weekday() -> None:
    dt = datetime(2026, 5, 22, 15, 0, tzinfo=timezone.utc)  # Fri 17:00 SAST
    text = _friendly_due_at(dt, "en")
    assert "Friday" in text


# --- find_candidates_in_window ---------------------------------------------

def _fake_canvas(courses: list[dict], assignments_by_course: dict, subs_by_assignment: dict) -> MagicMock:
    """Build a MagicMock CanvasClient whose three list_* methods return canned data."""
    canvas = MagicMock()
    canvas.list_courses.return_value = courses
    canvas.list_upcoming_assignments.side_effect = lambda cid: assignments_by_course.get(cid, [])
    canvas.list_submissions.side_effect = lambda cid, aid: subs_by_assignment.get(aid, [])
    return canvas


def _iso(now: datetime, hours: int) -> str:
    return (now + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def test_window_picks_only_assignments_inside_range() -> None:
    """72h tier window is (24h, 72h] — pick 48h, drop 12h, drop 100h, drop no-due-date."""
    now = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    courses = [{"id": 1, "name": "Test"}]
    assignments = {
        1: [
            {"id": 100, "name": "Too soon",  "due_at": _iso(now, 12)},
            {"id": 101, "name": "Just right", "due_at": _iso(now, 48)},
            {"id": 102, "name": "Too far",   "due_at": _iso(now, 100)},
            {"id": 103, "name": "No date",   "due_at": None},
        ],
    }
    subs = {
        101: [
            {"user_id": 7, "workflow_state": "unsubmitted",
             "user": {"id": 7, "name": "Thandi Nkosi", "locale": "en"}},
        ],
    }
    canvas = _fake_canvas(courses, assignments, subs)
    candidates = list(find_candidates_in_window(
        canvas, tier=TIER_72H, lower_hours=24, upper_hours=72, now=now,
    ))
    assert len(candidates) == 1
    c = candidates[0]
    assert c.assignment_id == 101
    assert c.learner_id == 7
    assert c.learner_first_name == "Thandi"
    assert c.tier == TIER_72H


def test_window_excludes_submitted_learners() -> None:
    now = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    courses = [{"id": 1, "name": "Test"}]
    assignments = {1: [{"id": 101, "name": "X", "due_at": _iso(now, 48)}]}
    subs = {
        101: [
            {"user_id": 7, "workflow_state": "submitted", "user": {"id": 7, "name": "Already In"}},
            {"user_id": 8, "workflow_state": "unsubmitted", "user": {"id": 8, "name": "Still Owes"}},
            {"user_id": 9, "submitted_at": "2026-05-20T10:00:00Z",
             "user": {"id": 9, "name": "Late But Done"}},
        ],
    }
    canvas = _fake_canvas(courses, assignments, subs)
    candidates = list(find_candidates_in_window(
        canvas, tier=TIER_72H, lower_hours=24, upper_hours=72, now=now,
    ))
    assert {c.learner_id for c in candidates} == {8}


def test_window_picks_up_locale_for_language() -> None:
    now = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    courses = [{"id": 1, "name": "T"}]
    assignments = {1: [{"id": 101, "name": "X", "due_at": _iso(now, 48)}]}
    subs = {
        101: [
            {"user_id": 1, "workflow_state": "unsubmitted",
             "user": {"id": 1, "name": "Annelie", "locale": "af-ZA"}},
            {"user_id": 2, "workflow_state": "unsubmitted",
             "user": {"id": 2, "name": "Liam", "locale": "en-ZA"}},
        ],
    }
    canvas = _fake_canvas(courses, assignments, subs)
    by_id = {c.learner_id: c for c in find_candidates_in_window(
        canvas, tier=TIER_72H, lower_hours=24, upper_hours=72, now=now,
    )}
    assert by_id[1].learner_language == "af"
    assert by_id[2].learner_language == "en"


def test_drift_does_not_fire_on_grade_level_in_course_name() -> None:
    """'Grade 2 Mathematics' is the year-level, not an academic mark."""
    from sgeg_nudge.claude import detect_drift
    text = "You're enrolled in CAPS Grade 2 Mathematics and CAPS Grade 10 Travel and Tourism."
    assert detect_drift(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "Your grade is 75%",
        "your mark for the test was 8/10",
        "Your score is excellent",
        "you reached the top of the ranking",
        "you got 85%",
        "your result was 60/100 marks",
    ],
)
def test_drift_fires_on_actual_evaluation_phrasing(text: str) -> None:
    from sgeg_nudge.claude import detect_drift
    assert detect_drift(text), f"expected drift on {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        # Padding to keep > 8 words so the suspiciously-short check doesn't fire.
        "Hi there, please open your Grade 2 Mathematics course and look at Modules.",
        "Your Grade 12 assignment is in the Modules section of the course menu.",
        "I noticed you have a Grade 5 question listed in the syllabus document.",
        "You can find the Grade 2 syllabus inside the Modules tab of the course.",
    ],
)
def test_drift_does_not_fire_on_grade_with_year_level(text: str) -> None:
    from sgeg_nudge.claude import detect_drift
    assert detect_drift(text) == [], f"unexpected drift on {text!r}"


def test_find_weekly_digest_yields_one_candidate_per_learner() -> None:
    """Two learners each with 2 outstanding items → 2 digest candidates, each with 2 items."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sgeg_nudge.db import Base, TIER_WEEKLY_PREFIX, upsert_course_config
    from sgeg_nudge.nudge import find_weekly_digest_candidates

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(engine, expire_on_commit=False, future=True)

    now = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
    iso_in_3d = _iso(now, 72)
    iso_past_3d = _iso(now, -72)

    canvas = MagicMock()
    canvas.list_courses.return_value = [{"id": 1, "name": "Maths"}]
    canvas.list_assignments.return_value = [
        {"id": 101, "name": "Quiz 1", "due_at": iso_in_3d},
        {"id": 102, "name": "Quiz 2", "due_at": iso_past_3d},
    ]
    # Same submissions for both assignments — two unsubmitted learners
    canvas.list_submissions.side_effect = lambda c, a: [
        {"user_id": 7, "workflow_state": "unsubmitted", "user": {"id": 7, "name": "Thandi"}},
        {"user_id": 8, "workflow_state": "unsubmitted", "user": {"id": 8, "name": "Liam"}},
    ]

    with SessionLocal() as session:
        upsert_course_config(session, 1, enabled=True)
        session.commit()
        candidates = list(find_weekly_digest_candidates(canvas, session, now=now))

    assert len(candidates) == 2
    by_id = {c.learner_id: c for c in candidates}
    assert set(by_id) == {7, 8}
    for c in candidates:
        assert c.tier.startswith(TIER_WEEKLY_PREFIX)
        assert "2026-W21" in c.tier  # ISO week 21 of 2026
        assert len(c.outstanding_items) == 2
        # one past-due, one not
        flags = {item["is_past_due"] for item in c.outstanding_items}
        assert flags == {True, False}


def test_find_weekly_digest_respects_course_config() -> None:
    """Digest excludes disabled courses."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sgeg_nudge.db import Base
    from sgeg_nudge.nudge import find_weekly_digest_candidates

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(engine, expire_on_commit=False, future=True)

    now = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
    canvas = MagicMock()
    canvas.list_courses.return_value = [{"id": 1, "name": "T"}]
    canvas.list_assignments.return_value = [{"id": 101, "name": "X", "due_at": _iso(now, 72)}]
    canvas.list_submissions.return_value = [
        {"user_id": 7, "workflow_state": "unsubmitted", "user": {"id": 7, "name": "X"}},
    ]

    with SessionLocal() as session:
        # No CourseConfig row → course is disabled → no candidates
        assert list(find_weekly_digest_candidates(canvas, session, now=now)) == []


def test_find_weekly_digest_drops_far_out_items() -> None:
    """Items > 14 days past or > 14 days ahead are excluded."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sgeg_nudge.db import Base, upsert_course_config
    from sgeg_nudge.nudge import find_weekly_digest_candidates

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(engine, expire_on_commit=False, future=True)

    now = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
    canvas = MagicMock()
    canvas.list_courses.return_value = [{"id": 1, "name": "T"}]
    canvas.list_assignments.return_value = [
        {"id": 1, "name": "Way past", "due_at": _iso(now, -24 * 30)},
        {"id": 2, "name": "Way future", "due_at": _iso(now, 24 * 60)},
        {"id": 3, "name": "In window", "due_at": _iso(now, 72)},
    ]
    canvas.list_submissions.side_effect = lambda c, a: [
        {"user_id": 7, "workflow_state": "unsubmitted", "user": {"id": 7, "name": "X"}},
    ]

    with SessionLocal() as session:
        upsert_course_config(session, 1, enabled=True)
        session.commit()
        candidates = list(find_weekly_digest_candidates(canvas, session, now=now))

    assert len(candidates) == 1
    assert len(candidates[0].outstanding_items) == 1
    assert candidates[0].outstanding_items[0]["assignment_name"] == "In window"


@pytest.mark.parametrize(
    "course_name,expected",
    [
        ("CAPS Grade 2 Mathematics", 2),
        ("CAPS Grade R English", 0),
        ("CAPS Grade 10 Travel and Tourism", 10),
        ("CAPS Grade 12 Life Orientation", 12),
        ("Developer Test Archived - CAPS Grade 2 English Home Language", 2),
        ("Saving Grace - General Information", None),
        ("Nothing useful here", None),
        ("Grade 13 Imaginary Subject", None),  # > 12, reject
        ("", None),
        (None, None),
    ],
)
def test_extract_grade_from_course_name(course_name: str | None, expected: int | None) -> None:
    from sgeg_nudge.nudge import _extract_grade
    assert _extract_grade(course_name) == expected


@pytest.mark.parametrize(
    "grade,expected",
    [(None, False), (0, True), (1, True), (7, True), (8, False), (12, False)],
)
def test_should_cc_parent(grade: int | None, expected: bool) -> None:
    from sgeg_nudge.nudge import _should_cc_parent
    assert _should_cc_parent(grade) is expected


def test_find_parents_for_learner_filters_correctly() -> None:
    """Only observers whose associated_user_id matches the learner are returned."""
    from sgeg_nudge.nudge import _find_parents_for_learner
    canvas = MagicMock()
    canvas.list_observer_enrollments.return_value = [
        {"user_id": 1000, "associated_user_id": 7},  # parent of 7
        {"user_id": 1001, "associated_user_id": 7},  # second parent of 7
        {"user_id": 1002, "associated_user_id": 8},  # parent of someone else
        {"user_id": 1000, "associated_user_id": 7},  # duplicate — should dedup
    ]
    parents = _find_parents_for_learner(canvas, course_id=42, learner_id=7)
    assert parents == [1000, 1001]


def test_find_parents_swallows_canvas_errors() -> None:
    """If the observer-enrolment endpoint fails, return an empty list (no crash)."""
    from sgeg_nudge.nudge import _find_parents_for_learner
    canvas = MagicMock()
    canvas.list_observer_enrollments.side_effect = RuntimeError("403")
    assert _find_parents_for_learner(canvas, course_id=42, learner_id=7) == []


def test_find_new_content_picks_recent_with_future_due() -> None:
    """An assignment created in the last 7 days with a future due_at should yield a candidate per enrolled learner."""
    from sgeg_nudge.nudge import find_new_content_candidates
    from sgeg_nudge.db import TIER_NEW

    now = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
    iso_created_recent = (now - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    iso_due_future = _iso(now, 100)

    courses = [{"id": 1, "name": "Test"}]
    canvas = MagicMock()
    canvas.list_courses.return_value = courses
    canvas.list_assignments.return_value = [
        {"id": 101, "name": "Brand new", "created_at": iso_created_recent, "due_at": iso_due_future},
    ]
    canvas.list_submissions.return_value = [
        {"user_id": 7, "workflow_state": "unsubmitted", "user": {"id": 7, "name": "Thandi"}},
        {"user_id": 8, "workflow_state": "unsubmitted", "user": {"id": 8, "name": "Liam"}},
    ]
    candidates = list(find_new_content_candidates(canvas, None, now=now))
    assert {c.learner_id for c in candidates} == {7, 8}
    assert all(c.tier == TIER_NEW for c in candidates)


def test_find_new_content_skips_old_assignments() -> None:
    """An assignment created > lookback_days ago should NOT yield candidates."""
    from sgeg_nudge.nudge import find_new_content_candidates

    now = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
    iso_created_old = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")

    canvas = MagicMock()
    canvas.list_courses.return_value = [{"id": 1, "name": "T"}]
    canvas.list_assignments.return_value = [
        {"id": 101, "name": "Old", "created_at": iso_created_old, "due_at": _iso(now, 48)},
    ]
    canvas.list_submissions.return_value = [
        {"user_id": 7, "workflow_state": "unsubmitted", "user": {"id": 7, "name": "X"}},
    ]
    assert list(find_new_content_candidates(canvas, None, now=now)) == []


def test_find_new_content_skips_past_due_new_work() -> None:
    """If a 'new' assignment is already past due, it falls through to the missed tier."""
    from sgeg_nudge.nudge import find_new_content_candidates

    now = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
    iso_created_recent = (now - timedelta(hours=12)).isoformat().replace("+00:00", "Z")
    iso_due_past = _iso(now, -2)

    canvas = MagicMock()
    canvas.list_courses.return_value = [{"id": 1, "name": "T"}]
    canvas.list_assignments.return_value = [
        {"id": 101, "name": "Late-published", "created_at": iso_created_recent, "due_at": iso_due_past},
    ]
    canvas.list_submissions.return_value = [
        {"user_id": 7, "workflow_state": "unsubmitted", "user": {"id": 7, "name": "X"}},
    ]
    assert list(find_new_content_candidates(canvas, None, now=now)) == []


def test_find_new_content_skips_already_submitted() -> None:
    """If a learner already submitted the brand-new assignment, no candidate."""
    from sgeg_nudge.nudge import find_new_content_candidates

    now = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
    iso_created_recent = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    iso_due_future = _iso(now, 72)

    canvas = MagicMock()
    canvas.list_courses.return_value = [{"id": 1, "name": "T"}]
    canvas.list_assignments.return_value = [
        {"id": 101, "name": "New", "created_at": iso_created_recent, "due_at": iso_due_future},
    ]
    canvas.list_submissions.return_value = [
        {"user_id": 7, "workflow_state": "submitted", "user": {"id": 7, "name": "EarlyBird"}},
        {"user_id": 8, "workflow_state": "unsubmitted", "user": {"id": 8, "name": "NotYet"}},
    ]
    candidates = list(find_new_content_candidates(canvas, None, now=now))
    assert {c.learner_id for c in candidates} == {8}


def test_window_skips_assignment_with_no_due_date() -> None:
    now = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    courses = [{"id": 1, "name": "T"}]
    assignments = {1: [{"id": 101, "name": "X", "due_at": None}]}
    subs = {101: [{"user_id": 1, "user": {"id": 1, "name": "X"}, "workflow_state": "unsubmitted"}]}
    canvas = _fake_canvas(courses, assignments, subs)
    candidates = list(find_candidates_in_window(
        canvas, tier=TIER_72H, lower_hours=24, upper_hours=72, now=now,
    ))
    assert candidates == []
