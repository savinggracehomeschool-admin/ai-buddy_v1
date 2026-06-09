"""Day 1 smoke test: prove the SGEG Assistant token can reach Canvas.

Calls GET /api/v1/courses and prints the courses the token can see.
Also surfaces the rate-limit headers we'll rely on from Day 2 onward.

Run with:  uv run python scripts/test_canvas.py
"""

from __future__ import annotations

import sys

import httpx

from sgeg_nudge.config import settings


def main() -> int:
    url = f"{settings.canvas_base_url}/api/v1/courses"
    headers = {"Authorization": f"Bearer {settings.canvas_api_token}"}
    # per_page=10 keeps the output readable on Day 1; the real client will use 100.
    params = {"per_page": 10, "enrollment_state": "active"}

    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=15.0)
    except httpx.HTTPError as exc:
        print(f"Network error talking to Canvas: {exc}", file=sys.stderr)
        return 1

    print(f"Canvas: {settings.canvas_base_url}")
    print(f"HTTP {resp.status_code}")
    print(f"X-Rate-Limit-Remaining: {resp.headers.get('X-Rate-Limit-Remaining', 'n/a')}")
    print(f"X-Request-Cost:         {resp.headers.get('X-Request-Cost', 'n/a')}")
    print("-" * 60)

    if resp.status_code == 401:
        print("401 Unauthorized — the SGEG Assistant token is rejected.")
        print("Check the token value in .env and that the user is still active in Canvas.")
        return 1
    if resp.status_code == 403:
        print("403 Forbidden — token is valid but lacks permission for /courses.")
        return 1
    if resp.status_code >= 400:
        print(f"Unexpected error response: {resp.text[:500]}")
        return 1

    courses = resp.json()
    if not courses:
        print("Token works, but SGEG Assistant has no active courses visible.")
        print("Add SGEG Assistant as a Teacher/TA/Observer to at least one course and rerun.")
        return 0

    print(f"Found {len(courses)} course(s):")
    for c in courses:
        print(f"  {c.get('id'):>8}  {c.get('name')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
