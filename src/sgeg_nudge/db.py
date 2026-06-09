"""Local SQLite store: nudge records + audit log.

MVP storage. In Phase 1 this becomes Postgres; the models stay the same.

Two tables:
  - nudge:     one row per reminder we composed (sent, failed, or held for review)
  - audit_log: job-level events, for the "audit every action" safety rule

Dedup rule lives here too: see already_nudged().
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# DB_PATH env var lets Render (or any host) store the DB on a persistent disk.
import os as _os
DEFAULT_DB_PATH = Path(_os.environ.get("DB_PATH", str(PROJECT_ROOT / "sgeg.db")))
DEFAULT_DB_URL = f"sqlite:///{DEFAULT_DB_PATH}"

# --- enum-ish constants kept as plain strings so SQLite stays simple --------

TIER_72H = "72h"
TIER_24H = "24h"
TIER_MISSED = "missed"             # assignment past due, still not submitted
TIER_NEW = "new"                   # new content just posted in a course
TIER_REINFORCE = "reinforce"        # positive-reinforcement after a submission
TIER_WEEKLY_PREFIX = "weekly_"      # weekly digest; concrete value e.g. "weekly_2026-W21"

STATUS_PENDING = "pending"                  # composed, send in flight
STATUS_SENT = "sent"                        # confirmed sent via Conversations
STATUS_FAILED = "failed"                    # delivery error — retryable
STATUS_REQUIRES_REVIEW = "requires_review"  # content drift — hold for human
STATUS_SKIPPED = "skipped"                  # late-check showed it was submitted

# Any of these statuses occupies the (learner, assignment, tier) slot and
# prevents another nudge for the same triple. Only STATUS_FAILED is retryable.
DEDUP_STATUSES: tuple[str, ...] = (
    STATUS_PENDING,
    STATUS_SENT,
    STATUS_REQUIRES_REVIEW,
    STATUS_SKIPPED,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Nudge(Base):
    __tablename__ = "nudge"

    id: Mapped[int] = mapped_column(primary_key=True)

    learner_id: Mapped[int] = mapped_column(Integer, index=True)
    learner_name: Mapped[str] = mapped_column(String(200))
    course_id: Mapped[int] = mapped_column(Integer, index=True)
    assignment_id: Mapped[int] = mapped_column(Integer, index=True)
    tier: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=STATUS_PENDING)

    message_text: Mapped[str] = mapped_column(Text)
    canvas_conversation_id: Mapped[Optional[str]] = mapped_column(String(64))
    error_text: Mapped[Optional[str]] = mapped_column(Text)

    composed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (
        # Speeds up the dedup query — not unique because failed rows
        # legitimately repeat (learner_id, assignment_id, tier).
        Index("idx_nudge_dedup", "learner_id", "assignment_id", "tier", "status"),
    )


class CourseConfig(Base):
    """Per-course enable/disable flag. Default = disabled (opt-in for safety).

    A course only receives nudges when an explicit row exists with enabled=True.
    """

    __tablename__ = "course_config"

    course_id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    name: Mapped[Optional[str]] = mapped_column(String(200))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LearnerConfig(Base):
    """Per-learner enable/disable flag. Default = enabled (opt-out per learner).

    If no row exists for a learner, they receive nudges normally (provided
    their course is enabled). Insert a row with enabled=False to opt them out.
    """

    __tablename__ = "learner_config"

    learner_id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    name: Mapped[Optional[str]] = mapped_column(String(200))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Ticket(Base):
    """A learner-question ticket opened on behalf of a student.

    Created whenever the auto-replier decides a question needs human handling
    (escalate, drift, or any non-navigation request). The bot files the ticket
    with the SGEG curriculum team via Canvas Conversations and replies to the
    learner with an acknowledgement, so the learner is never left wondering.
    """

    __tablename__ = "ticket"

    id: Mapped[int] = mapped_column(primary_key=True)

    learner_id: Mapped[int] = mapped_column(Integer, index=True)
    learner_name: Mapped[str] = mapped_column(String(200))
    conversation_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    question: Mapped[str] = mapped_column(Text)
    context_snippet: Mapped[Optional[str]] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(64))  # 'content', 'distress', 'drift', 'off_subject', ...
    urgency: Mapped[str] = mapped_column(String(16), default="normal")  # 'normal' | 'urgent'
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # 'open' | 'closed'

    curriculum_team_conv_id: Mapped[Optional[str]] = mapped_column(String(64))  # where we filed it
    learner_ack_sent: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(32))
    entity_id: Mapped[Optional[int]] = mapped_column(Integer)
    detail: Mapped[Optional[str]] = mapped_column(Text)


# ── AI Buddy LTI + Chat models ───────────────────────────────────────────────

class LTIState(Base):
    """Short-lived OIDC state/nonce pair stored between /lti/login and /lti/launch.

    Canvas sends the state back in the launch POST so we can verify the round-trip
    hasn't been tampered with. Rows expire after 10 minutes.
    """

    __tablename__ = "lti_state"

    state: Mapped[str] = mapped_column(String(200), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(200))
    target_link_uri: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LTISession(Base):
    """Active session created after a successful LTI 1.3 launch.

    The session_id (UUID) is passed as a query-param to the chat page so
    the frontend can authenticate API calls without cookies (iframe-safe).
    """

    __tablename__ = "lti_session"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(200), index=True)
    course_id: Mapped[Optional[str]] = mapped_column(String(200))
    user_name: Mapped[str] = mapped_column(String(300))
    user_email: Mapped[Optional[str]] = mapped_column(String(300))
    roles: Mapped[str] = mapped_column(String(500))
    course_title: Mapped[Optional[str]] = mapped_column(String(500))
    grade_level: Mapped[Optional[int]] = mapped_column(Integer)  # 0=Grade R, 1-12 otherwise
    platform_id: Mapped[str] = mapped_column(String(500))

    # Security scoping — populated at launch time
    # JSON arrays stored as text so SQLite/Postgres both work without migrations.
    enrolled_course_ids: Mapped[Optional[str]] = mapped_column(Text)  # '["224","655","879"]'
    enrolled_account_ids: Mapped[Optional[str]] = mapped_column(Text)  # '[209,216]'  sub-accounts
    launch_account_id: Mapped[Optional[int]] = mapped_column(Integer)  # sub-account of the launch course

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


# ── Phase 2.5 — Canvas Content Index ─────────────────────────────────────────

# Maps Canvas account_id → stream/curriculum name
ACCOUNT_STREAMS: dict[int, str] = {
    208: "Cambridge",
    209: "CAPS",
    210: "Special Needs CAPS",
    211: "Remedial CAPS",
    219: "FET Phase",
    233: "Special Needs",
    239: "Internal Training",
    255: "Senior Phase SNC",
    2:   "Archived",
}


class CanvasContentItem(Base):
    """One indexed row per Canvas content item (module item, assignment, page, etc.).

    Written by the sync job; read by the router for instant content lookups.
    Every row carries its sub-account/stream so Phase 1 scoping is a simple
    WHERE clause — no live Canvas calls required.
    """

    __tablename__ = "canvas_content"

    id: Mapped[int] = mapped_column(primary_key=True)
    canvas_id: Mapped[str] = mapped_column(String(64), index=True)
    item_type: Mapped[str] = mapped_column(String(32), index=True)
    # 'Module' | 'Assignment' | 'Page' | 'File' | 'Quiz' | 'ExternalTool' | 'Discussion'

    title: Mapped[str] = mapped_column(String(500))
    title_search: Mapped[str] = mapped_column(String(500), index=True)
    # Lowercase, punctuation-stripped — used for LIKE queries and Afrikaans matching

    course_id: Mapped[str] = mapped_column(String(64), index=True)
    course_name: Mapped[str] = mapped_column(String(500))
    module_id: Mapped[Optional[str]] = mapped_column(String(64))
    module_name: Mapped[Optional[str]] = mapped_column(String(500))

    account_id: Mapped[int] = mapped_column(Integer, index=True)
    stream: Mapped[Optional[str]] = mapped_column(String(64))   # 'CAPS' | 'Cambridge' | …
    grade_level: Mapped[Optional[int]] = mapped_column(Integer)  # 0=Grade R, 1–12

    language: Mapped[str] = mapped_column(String(8), default="en")  # 'en' | 'af' | 'both'
    canvas_url: Mapped[str] = mapped_column(String(1000))
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    __table_args__ = (
        Index("idx_content_course_search", "course_id", "title_search"),
        Index("idx_content_account_type",  "account_id", "item_type"),
    )


class CanvasSyncLog(Base):
    """One row per sync run — tracks progress and errors."""

    __tablename__ = "canvas_sync_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    courses_synced: Mapped[int] = mapped_column(Integer, default=0)
    items_synced: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="running")
    # 'running' | 'complete' | 'failed'
    error: Mapped[Optional[str]] = mapped_column(Text)


# ── Content index helpers ─────────────────────────────────────────────────────

import re as _re_db


def _normalise(title: str) -> str:
    """Lowercase + strip punctuation for search-friendly comparison."""
    return _re_db.sub(r"[^\w\s]", " ", title.lower())


def search_content(
    session: Session,
    query: str,
    enrolled_course_ids: list[str],
    *,
    item_types: list[str] | None = None,
    grade_level: int | None = None,
    limit: int = 12,
) -> list["CanvasContentItem"]:
    """Full-text search across the local Canvas index, scoped to enrolled courses.

    Splits the query into words and requires ALL words to appear in title_search
    (handles natural language like 'letter A video' or 'die video oor letter A').
    Always filters by enrolled_course_ids — Phase 1 scoping guaranteed.
    """
    words = [w for w in _normalise(query).split() if len(w) > 1]
    if not words:
        return []

    stmt = (
        select(CanvasContentItem)
        .where(
            CanvasContentItem.course_id.in_(enrolled_course_ids),
            CanvasContentItem.is_published == True,  # noqa: E712
        )
    )
    for word in words:
        stmt = stmt.where(CanvasContentItem.title_search.contains(word))
    if item_types:
        stmt = stmt.where(CanvasContentItem.item_type.in_(item_types))
    if grade_level is not None:
        stmt = stmt.where(CanvasContentItem.grade_level == grade_level)

    stmt = stmt.order_by(CanvasContentItem.title_search).limit(limit)
    return list(session.scalars(stmt).all())


class ChatMessage(Base):
    """One turn in an AI Buddy conversation — extended with analytics fields."""

    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(200), index=True)
    course_id: Mapped[Optional[str]] = mapped_column(String(200))
    grade_level: Mapped[Optional[int]] = mapped_column(Integer)   # denormalised for fast queries
    stream: Mapped[Optional[str]] = mapped_column(String(64))     # 'CAPS' | 'Cambridge' | …

    role: Mapped[str] = mapped_column(String(16))   # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    # Claude / token usage
    tokens_in: Mapped[Optional[int]] = mapped_column(Integer)
    tokens_out: Mapped[Optional[int]] = mapped_column(Integer)

    # Analytics
    intent: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    # 'grades'|'due_dates'|'module_content'|'escalation'|'other'
    routed_by: Mapped[Optional[str]] = mapped_column(String(32))
    # 'exact_match'|'router_fast'|'full_claude'
    response_time_ms: Mapped[Optional[int]] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)

    # Safety
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    flag_reason: Mapped[Optional[str]] = mapped_column(String(200))


class ChatTicket(Base):
    """Chat-originated escalation ticket.

    Created when the bot or student escalates. Stores the full conversation
    transcript so staff see context without asking the student to repeat.
    Role-based access: teachers see only their courses; admins see all.
    """

    __tablename__ = "chat_ticket"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Student identity
    user_id: Mapped[str] = mapped_column(String(200), index=True)
    user_name: Mapped[str] = mapped_column(String(300))
    grade_level: Mapped[Optional[int]] = mapped_column(Integer)
    course_id: Mapped[Optional[str]] = mapped_column(String(200))
    course_name: Mapped[Optional[str]] = mapped_column(String(500))
    stream: Mapped[Optional[str]] = mapped_column(String(64))

    # Ticket content
    category: Mapped[str] = mapped_column(String(32), default="other")
    # 'academic'|'technical'|'distress'|'other'
    urgency: Mapped[str] = mapped_column(String(16), default="normal")
    subject: Mapped[Optional[str]] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text)   # student's own words
    transcript: Mapped[Optional[str]] = mapped_column(Text)    # JSON: last N messages

    # Workflow
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    # 'new'|'assigned'|'in_progress'|'resolved'
    assigned_to: Mapped[Optional[str]] = mapped_column(String(200))
    resolution_note: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# --- engine / session plumbing ----------------------------------------------

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def init_engine(url: str = DEFAULT_DB_URL) -> Engine:
    """Build the engine + session factory and create tables if missing.

    SQLite-specific: enables WAL journal mode and a 30-second busy timeout so
    the nightly sync job's bulk writes don't lock out the web server.
    WAL allows concurrent reads while a write is in progress.
    """
    global _engine, _SessionLocal

    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"timeout": 30, "check_same_thread": False}

    _engine = create_engine(url, future=True, connect_args=connect_args)

    # Enable WAL mode for SQLite — allows reads during bulk sync writes
    if url.startswith("sqlite"):
        from sqlalchemy import event, text

        @event.listens_for(_engine, "connect")
        def _set_wal(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA busy_timeout=30000")

    _SessionLocal = sessionmaker(_engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(_engine)
    return _engine


def get_session() -> Session:
    """Open a new session; caller is responsible for closing / committing.

    Typical pattern:
        with get_session() as session:
            ...
            session.commit()
    """
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None  # for type-checkers
    return _SessionLocal()


# --- domain helpers ---------------------------------------------------------

def already_nudged(
    session: Session,
    *,
    learner_id: int,
    assignment_id: int,
    tier: str,
) -> bool:
    """Return True if this (learner, assignment, tier) already has a non-failed row."""
    stmt = (
        select(Nudge.id)
        .where(
            Nudge.learner_id == learner_id,
            Nudge.assignment_id == assignment_id,
            Nudge.tier == tier,
            Nudge.status.in_(DEDUP_STATUSES),
        )
        .limit(1)
    )
    return session.scalar(stmt) is not None


def record_audit(
    session: Session,
    event: str,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    detail: str | None = None,
) -> AuditLog:
    """Append an audit-log row. Caller must commit."""
    entry = AuditLog(
        event=event,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail,
    )
    session.add(entry)
    return entry


# --- per-course / per-learner config helpers --------------------------------

def is_course_enabled(session: Session, course_id: int) -> bool:
    """Opt-in: a course needs an explicit CourseConfig row with enabled=True."""
    cfg = session.get(CourseConfig, course_id)
    return bool(cfg and cfg.enabled)


def is_learner_enabled(session: Session, learner_id: int) -> bool:
    """Opt-out: a learner is enabled by default unless an explicit row disables them."""
    cfg = session.get(LearnerConfig, learner_id)
    return cfg is None or cfg.enabled


def upsert_course_config(
    session: Session,
    course_id: int,
    *,
    enabled: bool,
    name: str | None = None,
    notes: str | None = None,
) -> CourseConfig:
    """Create-or-update a CourseConfig row. Caller commits."""
    cfg = session.get(CourseConfig, course_id)
    if cfg is None:
        cfg = CourseConfig(course_id=course_id, enabled=enabled, name=name, notes=notes)
        session.add(cfg)
    else:
        cfg.enabled = enabled
        if name is not None:
            cfg.name = name
        if notes is not None:
            cfg.notes = notes
        cfg.updated_at = _utcnow()
    return cfg


def session_allows_course(lti: "LTISession", course_id: str) -> bool:
    """Return True if the session's enrollment list includes this course.

    Returns True (permissive) when no enrollment list is stored — this covers
    dev-mode sessions that have non-numeric user IDs and can't pre-fetch
    enrollments.  The hard Canvas API guardrail still applies in all cases.
    """
    import json as _json
    raw = lti.enrolled_course_ids
    if not raw:
        return True   # no list stored → dev mode, defer to Canvas
    try:
        ids: list[str] = _json.loads(raw)
    except Exception:
        return True
    return str(course_id) in ids


def session_allows_account(lti: "LTISession", account_id: int) -> bool:
    """Return True if account_id is within the student's sub-account tree."""
    import json as _json
    raw = lti.enrolled_account_ids
    if not raw:
        return True
    try:
        ids: list[int] = _json.loads(raw)
    except Exception:
        return True
    return account_id in ids


def get_lti_session(session: Session, session_id: str) -> LTISession | None:
    """Return the LTISession if it exists and has not expired."""
    row = session.get(LTISession, session_id)
    if row is None:
        return None
    if row.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        return None
    return row


def upsert_learner_config(
    session: Session,
    learner_id: int,
    *,
    enabled: bool,
    name: str | None = None,
    notes: str | None = None,
) -> LearnerConfig:
    """Create-or-update a LearnerConfig row. Caller commits."""
    cfg = session.get(LearnerConfig, learner_id)
    if cfg is None:
        cfg = LearnerConfig(learner_id=learner_id, enabled=enabled, name=name, notes=notes)
        session.add(cfg)
    else:
        cfg.enabled = enabled
        if name is not None:
            cfg.name = name
        if notes is not None:
            cfg.notes = notes
        cfg.updated_at = _utcnow()
    return cfg
