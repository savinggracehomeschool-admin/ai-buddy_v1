"""Day 2 test: exercise CanvasClient end-to-end.

Lists courses, then for the first course lists upcoming assignments,
then for the first upcoming assignment lists submissions.
Logs every Canvas call's rate-limit headers so we can see the throttle in action.

Run with:  uv run python scripts/test_canvas_client.py
"""

from __future__ import annotations

import logging
import sys

from sgeg_nudge.canvas import CanvasClient, CanvasRateLimitError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> int:
    try:
        with CanvasClient() as canvas:
            print("== Courses ==")
            courses = canvas.list_courses()
            for c in courses:
                print(f"  {c['id']:>8}  {c.get('name')}")
            if not courses:
                print("(no active courses — nothing else to test)")
                return 0

            course = courses[0]
            print(f"\n== Upcoming assignments in course {course['id']} ==")
            assignments = canvas.list_upcoming_assignments(course["id"])
            for a in assignments:
                print(f"  {a['id']:>8}  due {a.get('due_at') or 'no due date'}  {a.get('name')}")

            if not assignments:
                # Test affordance only: archived/test courses have no upcoming work,
                # but we still want to prove list_submissions wiring end-to-end.
                # Reach past the public API into _paginate to grab any assignment.
                print("(no upcoming — falling back to any past assignment for the submissions test)")
                assignments = list(canvas._paginate(  # noqa: SLF001 - test-only
                    f"/api/v1/courses/{course['id']}/assignments",
                    {"bucket": "past"},
                ))
                for a in assignments[:5]:
                    print(f"  {a['id']:>8}  due {a.get('due_at') or 'no due date'}  {a.get('name')}")

            if not assignments:
                print("(no assignments at all in this course — skipping submissions test)")
                return 0

            assignment = assignments[0]
            print(f"\n== Submissions for assignment {assignment['id']} ==")
            subs = canvas.list_submissions(course["id"], assignment["id"])
            print(f"  {len(subs)} submission record(s) total")
            counts: dict[str, int] = {}
            for s in subs:
                state = s.get("workflow_state", "unknown")
                counts[state] = counts.get(state, 0) + 1
            for state, n in sorted(counts.items()):
                print(f"    {state}: {n}")

    except CanvasRateLimitError as exc:
        print(f"\nABORT: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"\nUnexpected error: {exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
