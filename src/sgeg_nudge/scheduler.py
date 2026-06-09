"""APScheduler wiring — runs the daily nudge job + the inbox replier loop.

Two jobs:
  - daily_nudge: 08:00 SAST every day, runs run_daily_job()
  - replier_loop: every 5 minutes, runs process_inbox()

Run with:  uv run python -m sgeg_nudge.scheduler
Ctrl-C to stop.
"""

from __future__ import annotations

import logging
import traceback
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from sgeg_nudge.alerts import send_alert_to_admin
from sgeg_nudge.canvas import CanvasClient
from sgeg_nudge.db import get_session, init_engine
from sgeg_nudge.logging_setup import setup_logging
from sgeg_nudge.nudge import run_daily_job
from sgeg_nudge.replier import process_inbox
from sgeg_nudge.sync import run_full_sync

log = logging.getLogger(__name__)
SAST = ZoneInfo("Africa/Johannesburg")


def _run_daily_safe() -> None:
    init_engine()
    try:
        with CanvasClient() as canvas, get_session() as session:
            counts = run_daily_job(canvas, session)
            log.info("daily_job done: %s", counts)
    except Exception as exc:
        log.exception("daily_job crashed")
        send_alert_to_admin(
            "Daily nudge job crashed",
            f"The daily nudge job raised an exception and did not complete.\n\n"
            f"{exc!r}\n\n"
            f"{traceback.format_exc()[-1500:]}",
        )


def _run_replier_safe() -> None:
    init_engine()
    try:
        with CanvasClient() as canvas, get_session() as session:
            counts = process_inbox(canvas, session)
            log.info("replier done: %s", counts)
    except Exception as exc:
        log.exception("replier crashed")
        send_alert_to_admin(
            "Inbox replier crashed",
            f"The inbox-reply cycle raised an exception.\n\n"
            f"{exc!r}\n\n"
            f"{traceback.format_exc()[-1500:]}",
        )


def _run_canvas_sync_safe() -> None:
    """Nightly Canvas content sync — rebuilds the local course/module/item index."""
    init_engine()
    try:
        result = run_full_sync()
        log.info(
            "canvas_sync done: %d courses, %d items",
            result.courses_synced if result else 0,
            result.items_synced   if result else 0,
        )
    except Exception as exc:
        log.exception("canvas_sync crashed")
        send_alert_to_admin(
            "Canvas content sync crashed",
            f"The nightly content index sync failed.\n\n{exc!r}\n\n"
            f"{traceback.format_exc()[-1500:]}",
        )


def make_scheduler() -> BlockingScheduler:
    sched = BlockingScheduler(timezone=SAST)
    sched.add_job(
        _run_daily_safe,
        trigger=CronTrigger(hour=8, minute=0),
        id="daily_nudge",
        name="SGEG daily nudge job (08:00 SAST)",
    )
    sched.add_job(
        _run_replier_safe,
        trigger=IntervalTrigger(minutes=5),
        id="replier_loop",
        name="SGEG inbox replier (every 5 min)",
    )
    sched.add_job(
        _run_canvas_sync_safe,
        trigger=CronTrigger(hour=2, minute=0),
        id="canvas_sync",
        name="Canvas content index sync (02:00 SAST)",
    )
    return sched


def main() -> None:
    setup_logging()
    sched = make_scheduler()
    log.info("Scheduler starting. Jobs:")
    for job in sched.get_jobs():
        log.info("  %s -> next: %s", job.name, job.next_run_time)
    log.info("Ctrl-C to stop.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
