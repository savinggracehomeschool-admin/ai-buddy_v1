"""Canvas LMS HTTP client with built-in rate-limit handling.

Single-threaded by design (the MVP never runs concurrent Canvas calls).
Every request reads X-Rate-Limit-Remaining and X-Request-Cost and respects
429 responses with exponential backoff.

Usage:
    with CanvasClient() as canvas:
        courses = canvas.list_courses()
        for c in courses:
            assignments = canvas.list_upcoming_assignments(c["id"])
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import httpx

from sgeg_nudge.config import settings

log = logging.getLogger(__name__)

# When remaining budget drops below this, sleep before the next request.
RATE_LIMIT_THROTTLE_AT = 200
THROTTLE_SLEEP_SECONDS = 1.0

# 429 backoff: 2s, 4s, 8s, 16s, then cap at 60s.
BACKOFF_SCHEDULE = (2.0, 4.0, 8.0, 16.0, 60.0)

# Stop the job entirely after this many 429s in a row — needs human eyes.
MAX_CONSECUTIVE_429 = 5

# 5xx retry settings (server errors are usually transient).
MAX_5XX_RETRIES = 3
_5XX_BACKOFF = (1.0, 3.0, 7.0)


class CanvasRateLimitError(RuntimeError):
    """Raised when Canvas keeps returning 429 — abort the daily job."""


class CanvasClient:
    """Thin wrapper over httpx.Client that respects Canvas rate limits."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url or settings.canvas_base_url,
            headers={"Authorization": f"Bearer {token or settings.canvas_api_token}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CanvasClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ---------------- request layer ----------------

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Make one HTTP call with 429 retry + 5xx retry + adaptive throttle."""
        consecutive_429 = 0
        attempt_429 = 0
        attempt_5xx = 0
        while True:
            resp = self._client.request(method, url, **kwargs)
            remaining = resp.headers.get("X-Rate-Limit-Remaining")
            cost = resp.headers.get("X-Request-Cost")
            log.info(
                "%s %s -> %s  remaining=%s cost=%s",
                method, url, resp.status_code, remaining, cost,
            )

            if resp.status_code == 429:
                consecutive_429 += 1
                if consecutive_429 >= MAX_CONSECUTIVE_429:
                    msg = (
                        f"{MAX_CONSECUTIVE_429} consecutive 429s on {method} {url}. "
                        "Daily job halted; manual intervention required."
                    )
                    self._alert_admin_safe("Canvas rate-limit halt", msg)
                    raise CanvasRateLimitError(msg)
                delay = self._429_delay(resp, attempt_429)
                log.warning("Canvas 429; sleeping %.1fs (attempt %d)", delay, attempt_429 + 1)
                time.sleep(delay)
                attempt_429 += 1
                continue

            if 500 <= resp.status_code < 600:
                if attempt_5xx >= MAX_5XX_RETRIES:
                    log.error("Canvas %s for %s %s — giving up after %d retries",
                              resp.status_code, method, url, attempt_5xx)
                    resp.raise_for_status()
                delay = _5XX_BACKOFF[min(attempt_5xx, len(_5XX_BACKOFF) - 1)]
                log.warning("Canvas %s for %s; sleeping %.1fs (retry %d/%d)",
                            resp.status_code, url, delay, attempt_5xx + 1, MAX_5XX_RETRIES)
                time.sleep(delay)
                attempt_5xx += 1
                continue

            self._throttle_if_low(remaining)
            resp.raise_for_status()
            return resp

    @staticmethod
    def _alert_admin_safe(subject: str, body: str) -> None:
        """Send an alert to the admin, swallowing all errors so we never cascade."""
        try:
            # Local import avoids circular dependency with alerts.py
            from sgeg_nudge.alerts import send_alert_to_admin
            send_alert_to_admin(subject, body)
        except Exception:
            log.exception("alert dispatch failed")

    @staticmethod
    def _429_delay(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), BACKOFF_SCHEDULE[-1])
            except ValueError:
                pass
        return BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]

    @staticmethod
    def _throttle_if_low(remaining_header: str | None) -> None:
        if remaining_header is None:
            return
        try:
            remaining = float(remaining_header)
        except ValueError:
            return
        if remaining < RATE_LIMIT_THROTTLE_AT:
            log.info(
                "Rate-limit budget low (%.0f < %d); sleeping %.1fs",
                remaining, RATE_LIMIT_THROTTLE_AT, THROTTLE_SLEEP_SECONDS,
            )
            time.sleep(THROTTLE_SLEEP_SECONDS)

    # ---------------- pagination ----------------

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        """GET path, follow Link: rel=next until exhausted, yield each item."""
        merged = {"per_page": 100, **(params or {})}
        resp = self._request("GET", path, params=merged)
        while True:
            for item in resp.json():
                yield item
            next_url = self._next_link(resp)
            if not next_url:
                return
            # The next-link URL is absolute and already carries its own query string,
            # so we don't pass params again.
            resp = self._request("GET", next_url)

    @staticmethod
    def _next_link(resp: httpx.Response) -> str | None:
        link = resp.headers.get("Link")
        if not link:
            return None
        for part in link.split(","):
            url_part, _, rel_part = part.strip().partition(";")
            if 'rel="next"' in rel_part:
                return url_part.strip().strip("<>")
        return None

    # ---------------- public API ----------------

    def list_courses(self, enrollment_state: str = "active") -> list[dict]:
        """All courses the token's user is enrolled in with the given state."""
        return list(self._paginate(
            "/api/v1/courses",
            {"enrollment_state": enrollment_state},
        ))

    def list_assignments(
        self,
        course_id: int,
        *,
        bucket: str | None = None,
    ) -> list[dict]:
        """List assignments in a course, optionally filtered by Canvas's bucket value.

        Buckets: 'past', 'overdue', 'undated', 'ungraded', 'unsubmitted', 'upcoming', 'future'.
        """
        params: dict = {}
        if bucket:
            params["bucket"] = bucket
        return list(self._paginate(
            f"/api/v1/courses/{course_id}/assignments",
            params,
        ))

    def list_upcoming_assignments(self, course_id: int) -> list[dict]:
        """Assignments in a course whose due_at is in the future (bucket=upcoming)."""
        return self.list_assignments(course_id, bucket="upcoming")

    def list_past_assignments(self, course_id: int) -> list[dict]:
        """Assignments whose due_at is in the past (bucket=past) — used for missed-submission detection."""
        return self.list_assignments(course_id, bucket="past")

    def list_observer_enrollments(self, course_id: int) -> list[dict]:
        """Return active observer enrolments for a course.

        Each row has `user_id` (the observer / parent) and `associated_user_id`
        (the learner they observe). Used to look up a learner's parents for
        the parent-CC nudge feature.
        """
        return list(self._paginate(
            f"/api/v1/courses/{course_id}/enrollments",
            {"type[]": "ObserverEnrollment", "state[]": "active"},
        ))

    def list_courses_for_user(
        self,
        user_id: int | str,
        *,
        enrollment_type: str | None = "student",
        state: str | None = "active",
    ) -> list[dict]:
        """Return Canvas courses a given user is enrolled in.

        Defaults to active student enrolments — i.e. the subjects the learner
        is currently taking. Used by the replier to cross-reference whether a
        learner's question concerns a subject they're actually enrolled in.
        """
        params: dict = {}
        if enrollment_type:
            params["enrollment_type"] = enrollment_type
        if state:
            params["state[]"] = state
        return list(self._paginate(
            f"/api/v1/users/{user_id}/courses",
            params,
        ))

    def list_submissions(self, course_id: int, assignment_id: int) -> list[dict]:
        """All student submission records for one assignment in a course.

        Always includes the embedded `user` object so the caller has name +
        locale per learner without a second round-trip per user.
        """
        return list(self._paginate(
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions",
            {"include[]": "user"},
        ))

    def list_missing_submissions(
        self,
        user_id: int | str,
    ) -> list[dict]:
        """Canvas-side list of assignments this user is missing (past due, not submitted)."""
        return list(self._paginate(
            f"/api/v1/users/{user_id}/missing_submissions",
            {"include[]": "course"},
        ))

    def get_student_submissions_bulk(
        self,
        course_id: int | str,
        user_id: int | str,
    ) -> list[dict]:
        """Fetch ALL submission records for one student in a course in a single paginated call.

        Uses /courses/:id/students/submissions which returns one row per assignment
        regardless of whether the student submitted. Each row includes:
          - assignment_id, submitted_at, missing (Canvas flag), late, excused,
            workflow_state ('submitted'|'unsubmitted'|'graded'|'pending_review'),
            score, grade.

        This is the authoritative bulk check — far more reliable than N+1 calls
        to /submissions/:assignment_id/:user_id.
        """
        return list(self._paginate(
            f"/api/v1/courses/{course_id}/students/submissions",
            {
                "student_ids[]": str(user_id),
                "include[]": ["assignment"],
                "per_page": 100,
            },
        ))

    def get_student_assignment_analytics(
        self,
        course_id: int | str,
        user_id: int | str,
    ) -> list[dict]:
        """Return per-student assignment analytics from the Canvas Analytics API.

        Endpoint: GET /courses/:id/analytics/users/:uid/assignments

        Each record includes:
          assignment_id, title, due_at, points_possible, excused, status, submission
          status values: 'on_time'|'late'|'missing'|'unsubmitted'|'floating'|'excused'
            floating  = no due date and not submitted (e.g. undated Cambridge papers)
            missing   = past due, not submitted
            unsubmitted = future due date, not submitted
          submission.submitted_at  — None if not submitted
          submission.score         — None if not graded

        Only PUBLISHED assignments visible to the student are returned — unpublished
        assignments never appear here, unlike the raw assignments list endpoint.
        """
        return list(self._paginate(
            f"/api/v1/courses/{course_id}/analytics/users/{user_id}/assignments",
            {"per_page": 100},
        ))

    def get_submission(
        self,
        course_id: int,
        assignment_id: int,
        user_id: int | str,
    ) -> dict:
        """Return the submission record for one learner on one assignment.

        Used for the safety re-check at send time: even if a learner was a
        candidate ten minutes ago, we re-verify right before sending so we
        never nudge about an assignment they just submitted.
        """
        return self._request(
            "GET",
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}",
        ).json()

    def whoami(self) -> dict:
        """Return /api/v1/users/self — the user the API token authenticates as.

        Used as a sanity check that we're sending FROM the right Canvas user
        (should always be the SGEG Assistant in production).
        """
        return self._request("GET", "/api/v1/users/self").json()

    def lookup_user(self, user_id: int | str) -> dict:
        """Return the Canvas user record for a given user ID.

        Used to confirm a recipient's identity before sending a nudge.
        """
        return self._request("GET", f"/api/v1/users/{user_id}").json()

    def get_conversation(self, conversation_id: int | str) -> dict:
        """Return the full conversation thread including all messages.

        We pass auto_mark_as_read=false because Canvas's default behaviour is
        to mark a conversation read just because we GET it — that would hide
        unprocessed threads from our next unread-poll cycle if the reply step
        later fails.
        """
        return self._request(
            "GET",
            f"/api/v1/conversations/{conversation_id}",
            params={"auto_mark_as_read": "false"},
        ).json()

    def reply_to_conversation(
        self,
        conversation_id: int | str,
        body: str,
    ) -> dict:
        """POST /api/v1/conversations/:id/add_message — append a reply to an existing thread."""
        return self._request(
            "POST",
            f"/api/v1/conversations/{conversation_id}/add_message",
            data={"body": body},
        ).json()

    def mark_conversation_read(self, conversation_id: int | str) -> dict:
        """PUT workflow_state=read on a conversation so it stops appearing in unread polls."""
        return self._request(
            "PUT",
            f"/api/v1/conversations/{conversation_id}",
            data={"conversation[workflow_state]": "read"},
        ).json()

    def list_conversations(
        self,
        *,
        scope: str | None = None,
        limit: int = 25,
    ) -> list[dict]:
        """Read SGEG Assistant's Canvas Conversations inbox.

        scope: one of 'unread', 'archived', 'sent', 'starred'.
            Omit (None) for the default inbox view.
        limit: max conversations to return on this call (Canvas caps at 100/page).
        """
        params: dict = {"per_page": min(limit, 100)}
        if scope:
            params["scope"] = scope
        return self._request("GET", "/api/v1/conversations", params=params).json()

    # ── Course content (modules, pages, assignments) ──────────────────────────

    def get_student_enrollments(self, user_id: int | str) -> list[dict]:
        """Return all active StudentEnrollment and ObserverEnrollment rows for a user.

        ObserverEnrollment covers parent/guardian accounts that monitor courses —
        they see the same courses and grades as the students they observe.
        """
        return list(self._paginate(
            f"/api/v1/users/{user_id}/enrollments",
            {
                "state[]": "active",
                "type[]": ["StudentEnrollment", "ObserverEnrollment"],
                "include[]": ["grades", "course"],
            },
        ))

    def get_course_enrollment_for_user(
        self,
        course_id: int | str,
        user_id: int | str,
    ) -> list[dict]:
        """Return the StudentEnrollment row(s) for one student in one course, with grades."""
        return list(self._paginate(
            f"/api/v1/courses/{course_id}/enrollments",
            {
                "user_id": str(user_id),
                "type[]": "StudentEnrollment",
                "include[]": "grades",
            },
        ))

    def get_course(self, course_id: int | str) -> dict:
        """Return the course record including name, course_code, and workflow_state."""
        return self._request(
            "GET",
            f"/api/v1/courses/{course_id}",
            params={"include[]": ["total_students", "public_description"]},
        ).json()

    def list_assignment_groups(self, course_id: int | str) -> list[dict]:
        """Return assignment groups for a course (id, name, position)."""
        return list(self._paginate(
            f"/api/v1/courses/{course_id}/assignment_groups",
            {},
        ))

    def list_modules(self, course_id: int | str) -> list[dict]:
        """Return all modules in a course, ordered by position.

        Each module dict includes: id, name, position, items_count, state.
        """
        return list(self._paginate(
            f"/api/v1/courses/{course_id}/modules",
            {"include[]": "items"},
        ))

    def list_module_items(self, course_id: int | str, module_id: int | str) -> list[dict]:
        """Return all items in a module.

        Each item has: id, title, type (Page/Assignment/Quiz/File/…), position,
        content_id, html_url, url (API url for the underlying resource).
        """
        return list(self._paginate(
            f"/api/v1/courses/{course_id}/modules/{module_id}/items",
            {"include[]": "content_details"},
        ))

    def get_page(self, course_id: int | str, page_url: str) -> dict:
        """Return a wiki page record including its body HTML.

        `page_url` is the page's URL slug (the part after /pages/ in Canvas),
        or the numeric page ID as a string.
        """
        return self._request(
            "GET",
            f"/api/v1/courses/{course_id}/pages/{page_url}",
        ).json()

    def list_pages(self, course_id: int | str) -> list[dict]:
        """Return all published wiki pages in a course (title + url, no body)."""
        return list(self._paginate(
            f"/api/v1/courses/{course_id}/pages",
            {"sort": "title", "published": "true"},
        ))

    def get_assignment_details(self, course_id: int | str, assignment_id: int | str) -> dict:
        """Return a single assignment with full description HTML."""
        return self._request(
            "GET",
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}",
        ).json()

    def send_conversation(
        self,
        *,
        recipient_ids: list[int | str],
        body: str,
        subject: str | None = None,
        force_new: bool = True,
        group_conversation: bool = False,
    ) -> dict:
        """POST /api/v1/conversations — send a message to one or more Canvas users.

        The sender is implicit (whoever the API token belongs to). Returns the
        first conversation object Canvas creates. The returned conversation_id
        should be stored in the nudge row so we can link the audit record back
        to the actual Canvas thread.

        force_new=True opens a fresh thread instead of appending to any existing
        conversation with the recipient — keeps per-nudge audit clean.
        """
        data: dict = {
            "recipients[]": [str(r) for r in recipient_ids],
            "body": body,
            "force_new": "true" if force_new else "false",
            "group_conversation": "true" if group_conversation else "false",
        }
        if subject:
            data["subject"] = subject

        resp = self._request("POST", "/api/v1/conversations", data=data)
        payload = resp.json()
        # Canvas returns a list of conversations (one per recipient when not grouped).
        if isinstance(payload, list):
            return payload[0] if payload else {}
        return payload
