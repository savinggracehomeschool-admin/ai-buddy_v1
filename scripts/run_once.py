"""Run the daily nudge job once. Three modes:

  --preview   list candidates only; no Claude API, no DB writes (cheapest preview)
  --dry-run   compose with Claude, write to DB, but skip the actual Canvas send
  (no flag)   compose, write, and SEND for real

Usage:
    uv run python scripts/run_once.py --preview
    uv run python scripts/run_once.py --dry-run
    uv run python scripts/run_once.py
"""

from __future__ import annotations

import argparse
import logging
import sys

from sgeg_nudge.canvas import CanvasClient
from sgeg_nudge.db import (
    TIER_24H,
    TIER_72H,
    TIER_REINFORCE,
    already_nudged,
    get_session,
    init_engine,
)
from sgeg_nudge.nudge import (
    find_24h_candidates,
    find_72h_candidates,
    find_missed_candidates,
    find_new_content_candidates,
    find_reinforce_candidates,
    run_daily_job,
)

# ANSI colour codes — small footprint, no external dep.
_NAVY = "\033[38;5;25m"
_TEAL = "\033[38;5;37m"
_GOLD = "\033[38;5;214m"
_GREY = "\033[38;5;245m"
_RED = "\033[38;5;160m"
_RESET = "\033[0m"
_BOLD = "\033[1m"


def _hr(char: str = "─", width: int = 64) -> str:
    return char * width


def _h1(text: str) -> str:
    return f"\n{_BOLD}{_NAVY}{text}{_RESET}\n{_GREY}{_hr()}{_RESET}"


def _h2(text: str, n: int) -> str:
    pill = f"{_GOLD}{n}{_RESET}" if n else f"{_GREY}{n}{_RESET}"
    return f"\n{_BOLD}{_TEAL}{text}{_RESET}  {pill}"


def _candidate_line(c, dedup: bool) -> str:
    icon = "⤫" if dedup else "→"
    colour = _GREY if dedup else _RESET
    suffix = f"  {_GREY}(already nudged at {c.tier}){_RESET}" if dedup else ""
    return (
        f"  {colour}{icon} learner={c.learner_first_name} ({c.learner_id})"
        f"  course={c.course_id}  assignment={c.assignment_id}"
        f"  lang={c.learner_language}{suffix}{_RESET}"
    )


def _preview() -> int:
    """List candidates per tier without composing or writing to the DB."""
    init_engine()
    print(_h1("SGEG Nudge Engine — preview"))
    print(f"{_GREY}This is a read-only preview. No DB writes. No Claude calls. No sends.{_RESET}")

    total_eligible = 0
    total_dedup = 0

    with CanvasClient() as canvas, get_session() as session:
        for tier_name, finder in (
            ("new", lambda: find_new_content_candidates(canvas, session)),
            ("72h", lambda: find_72h_candidates(canvas, session)),
            ("24h", lambda: find_24h_candidates(canvas, session)),
            ("missed", lambda: find_missed_candidates(canvas, session)),
            ("reinforce", lambda: find_reinforce_candidates(canvas, session)),
        ):
            candidates = list(finder())
            print(_h2(f"Tier {tier_name}", len(candidates)))
            if not candidates:
                print(f"  {_GREY}(no candidates){_RESET}")
                continue
            for c in candidates:
                is_dedup = already_nudged(
                    session,
                    learner_id=c.learner_id,
                    assignment_id=c.assignment_id,
                    tier=c.tier,
                )
                total_eligible += 0 if is_dedup else 1
                total_dedup += 1 if is_dedup else 0
                print(_candidate_line(c, is_dedup))

    print(_h1("Summary"))
    print(f"  would compose:  {_BOLD}{total_eligible}{_RESET}")
    print(f"  dedup-skip:     {_GREY}{total_dedup}{_RESET}")
    print()
    if total_eligible == 0:
        print(f"{_GREY}Nothing to send today.{_RESET}\n")
    else:
        print(f"{_TEAL}Tip:{_RESET} run with {_BOLD}--dry-run{_RESET} to see what each message would say (composes with Claude but doesn't send).\n")
    return 0


def _run(dry_run: bool) -> int:
    init_engine()
    label = "DRY RUN" if dry_run else "LIVE RUN"
    colour = _GOLD if dry_run else _RED
    print(_h1(f"SGEG Nudge Engine — {label}"))
    if dry_run:
        print(f"{_GREY}Composes with Claude and writes pending rows to the DB, but DOES NOT send.{_RESET}")
    else:
        print(f"{colour}This will send real messages via Canvas Conversations.{_RESET}")

    with CanvasClient() as canvas, get_session() as session:
        counts = run_daily_job(canvas, session, dry_run=dry_run)

    print(_h1("Outcome counts"))
    if not counts:
        print(f"  {_GREY}(no candidates){_RESET}")
    else:
        for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            colour = {
                "sent": _TEAL,
                "dry_run": _GOLD,
                "submitted_skipped": _GREY,
                "dedup_skipped": _GREY,
                "held_for_review": "\033[38;5;141m",
                "failed": _RED,
            }.get(k, _RESET)
            print(f"  {colour}{k:<20}{_RESET}  {_BOLD}{v}{_RESET}")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="List candidates only (no Claude, no DB)")
    mode.add_argument("--dry-run", action="store_true", help="Compose + DB writes; skip the Canvas send")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quieten the per-request log lines for prettier preview output.
    logging.getLogger("sgeg_nudge.canvas").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.preview:
        return _preview()
    return _run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
