"""Print the latest conversations in SGEG Assistant's Canvas inbox.

Use this to verify that incoming messages from learners are arriving. The MVP
does NOT auto-reply — replies land here for a human to read.

Usage:
    uv run python scripts/peek_inbox.py
    uv run python scripts/peek_inbox.py --unread
    uv run python scripts/peek_inbox.py --limit 5
    uv run python scripts/peek_inbox.py --watch          # poll every 5s
"""

from __future__ import annotations

import argparse
import time
from typing import Iterable

from sgeg_nudge.canvas import CanvasClient


def _render(conversations: Iterable[dict]) -> None:
    convs = list(conversations)
    if not convs:
        print("(no conversations match)")
        return
    for c in convs:
        audience = ", ".join(p.get("name", "?") for p in c.get("participants", []))
        subject = c.get("subject") or "(no subject)"
        last_msg = (c.get("last_message") or "").strip().replace("\n", " ")
        if len(last_msg) > 160:
            last_msg = last_msg[:160] + "…"
        print(f"[{c['id']}] {c.get('last_message_at')}  {subject}")
        print(f"    state:        {c.get('workflow_state')}  msgs={c.get('message_count')}")
        print(f"    participants: {audience}")
        print(f"    last:         {last_msg}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unread", action="store_true", help="Show only unread threads")
    parser.add_argument("--limit", type=int, default=10, help="Max threads to show (default 10)")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Re-poll every 5 seconds until you Ctrl-C — useful for live testing",
    )
    args = parser.parse_args()

    scope = "unread" if args.unread else None
    last_top_id: int | None = None

    with CanvasClient() as canvas:
        while True:
            convs = canvas.list_conversations(scope=scope, limit=args.limit)
            top_id = convs[0]["id"] if convs else None
            if not args.watch or top_id != last_top_id:
                if args.watch:
                    print("\n" + "=" * 60)
                _render(convs)
                last_top_id = top_id
            if not args.watch:
                break
            time.sleep(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
