# Running log of weird things discovered

## 2026-05-21 — Prompt caching threshold

Day 4: caching is enabled in `claude.py` via `cache_control: {"type": "ephemeral"}` on the system prompt block. But `cache_read_input_tokens` came back as 0 for all four test calls, including repeated tiers. Likely reason: our system prompt is ~1690 input tokens and Haiku 4.5's cache activation threshold appears to sit just above that for the system block. As the prompt grows (more examples, course-specific overrides), caching will start to register. Cost impact is negligible for the MVP at ~4 nudges per learner across a 14-day pilot.

Action if costs ever look elevated: expand `docs/02-nudge-prompt.md` past ~2k tokens of pure system content (more worked examples per tier, more edge-case guidance) and re-test.

## 2026-05-21 — Test course 224 is empty

`Developer Test Archived - CAPS Grade 2 English Home Language` is the only course SGEG Assistant is enrolled in for Day 1/2 work, and it has zero assignments in any bucket. `list_submissions` therefore had no live data to exercise on Day 2 — it'll be exercised naturally during Day 3+ work against real pilot courses.
