"""Canvas Content Sync — Phase 2.5.

Crawls Canvas through the admin API and writes the full course/module/item
structure into the local `canvas_content` index.

Sync strategy
─────────────
• Full sync   — nightly at 02:00 SAST via APScheduler
• Course sync — on-demand when content is published (POST /api/canvas/sync/course/<id>)
• Staleness   — items older than STALE_HOURS are re-synced on next run

The index is scoped but not access-controlled — every query against it must
filter by the student's enrolled_course_ids (Phase 1). This job just builds
the map; Phase 1 decides who can read which part.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from .config import settings
from .db import (
    ACCOUNT_STREAMS,
    CanvasContentItem,
    CanvasSyncLog,
    _normalise,
    _utcnow,
    get_session,
)

log = logging.getLogger(__name__)

STALE_HOURS = 23   # re-sync items older than this on the next nightly run
_BATCH_COMMIT = 100  # commit every N items to avoid huge transactions


# ── Grade-level extraction (reuse lti.py logic without importing it) ──────────

_GRADE_R_RE = re.compile(r"\bgrade\s+r\b", re.IGNORECASE)
_GRADE_N_RE = re.compile(r"\b(?:grade|gr\.?)\s+(\d{1,2})\b", re.IGNORECASE)


def _grade(text: str | None) -> int | None:
    if not text:
        return None
    if _GRADE_R_RE.search(text):
        return 0
    m = _GRADE_N_RE.search(text)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 12 else None
    return None


def _detect_language(title: str) -> str:
    """Heuristic: flag Afrikaans items by common AF words in titles."""
    af_markers = {"die", "van", "en", "met", "aan", "oor", "vir", "les", "week"}
    words = set(title.lower().split())
    return "af" if words & af_markers else "en"


# ── Core sync functions ───────────────────────────────────────────────────────

def sync_course(course: dict, db_session, *, items_counter: list[int]) -> int:
    """Sync one course — modules + items. Returns count of items upserted."""
    from .canvas import CanvasClient

    course_id  = str(course["id"])
    course_name = course.get("name", "")
    account_id  = int(course.get("account_id", 0))
    stream      = ACCOUNT_STREAMS.get(account_id)
    grade       = _grade(course_name)
    now         = _utcnow()
    count       = 0

    try:
        with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
            modules = c.list_modules(int(course_id))

            for mod in modules:
                mod_id   = str(mod["id"])
                mod_name = mod.get("name", "")

                try:
                    raw_items = c.list_module_items(int(course_id), int(mod_id))
                except Exception as e:
                    log.warning("list_module_items %s/%s: %s", course_id, mod_id, e)
                    raw_items = []

                for item in raw_items:
                    canvas_id  = str(item.get("id", ""))
                    item_type  = item.get("type", "Unknown")
                    title      = item.get("title") or item.get("page_title") or ""
                    canvas_url = item.get("html_url", "")
                    published  = item.get("published", True)

                    if not title or not canvas_url:
                        continue

                    title_search = _normalise(title)
                    lang = _detect_language(title)

                    # Upsert: delete old row for same canvas_id+course_id, insert fresh
                    db_session.query(CanvasContentItem).filter(
                        CanvasContentItem.canvas_id == canvas_id,
                        CanvasContentItem.course_id == course_id,
                    ).delete(synchronize_session=False)

                    db_session.add(CanvasContentItem(
                        canvas_id    = canvas_id,
                        item_type    = item_type,
                        title        = title,
                        title_search = title_search,
                        course_id    = course_id,
                        course_name  = course_name,
                        module_id    = mod_id,
                        module_name  = mod_name,
                        account_id   = account_id,
                        stream       = stream,
                        grade_level  = grade,
                        language     = lang,
                        canvas_url   = canvas_url,
                        is_published = bool(published),
                        synced_at    = now,
                    ))
                    count += 1
                    items_counter[0] += 1

                    # Commit in batches to keep memory usage flat
                    if items_counter[0] % _BATCH_COMMIT == 0:
                        db_session.commit()

    except Exception as e:
        log.warning("sync_course %s failed: %s", course_id, e)

    return count


def run_full_sync(*, force: bool = False) -> CanvasSyncLog:
    """Crawl all published courses across all sub-accounts and rebuild the index.

    Skips courses whose items were synced within STALE_HOURS unless force=True.
    """
    from .canvas import CanvasClient

    log.info("Canvas content sync starting (force=%s)", force)

    with get_session() as db:
        sync_row = CanvasSyncLog(started_at=_utcnow(), status="running")
        db.add(sync_row)
        db.commit()
        sync_id = sync_row.id

    courses_done = 0
    items_counter = [0]  # mutable so sync_course can increment it

    try:
        with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
            # Paginate ALL published courses across the whole instance
            courses = list(c._paginate(
                "/api/v1/accounts/1/courses",
                {"per_page": "100", "published": "true"},
            ))

        log.info("Syncing %d courses", len(courses))

        with get_session() as db:
            stale_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=STALE_HOURS)

            for course in courses:
                course_id = str(course["id"])

                if not force:
                    # Skip if recently synced
                    recent = (
                        db.query(CanvasContentItem)
                        .filter(
                            CanvasContentItem.course_id == course_id,
                            CanvasContentItem.synced_at >= stale_cutoff,
                        )
                        .first()
                    )
                    if recent:
                        continue

                n = sync_course(course, db, items_counter=items_counter)
                courses_done += 1
                if n > 0:
                    log.info("  course %s — %d items", course_id, n)

            db.commit()

            # Mark sync complete
            sync_row = db.get(CanvasSyncLog, sync_id)
            if sync_row:
                sync_row.finished_at    = _utcnow()
                sync_row.courses_synced = courses_done
                sync_row.items_synced   = items_counter[0]
                sync_row.status         = "complete"
            db.commit()

        log.info("Sync complete — %d courses, %d items", courses_done, items_counter[0])

    except Exception as exc:
        log.exception("Sync failed: %s", exc)
        with get_session() as db:
            sync_row = db.get(CanvasSyncLog, sync_id)
            if sync_row:
                sync_row.finished_at = _utcnow()
                sync_row.status      = "failed"
                sync_row.error       = str(exc)[:500]
            db.commit()

    with get_session() as db:
        return db.get(CanvasSyncLog, sync_id)


def sync_one_course(course_id: str) -> dict:
    """On-demand sync for a single course (called after publishing new content)."""
    from .canvas import CanvasClient

    try:
        with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
            course = c.get_course(int(course_id))
    except Exception as e:
        return {"error": f"Could not fetch course {course_id}: {e}"}

    items_counter = [0]
    with get_session() as db:
        n = sync_course(course, db, items_counter=items_counter)
        db.commit()

    log.info("On-demand sync course %s — %d items", course_id, n)
    return {"course_id": course_id, "items_synced": n}


def get_last_sync() -> dict:
    """Return info about the most recent completed sync."""
    with get_session() as db:
        row = (
            db.query(CanvasSyncLog)
            .order_by(CanvasSyncLog.started_at.desc())
            .first()
        )
        if not row:
            return {"status": "never_run"}
        return {
            "status":         row.status,
            "started_at":     row.started_at.isoformat(),
            "finished_at":    row.finished_at.isoformat() if row.finished_at else None,
            "courses_synced": row.courses_synced,
            "items_synced":   row.items_synced,
            "error":          row.error,
        }
