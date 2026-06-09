"""Poll SGEG Assistant's inbox and reply to learner messages with Claude.

Usage:
    uv run python scripts/run_replier.py                # one-shot, sends real replies
    uv run python scripts/run_replier.py --dry-run      # see what it would do without sending
    uv run python scripts/run_replier.py --watch        # poll every 30s until Ctrl-C
    uv run python scripts/run_replier.py --watch --interval 10

Open Canvas as a student account in another window, message SGEG Assistant,
and watch the bot reply within one poll cycle.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime

from sgeg_nudge.canvas import CanvasClient
from sgeg_nudge.db import get_session
from sgeg_nudge.replier import process_inbox


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="Loop forever (poll every --interval seconds)")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds (default 30)")
    parser.add_argument("--dry-run", action="store_true", help="Compose but don't send replies")
    parser.add_argument("--limit", type=int, default=50, help="Max unread threads per cycle")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.dry_run:
        print("DRY RUN mode — no replies will actually be sent.\n")

    log = logging.getLogger(__name__)
    while True:
        cycle_start = datetime.now().strftime("%H:%M:%S")
        try:
            with CanvasClient() as canvas, get_session() as session:
                counts = process_inbox(canvas, session, dry_run=args.dry_run, limit=args.limit)
            summary = "  ".join(f"{k}={v}" for k, v in counts.items())
            print(f"[{cycle_start}] cycle done — {summary}")
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except Exception as exc:
            # In --watch mode we must NOT exit on transient errors
            # (DNS hiccup, brief network loss, Canvas 5xx, etc.). Log, sleep, retry.
            log.exception("cycle failed")
            print(f"[{cycle_start}] cycle errored: {exc!r} — will retry in {args.interval}s")
            if not args.watch:
                return 1

        if not args.watch:
            return 0

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
