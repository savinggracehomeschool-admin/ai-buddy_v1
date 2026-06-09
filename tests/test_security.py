"""Security guardrail tests — Phase 1.

Acceptance criteria from the implementation plan:
- A tool call with an out-of-scope course_id is rejected at the API layer
- Cross-sub-account fetches fail
- Cross-student fetches are structurally impossible (user_id is always session-bound)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from sgeg_nudge.db import init_engine, get_session, LTISession, session_allows_course, session_allows_account


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def in_memory_db():
    """Each test gets a fresh in-memory SQLite DB."""
    init_engine("sqlite:///:memory:")
    yield


def _make_session(
    user_id: str = "852",
    course_id: str = "224",
    enrolled_course_ids: list[str] | None = None,
    enrolled_account_ids: list[int] | None = None,
    launch_account_id: int | None = 209,
) -> LTISession:
    now = datetime.utcnow()
    row = LTISession(
        session_id="test-session",
        user_id=user_id,
        course_id=course_id,
        user_name="Test Student",
        roles="Learner",
        platform_id="dev",
        enrolled_course_ids=json.dumps(enrolled_course_ids) if enrolled_course_ids is not None else None,
        enrolled_account_ids=json.dumps(enrolled_account_ids) if enrolled_account_ids is not None else None,
        launch_account_id=launch_account_id,
        created_at=now,
        expires_at=now + timedelta(hours=8),
    )
    with get_session() as db:
        db.add(row)
        db.commit()
    return row


# ── 1. session_allows_course helper ──────────────────────────────────────────

def test_allows_course_when_enrolled():
    row = _make_session(enrolled_course_ids=["224", "655"])
    assert session_allows_course(row, "224") is True
    assert session_allows_course(row, "655") is True


def test_blocks_course_when_not_enrolled():
    row = _make_session(enrolled_course_ids=["224", "655"])
    assert session_allows_course(row, "999") is False
    assert session_allows_course(row, "879") is False


def test_allows_course_in_dev_mode_no_enrollment_list():
    """No enrolled_course_ids stored → permissive (dev mode / LTI without Canvas)."""
    row = _make_session(enrolled_course_ids=None)
    assert session_allows_course(row, "any-course") is True


def test_blocks_course_empty_enrollment_list():
    """Empty list stored → all courses blocked (student with no active enrolments)."""
    row = _make_session(enrolled_course_ids=[])
    assert session_allows_course(row, "224") is False


# ── 2. session_allows_account helper ─────────────────────────────────────────

def test_allows_account_when_in_scope():
    row = _make_session(enrolled_account_ids=[209, 216])
    assert session_allows_account(row, 209) is True
    assert session_allows_account(row, 216) is True


def test_blocks_account_outside_scope():
    row = _make_session(enrolled_account_ids=[209])  # CAPS only
    assert session_allows_account(row, 208) is False   # Cambridge blocked
    assert session_allows_account(row, 233) is False   # Special Needs blocked


def test_allows_account_when_no_list_stored():
    """No account list → permissive (dev mode)."""
    row = _make_session(enrolled_account_ids=None)
    assert session_allows_account(row, 208) is True


# ── 3. _run_canvas_tool guardrail ─────────────────────────────────────────────

def test_tool_call_blocked_for_unenrolled_course():
    """get_course_modules with an out-of-scope course_id returns an error, not data."""
    from sgeg_nudge.claude import _run_canvas_tool

    row = _make_session(
        user_id="852",
        course_id="224",
        enrolled_course_ids=["224", "655"],  # 999 is NOT here
    )

    raw, components = _run_canvas_tool(
        "get_course_modules",
        {"course_id": "999"},
        row,
    )

    assert raw.get("error") == "Access denied"
    assert components == []


def test_tool_call_allowed_for_enrolled_course():
    """get_upcoming_assignments with an enrolled course_id passes the guardrail."""
    from sgeg_nudge.claude import _run_canvas_tool

    row = _make_session(
        user_id="852",
        course_id="224",
        enrolled_course_ids=["224", "655"],
    )

    # Mock the Canvas client so we don't need real credentials
    mock_assignment = {
        "id": 1, "name": "Test Assignment", "due_at": None,
        "html_url": "https://canvas.example.com/courses/224/assignments/1",
        "points_possible": 10,
    }
    with patch("sgeg_nudge.canvas.CanvasClient") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.list_assignments.return_value = [mock_assignment]
        instance.get_submission.return_value = {}

        raw, components = _run_canvas_tool(
            "get_upcoming_assignments",
            {"course_id": "224", "bucket": "upcoming"},
            row,
        )

    # Should reach Canvas and return data, not an error
    assert raw.get("error") is None
    assert "assignments" in raw


# ── 4. Cross-student data isolation ──────────────────────────────────────────

def test_grades_tool_uses_session_user_id_only():
    """get_student_grades always fetches for the session's user_id, never a supplied one."""
    from sgeg_nudge.claude import _run_canvas_tool

    row = _make_session(user_id="852", enrolled_course_ids=["224"])

    captured_user_id = {}

    def fake_get_enrollments(uid):
        captured_user_id["uid"] = uid
        return []

    with patch("sgeg_nudge.canvas.CanvasClient") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.get_student_enrollments.side_effect = fake_get_enrollments
        instance.get_course_enrollment_for_user.return_value = []

        # Even if tool_input somehow contained a different user_id, it's ignored —
        # _run_canvas_tool reads user_id from lti_session only.
        _run_canvas_tool("get_student_grades", {}, row)

    # Must have called Canvas with the SESSION user ID, not any injected one
    assert captured_user_id.get("uid") == 852  # int from str "852"


# ── 5. Cross-sub-account fetch blocked ───────────────────────────────────────

def test_modules_blocked_for_out_of_scope_account():
    """get_course_modules is blocked when the course's account is outside the session scope."""
    from sgeg_nudge.claude import _run_canvas_tool

    row = _make_session(
        user_id="852",
        enrolled_course_ids=["999"],          # course exists in enrollment list
        enrolled_account_ids=[209],           # but account 208 (Cambridge) is NOT in scope
    )

    with patch("sgeg_nudge.canvas.CanvasClient") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        # Course 999 belongs to account 208 (Cambridge — outside CAPS scope)
        instance.get_course.return_value = {"account_id": 208, "name": "Cambridge Course"}
        instance.list_modules.return_value = []

        raw, components = _run_canvas_tool(
            "get_course_modules",
            {"course_id": "999"},
            row,
        )

    assert raw.get("error") == "Access denied"
    assert components == []
