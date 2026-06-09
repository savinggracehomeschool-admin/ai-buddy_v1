# SGEG Nudge Engine — session snapshot

Generated end of working session on 2026-05-21.

## Status

Feature-complete MVP plus auto-reply + tickets + admin UI.
**12 git commits** on `main`. Working tree clean as of the last commit.

```
17df709  Route navigation-reply fallback through the ticket system
c966117  Add Tickets panel to /admin
0abfbe6  Open tickets with the curriculum team on the learner's behalf
c7c800b  Terminology: SGEG provides curriculum support, not classes
06ae193  Fix drift false-positive on 'Grade N' year-level phrasing
7e54453  Tighten reply scope to navigation + add missed-submission tier
ce37a58  Fix: mark-read after own-latest skip in replier
c71f23d  Polish: tighten NOREPLY rule + add preview mode to run_once
8879413  Day 9: admin alerts, 5xx retry, structured rotating logs
06099b3  Days 7-8: admin web view + per-course/per-learner enable toggles
012b815  Days 1-6: SGEG Canvas Nudge Engine MVP foundation
```

## What's built

- Canvas client with pagination, 429 backoff, 5xx retry, admin-alerted halt
- Claude Haiku 4.5 nudge composition (EN + AF), drift detection
- Auto-reply with navigation-only scope, enrolment-checked, ticket-routed escalation
- Daily orchestration: 72h / 24h / missed / reinforce tiers + safety re-check + dedup
- APScheduler (08:00 SAST daily + replier every 5 min)
- `/admin` web UI with basic auth, course/learner toggles, tickets panel
- SQLite audit log + ticket DB + admin alerts via Canvas Conversations
- 68 passing tests

## How to run

```sh
# Admin web UI
uv run uvicorn sgeg_nudge.main:app --port 8000

# Preview tomorrow's candidates (no Claude, no DB)
uv run python scripts/run_once.py --preview

# Dry-run daily job (composes, no send)
uv run python scripts/run_once.py --dry-run

# Live daily job (one shot)
uv run python scripts/run_once.py

# Replier in watch mode (polls inbox every N seconds)
uv run python scripts/run_replier.py --watch --interval 15

# Full scheduler (daily nudge + replier loop)
uv run python -m sgeg_nudge.scheduler
```

## Pre-pilot checklist

- [ ] Set `CURRICULUM_TEAM_CANVAS_USER_ID` in `.env` (where tickets land)
- [ ] Set `ADMIN_CANVAS_USER_ID` in `.env` (where failure alerts go)
- [ ] Change `ADMIN_PASSWORD` from temporary `sgeg-pilot-2026`
- [ ] Enrol SGEG Assistant in real pilot courses on Canvas
- [ ] Enable those courses in `/admin` (default OFF)
- [ ] Collect manual POPIA consent from pilot families
- [ ] Final `uv run python scripts/run_once.py --preview` smoke check
- [ ] Start scheduler in own terminal (or background with nohup)

## Spec item not yet built

**Parent CC by grade (R–7).** Original spec said: Grade R–7 CC parent, Grade 8–12 do not. Needs to know where "grade" lives in your Canvas data (section name pattern? custom user field?). Skip if pilot is Grade 8–12 only.

## Out-of-MVP, deferred

LTI 1.3, Canvas Data 2 / DAP, WhatsApp/SMS/email, RAG, AWS deployment, Docker, formal POPIA consent UI, multi-tenant, React frontend.

## Notable design decisions made in this session

- **Auto-reply pulled into MVP scope** (was Phase 2 per spec). Dee's actual intent was bidirectional from day one.
- **Courses are opt-in** (default OFF) so daily job ignores until explicitly enabled.
- **Tickets file with curriculum team automatically** on escalate — bot files, learner gets acknowledgement. No "tell the team yourself" guidance anywhere.
- **SGEG = curriculum + support, not classes.** No teachers, no class times. Use "the SGEG curriculum team" everywhere.
- **Drift detector** tightened to evaluation-context phrasing only; "Grade 2 Mathematics" no longer false-positives.

## Source-of-truth files

- Day-by-day spec: `build-copilot-prompt.md.docx`
- Nudge prompt: `docs/02-nudge-prompt.md`
- Reply prompt: `docs/03-reply-prompt.md`
- Running log of discoveries: `docs/notes.md`
- This file: `SESSION-NOTES.md`
