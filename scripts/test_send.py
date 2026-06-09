"""Day 5 test: compose a nudge and send it via Canvas Conversations.

This is the FIRST script that actually delivers a message to a Canvas user.
There is no default recipient — you must pass the Canvas user ID on the CLI,
and the script will look up the recipient's name and ask for confirmation
before sending.

Usage:
    uv run python scripts/test_send.py <canvas-user-id>
    uv run python scripts/test_send.py <canvas-user-id> --tier 24h --first-name Dee
    uv run python scripts/test_send.py <canvas-user-id> --yes      # skip confirm

Find your user ID by visiting your profile in Canvas; the URL is:
    https://savinggraceeducationgroup.instructure.com/users/<NNNN>
"""

from __future__ import annotations

import argparse
import logging
import sys

from sgeg_nudge.canvas import CanvasClient
from sgeg_nudge.claude import compose_nudge
from sgeg_nudge.db import STATUS_PENDING

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "recipient_user_id",
        help="Canvas user ID to send the test nudge to (numeric, from a Canvas profile URL)",
    )
    parser.add_argument("--first-name", default="Dee", help="First name to use in the message")
    parser.add_argument(
        "--assignment-name",
        default="Sample assignment for Day 5 send test",
    )
    parser.add_argument("--tier", default="72h", choices=("72h", "24h", "reinforce"))
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the recipient-confirmation prompt (use only after a dry run)",
    )
    args = parser.parse_args()

    with CanvasClient() as canvas:
        # 1. Who are we sending FROM?
        me = canvas.whoami()
        print(f"Sender: id={me.get('id')} name={me.get('name')!r}")
        if "Assistant" not in (me.get("name") or ""):
            print(
                "  WARNING: sender name doesn't contain 'Assistant'. "
                "Make sure CANVAS_API_TOKEN belongs to the SGEG Assistant user.",
            )

        # 2. Who are we sending TO?
        try:
            recipient = canvas.lookup_user(args.recipient_user_id)
        except Exception as exc:
            print(f"\nCouldn't look up recipient user {args.recipient_user_id}: {exc!r}", file=sys.stderr)
            return 1
        print(f"Recipient: id={recipient.get('id')} name={recipient.get('name')!r}")
        print(f"           login_id={recipient.get('login_id')!r}")

        if not args.yes:
            ans = input("\nSend the nudge to this person? [y/N] ").strip().lower()
            if ans != "y":
                print("Aborted.")
                return 0

        # 3. Compose with Claude.
        print("\nComposing nudge with Claude…")
        nudge = compose_nudge(
            learner_first_name=args.first_name,
            assignment_name=args.assignment_name,
            due_at_friendly="Friday at 5pm",
            tier=args.tier,
            language="en",
        )
        print(f"  status:        {nudge.status}")
        if nudge.status != STATUS_PENDING:
            print(f"  drift_reasons: {nudge.drift_reasons}")
            print("\nAborting — drift detected. In production this would be saved as requires_review.")
            return 1
        print(f"  body:\n    {nudge.text}")

        # 4. Send.
        print("\nSending via Canvas Conversations…")
        conv = canvas.send_conversation(
            recipient_ids=[args.recipient_user_id],
            body=nudge.text,
            subject=f"Reminder: {args.assignment_name}",
        )
        print(f"  conversation_id: {conv.get('id')}")
        print(f"  workflow_state:  {conv.get('workflow_state')}")
        print(f"\nDone. Open Canvas → Inbox as {recipient.get('name')} to verify receipt.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
