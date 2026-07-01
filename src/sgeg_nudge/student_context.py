"""Assemble a student's Canvas context for injection into the Claude prompt.

Fetches:
  - Course details
  - Modules and their items (with type labels)
  - Page body text (stripped of HTML)
  - Assignment descriptions
  - Upcoming and overdue submissions

Returns a plain-text block embedded verbatim into the AI Buddy system prompt
so Claude always has a live snapshot of the student's course state.

Falls back gracefully when Canvas creds are absent or IDs are non-numeric
(LTI dev mode without Canvas credentials).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

from .config import settings
from .db import LTISession

log = logging.getLogger(__name__)

# Max chars of page/assignment body to include in context — keeps prompt lean
_MAX_PAGE_CHARS = 1500
_MAX_ASSIGN_DESC_CHARS = 600
_MAX_MODULES = 10       # modules to include
_MAX_ITEMS_PER_MODULE = 15
_MAX_PAGES_FETCHED = 5  # how many page bodies to fetch inline


# ── HTML stripping ────────────────────────────────────────────────────────────

class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._parts)).strip()


def _strip_html(html: str | None) -> str:
    if not html:
        return ""
    p = _Stripper()
    try:
        p.feed(html)
        return p.get_text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html or "").strip()


# ── Date helpers ──────────────────────────────────────────────────────────────

def _friendly(iso: str | None) -> str:
    if not iso:
        return "no due date"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%-d %B %Y")
    except Exception:
        return iso[:10]


def _is_overdue(iso: str | None) -> bool:
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt < datetime.now(timezone.utc)
    except Exception:
        return False


# ── Phase label ───────────────────────────────────────────────────────────────

_PHASE_MAP = {
    range(0, 4): "Foundation Phase",
    range(4, 8): "Intermediate Phase",
    range(8, 10): "Senior Phase",
    range(10, 13): "FET Phase",
}


def _phase(grade: int | None) -> str:
    if grade is None:
        return "Unknown Phase"
    for r, label in _PHASE_MAP.items():
        if grade in r:
            return label
    return "Unknown Phase"


# ── Canvas fetching ───────────────────────────────────────────────────────────

def _has_canvas() -> bool:
    return bool(settings.canvas_api_token and settings.canvas_base_url)


def fetch_course_overview(course_id: str) -> dict:
    """Fetch course record, modules, items, and inline page/assignment content.

    Returns a structured dict; never raises — errors are caught and logged.
    """
    result: dict = {
        "course": None,
        "modules": [],
        "pages_fetched": [],
        "assignments_fetched": [],
        "error": None,
    }

    if not _has_canvas():
        result["error"] = "Canvas not configured (dev mode)"
        return result

    try:
        from .canvas import CanvasClient

        with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
            # 1. Course record
            try:
                result["course"] = c.get_course(int(course_id))
            except Exception as e:
                log.warning("get_course(%s) failed: %s", course_id, e)

            # 2. Modules + items
            pages_to_fetch: list[tuple[str, str]] = []   # (page_url, module_name)
            assign_to_fetch: list[tuple[str, str]] = []  # (assignment_id, title)

            try:
                modules = c.list_modules(int(course_id))[:_MAX_MODULES]
                for mod in modules:
                    mod_entry: dict = {
                        "id": mod.get("id"),
                        "name": mod.get("name", "Unnamed Module"),
                        "position": mod.get("position"),
                        "items": [],
                    }
                    try:
                        items = c.list_module_items(int(course_id), mod["id"])
                        for item in items[:_MAX_ITEMS_PER_MODULE]:
                            item_entry = {
                                "title": item.get("title", ""),
                                "type": item.get("type", ""),
                                "position": item.get("position"),
                                "content_id": item.get("content_id"),
                                "url": item.get("html_url", ""),
                            }
                            mod_entry["items"].append(item_entry)

                            # Queue page bodies for inline fetch
                            if item.get("type") == "Page" and item.get("page_url"):
                                if len(pages_to_fetch) < _MAX_PAGES_FETCHED:
                                    pages_to_fetch.append((
                                        item["page_url"],
                                        mod_entry["name"],
                                    ))
                            # Queue assignment descriptions
                            if item.get("type") == "Assignment" and item.get("content_id"):
                                if len(assign_to_fetch) < _MAX_PAGES_FETCHED:
                                    assign_to_fetch.append((
                                        str(item["content_id"]),
                                        item.get("title", ""),
                                    ))
                    except Exception as e:
                        log.warning("list_module_items mod %s: %s", mod.get("id"), e)

                    result["modules"].append(mod_entry)
            except Exception as e:
                log.warning("list_modules(%s) failed: %s", course_id, e)

            # 3. Fetch page bodies
            for page_url, module_name in pages_to_fetch:
                try:
                    page = c.get_page(int(course_id), page_url)
                    body_text = _strip_html(page.get("body", ""))
                    result["pages_fetched"].append({
                        "title": page.get("title", page_url),
                        "module": module_name,
                        "url": page_url,
                        "body_preview": body_text[:_MAX_PAGE_CHARS],
                        "body_length": len(body_text),
                    })
                except Exception as e:
                    log.warning("get_page(%s) failed: %s", page_url, e)

            # 4. Fetch assignment descriptions
            for assign_id, title in assign_to_fetch:
                try:
                    a = c.get_assignment_details(int(course_id), int(assign_id))
                    desc = _strip_html(a.get("description", ""))
                    result["assignments_fetched"].append({
                        "id": assign_id,
                        "title": a.get("name", title),
                        "due_at": a.get("due_at"),
                        "due_friendly": _friendly(a.get("due_at")),
                        "points_possible": a.get("points_possible"),
                        "description_preview": desc[:_MAX_ASSIGN_DESC_CHARS],
                    })
                except Exception as e:
                    log.warning("get_assignment_details(%s) failed: %s", assign_id, e)

    except Exception as e:
        log.exception("fetch_course_overview error: %s", e)
        result["error"] = str(e)

    return result


def _extract_term(text: str | None) -> str | None:
    """Extract a term label from a group/module name e.g. 'Term 2 Assignments' → 'Term 2'."""
    if not text:
        return None
    m = re.search(r'\bterm\s*([1-4])\b', text, re.IGNORECASE)
    if m:
        return f"Term {m.group(1)}"
    return None


def _term_from_due_date(due_at: str | None) -> str | None:
    """Classify a due date into a SA school term as a fallback."""
    if not due_at:
        return None
    try:
        dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        month, day = dt.month, dt.day
        if (month == 1 and day >= 15) or month in (2, 3) or (month == 4 and day <= 4):
            return "Term 1"
        if (month == 4 and day >= 5) or month in (5, 6) or (month == 7 and day <= 4):
            return "Term 2"
        if (month == 7 and day >= 5) or month in (8, 9) or (month == 10 and day <= 3):
            return "Term 3"
        if (month == 10 and day >= 4) or month in (11, 12):
            return "Term 4"
    except Exception:
        pass
    return None


def _fetch_assignments(canvas_user_id: str, course_id: str) -> list[dict]:
    """Return all assignments for a course with accurate per-student submission status.

    Strategy:
    1. Fetch every assignment in the course (one paginated call).
    2. Fetch every submission for this student in the course in bulk (one paginated
       call to /courses/:id/students/submissions). This is the authoritative record —
       no N+1 per-assignment checks, no silent failures.
    3. Cross-reference: an assignment is outstanding if its due date has passed and
       the student has not submitted (submitted_at is null) and it is not excused.
    4. Upcoming = due in the future and not yet submitted.
    """
    if not _has_canvas():
        return []

    from .canvas import CanvasClient

    assignments: list[dict] = []
    try:
        with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:

            # ── Term context maps ─────────────────────────────────────────────
            group_term: dict[int, str] = {}
            try:
                for g in c.list_assignment_groups(int(course_id)):
                    t = _extract_term(g.get("name", ""))
                    if t:
                        group_term[int(g["id"])] = t
            except Exception:
                pass

            module_term: dict[int, str] = {}
            try:
                for mod in c.list_modules(int(course_id)):
                    t = _extract_term(mod.get("name", ""))
                    if t:
                        for item in c.list_module_items(int(course_id), mod["id"]):
                            if item.get("type") in ("Assignment", "Quiz"):
                                cid = item.get("content_id")
                                if cid:
                                    module_term[int(cid)] = t
            except Exception:
                pass

            # ── Step 1: all assignments ───────────────────────────────────────
            all_assignments = c.list_assignments(int(course_id))

            # ── Step 2: all submissions for this student (bulk) ───────────────
            submission_map: dict[int, dict] = {}
            try:
                for sub in c.get_student_submissions_bulk(int(course_id), int(canvas_user_id)):
                    aid = sub.get("assignment_id")
                    if aid is not None:
                        submission_map[int(aid)] = sub
            except Exception as e:
                log.warning("bulk submission fetch failed, falling back: %s", e)

            # ── Step 3: cross-reference ───────────────────────────────────────
            now = datetime.now(timezone.utc)
            for a in all_assignments:
                aid = int(a["id"])
                sub = submission_map.get(aid, {})

                excused = bool(sub.get("excused"))
                submitted_at = sub.get("submitted_at")
                submitted = bool(submitted_at)
                canvas_missing = bool(sub.get("missing"))  # Canvas's own flag
                workflow = sub.get("workflow_state", "unsubmitted")

                due_at = a.get("due_at")
                past_due = _is_overdue(due_at)

                # Outstanding = past due, not submitted, not excused
                # Also trust Canvas's own `missing` flag as a fallback
                overdue = (past_due and not submitted and not excused) or (canvas_missing and not excused)
                # Upcoming = not yet submitted, not excused (includes assignments with no due date)
                upcoming = not past_due and not submitted and not excused

                if submitted or excused:
                    continue  # done — skip

                group_id = a.get("assignment_group_id")
                term = (
                    (group_id and group_term.get(int(group_id)))
                    or module_term.get(aid)
                    or _term_from_due_date(due_at)
                )

                assignments.append({
                    "id": aid,
                    "name": a.get("name", "Unnamed"),
                    "due_at": due_at,
                    "due_friendly": _friendly(due_at),
                    "overdue": overdue,
                    "submitted": submitted,
                    "excused": excused,
                    "canvas_missing": canvas_missing,
                    "workflow_state": workflow,
                    "points_possible": a.get("points_possible"),
                    "term": term,
                    "assignment_group": group_id,
                    "url": a.get("html_url", ""),
                })

    except Exception as e:
        log.warning("_fetch_assignments error: %s", e)

    seen: set = set()
    unique = []
    for a in assignments:
        if a["id"] not in seen:
            seen.add(a["id"])
            unique.append(a)
    return unique[:50]


# ── Context builder ───────────────────────────────────────────────────────────

def build_student_context(session: LTISession) -> str:
    """Return the full plain-text context block to embed in the AI Buddy system prompt."""
    lines: list[str] = []

    grade_str = (
        "Grade R" if session.grade_level == 0
        else f"Grade {session.grade_level}" if session.grade_level is not None
        else "Unknown grade"
    )
    phase = _phase(session.grade_level)
    first_name = session.user_name.split()[0] if session.user_name else "Student"

    lines.append(f"STUDENT: {session.user_name}  (Canvas user ID: {session.user_id})")
    lines.append(f"YEAR LEVEL: {grade_str} ({phase})")
    lines.append(f"COURSE: {session.course_title or 'Unknown'}  (course ID: {session.course_id})")
    lines.append("")

    canvas_user_id = session.user_id or ""
    course_id = session.course_id or ""
    has_numeric_ids = (
        re.match(r"^\d+$", course_id) and
        re.match(r"^\d+$", canvas_user_id)
    )

    # ── Assignments ───────────────────────────────────────────────────────────
    if has_numeric_ids:
        assignments = _fetch_assignments(canvas_user_id, course_id)
        overdue   = [a for a in assignments if a["overdue"]]
        no_date   = [a for a in assignments if not a["overdue"] and not a.get("due_at")]
        upcoming  = [a for a in assignments if not a["overdue"] and a.get("due_at")]

        if overdue:
            lines.append(f"OVERDUE / MISSING WORK ({len(overdue)} assignments):")
            for a in overdue:
                term_label = f" [{a['term']}]" if a.get("term") else ""
                url_label  = f"  → {a['url']}" if a.get("url") else ""
                lines.append(f"  • {a['name']}{term_label} — was due {a['due_friendly']}{url_label}")
            lines.append("")

        if upcoming:
            lines.append(f"UPCOMING ASSIGNMENTS — NOT YET SUBMITTED ({len(upcoming)}):")
            for a in upcoming:
                term_label = f" [{a['term']}]" if a.get("term") else ""
                url_label  = f"  → {a['url']}" if a.get("url") else ""
                lines.append(f"  • {a['name']}{term_label} — due {a['due_friendly']}{url_label}")
            lines.append("")

        if no_date:
            lines.append(f"UNSUBMITTED — NO DUE DATE SET ({len(no_date)}):")
            for a in no_date:
                term_label = f" [{a['term']}]" if a.get("term") else ""
                url_label  = f"  → {a['url']}" if a.get("url") else ""
                lines.append(f"  • {a['name']}{term_label}{url_label}")
            lines.append("")

        if not overdue and not upcoming and not no_date:
            lines.append("All assignments submitted — nothing outstanding.")
            lines.append("")

    # ── Course modules ────────────────────────────────────────────────────────
    if has_numeric_ids and _has_canvas():
        overview = fetch_course_overview(course_id)

        if overview.get("modules"):
            lines.append("COURSE MODULES (include the Canvas URL as a markdown link when directing a student to any item):")
            for mod in overview["modules"]:
                lines.append(f"  Module: {mod['name']}")
                for item in mod.get("items", []):
                    url = item.get("url", "")
                    if url:
                        lines.append(f"    [{item['type']}] {item['title']} → {url}")
                    else:
                        lines.append(f"    [{item['type']}] {item['title']}")
            lines.append("")

        if overview.get("pages_fetched"):
            lines.append("COURSE PAGE CONTENT (excerpts):")
            for pg in overview["pages_fetched"]:
                lines.append(f"  ── {pg['title']} (in: {pg['module']}) ──")
                if pg["body_preview"]:
                    lines.append(f"  {pg['body_preview'][:800]}")
            lines.append("")

        if overview.get("assignments_fetched"):
            lines.append("ASSIGNMENT DETAILS:")
            for a in overview["assignments_fetched"]:
                desc = a.get("description_preview", "")
                lines.append(
                    f"  • {a['title']} — due {a['due_friendly']}"
                    + (f" ({a['points_possible']} pts)" if a.get("points_possible") else "")
                )
                if desc:
                    lines.append(f"    Description: {desc[:400]}")
            lines.append("")

    if not has_numeric_ids:
        lines.append("Canvas data unavailable in dev mode (IDs are non-numeric).")
        lines.append("")

    lines.append(f"Address the student as: {first_name}")
    return "\n".join(lines)
