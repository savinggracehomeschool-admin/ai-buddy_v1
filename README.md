# SGEG Canvas Nudge Engine

An assistant that helps SGEG learners stay on top of their work. It runs daily
to nudge learners about assignments that are due soon, recently posted, or
overdue — and it answers their Canvas questions in real time. Every reply is
either Canvas navigation, a how-to-use-Canvas tip, or an automatic ticket to
the SGEG curriculum team.

Built for a 2–3 week local pilot. Phase 1 moves to AWS af-south-1.

---

## Quick start

```sh
# 1. Install uv (one-time):
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Copy and fill .env.example -> .env. See the section below.
cp .env.example .env
$EDITOR .env

# 3. Smoke-test the Canvas connection:
uv run python scripts/test_canvas.py

# 4. Preview tomorrow's daily-job candidates (no Claude, no DB writes):
uv run python scripts/run_once.py --preview

# 5. Start the admin web UI (separate terminal):
uv run uvicorn sgeg_nudge.main:app --port 8000
#    open http://localhost:8000/admin

# 6. Start the production loop (separate terminal):
uv run python -m sgeg_nudge.scheduler
```

---

## What it does

### Outbound nudges (daily job, 08:00 SAST)

For every learner in every enabled course:

| Tier | Trigger | Tone |
|---|---|---|
| `new` | New assignment posted in the last 7 days, still has a future due date | Heads-up, welcoming |
| `72h` | Assignment due in 24–72 h, no submission | Gentle reminder |
| `24h` | Assignment due in 0–24 h, no submission | Slightly more urgency |
| `missed` | Assignment past due in the last 14 days, no submission | Kind catch-up, no shame |
| `weekly_*` | Once-per-ISO-week consolidated rundown of outstanding work | Warm summary with bulleted list |
| `reinforce` | Learner submitted after we'd nudged them | Brief celebratory thank-you |

Composed by Claude Haiku 4.5 (~50–80 words, EN or AF based on locale). Sent
via Canvas Conversations from the **SGEG Assistant** account. Per-learner /
per-assignment / per-tier dedup so no one gets the same nudge twice. Safety
re-check right before send — never nudges about an assignment that was just
submitted.

### Inbound auto-reply (conservative)

When a learner messages SGEG Assistant on Canvas, the bot replies if the
question fits one of **two** narrow categories:

1. **Navigation** — *"where is my [thing]"* — subject-specific, requires the
   learner be enrolled in that subject.
2. **Canvas-usage how-to** — *"how do I [submit / message / find To Do / …]"* —
   generic Canvas-platform questions, no enrolment check.

Anything else (content questions, definitions, solutions, off-subject,
distress, grade values, manipulation attempts) triggers an automatic
**ticket** to the SGEG curriculum team. The learner gets an acknowledgement
in the same thread: *"I've passed your question on to the SGEG curriculum
team — they'll come back to you here soon."*

### Admin web UI (`/admin`, HTTP Basic auth)

- Enable / disable courses (opt-in, default off — daily job ignores a course
  until you flip the switch)
- Opt out individual learners (default on)
- View, filter, and close tickets, with click-through to both the learner's
  Canvas thread and the team's
- Per-learner journey page at `/admin/learner/{user_id}` with enrolment,
  Canvas-reported missing submissions, nudge history, tickets, and recent
  inbox activity
- Recent nudges and audit-log views

### Failure handling

- Canvas 429 backoff (2/4/8/16/60 s with `Retry-After` respect)
- Canvas 5xx retry (1/3/7 s)
- Halt + admin-alert (via Canvas Conversations) after 5 consecutive 429s
- Drift detection on every Claude output — anything mentioning URLs, emails,
  phones, grade values, parents, comparisons to other learners is held for
  review, never sent
- Auto-close stale tickets after 14 days no resolution

---

## Configuration (`.env`)

| Key | Purpose |
|---|---|
| `CANVAS_BASE_URL` | Your Canvas host, e.g. `https://savinggraceeducationgroup.instructure.com` |
| `CANVAS_API_TOKEN` | SGEG Assistant's long-lived API token (admin scope) |
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude Haiku 4.5 |
| `ADMIN_USER` | HTTP Basic username for `/admin` |
| `ADMIN_PASSWORD` | HTTP Basic password for `/admin` (rotate before pilot) |
| `ADMIN_CANVAS_USER_ID` | Canvas user id where system failure alerts land |
| `CURRICULUM_TEAM_CANVAS_USER_ID` | Canvas user id where learner tickets land (falls back to `ADMIN_CANVAS_USER_ID`) |

`.env` is gitignored. Never commit it.

---

## Scripts

| Script | What it does |
|---|---|
| `scripts/run_once.py --preview` | Lists tomorrow's candidates per tier. No Claude calls, no DB writes. Cheapest sanity check. |
| `scripts/run_once.py --dry-run` | Composes nudges with Claude and writes pending DB rows, but doesn't send through Canvas. |
| `scripts/run_once.py` | Runs the daily job for real — composes and sends. |
| `scripts/run_replier.py --watch` | Polls SGEG Assistant's Canvas inbox every N seconds and replies / escalates / opens tickets. |
| `scripts/peek_inbox.py` | Read-only inbox lookahead. |
| `scripts/test_canvas.py` | Day-1 smoke test — confirm Canvas auth works. |
| `scripts/test_canvas_client.py` | Day-2 exercise — courses → assignments → submissions through the rate-limit-aware client. |
| `scripts/test_claude.py` | Day-4 exercise — compose nudges in each tier and print them. |
| `scripts/test_send.py <user_id>` | Day-5 exercise — compose + send a real Canvas message to a specific user, with a confirmation prompt. |

Run the full scheduler (daily job + inbox replier loop in one process):

```sh
uv run python -m sgeg_nudge.scheduler
```

---

## Project layout

```
src/sgeg_nudge/
├── __init__.py
├── admin.py        # FastAPI /admin routes + HTML rendering
├── alerts.py       # Admin failure alerts via Canvas Conversations
├── canvas.py       # Canvas REST client with rate-limit + 429/5xx retry
├── claude.py       # Claude Haiku 4.5 nudge + digest composition with drift detection
├── config.py       # Settings loaded from .env
├── db.py           # SQLAlchemy models: Nudge, Ticket, AuditLog, CourseConfig, LearnerConfig
├── logging_setup.py# Rotating file + console logging
├── main.py         # FastAPI app entry point
├── nudge.py        # Candidate finders for each tier + daily job orchestration
├── replier.py      # Inbox processor: enrolment-aware, ticket-routing escalation
├── scheduler.py    # APScheduler wrapper (daily nudge + replier loop)
└── tickets.py      # Ticket creation, close, auto-close-stale

scripts/            # One-off / operational scripts (see table above)
tests/              # pytest unit tests (>75 cases)
docs/
├── 02-nudge-prompt.md    # Claude system prompt for outbound nudges (all tiers)
├── 03-reply-prompt.md    # Claude system prompt for inbound auto-replies
├── notes.md              # Running log of weird things discovered
build-copilot-prompt.md.docx  # Original day-by-day build spec
SESSION-NOTES.md           # End-of-session snapshot
```

---

## Pre-pilot checklist

- [ ] `.env` populated with all 7 keys (see table above)
- [ ] `ADMIN_PASSWORD` rotated from any temporary value
- [ ] SGEG Assistant enrolled as a Teacher in pilot courses (Canvas UI)
- [ ] Those courses enabled in `/admin` (default OFF)
- [ ] Manual POPIA consent collected from pilot families
- [ ] `uv run python scripts/run_once.py --preview` shows expected candidate counts
- [ ] Scheduler running in a terminal (or `nohup ... &`)
- [ ] Admin UI accessible at `/admin`

---

## Out of scope (deferred)

- Parent CC by grade band (R–7 CC, 8–12 no) — needs Canvas-side "grade" mapping
- LTI 1.3 launch handler
- Canvas Data 2 / DAP ingestion
- WhatsApp / SMS / email channels (Canvas-only)
- RAG / retrieval over course content
- AWS deployment, Docker, Terraform
- Formal POPIA consent UI
- Multi-tenant or per-staff customisation
- React / SPA frontend
- Mobile-responsive admin polish

See `build-copilot-prompt.md.docx` for the original day-by-day plan and the
full spec context.
