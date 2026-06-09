"""Day 4 test: compose nudges with Claude Haiku 4.5 and print them.

Exercises all three tiers (72h, 24h, reinforce) plus an Afrikaans variant.
Does NOT send anything to Canvas — this is draft mode.

Watch the token counts on the second+ calls: the cache_read_input_tokens
field should be > 0, confirming prompt caching is working.

Run with:  uv run python scripts/test_claude.py
"""

from __future__ import annotations

import sys

from sgeg_nudge.claude import compose_nudge


CASES: list[dict] = [
    {
        "tier": "72h",
        "learner_first_name": "Thandi",
        "assignment_name": "Phonics Week 3 — Letter Sounds",
        "due_at_friendly": "Friday at 5pm",
        "language": "en",
    },
    {
        "tier": "24h",
        "learner_first_name": "Liam",
        "assignment_name": "Maths — Fractions Practice 2",
        "due_at_friendly": "tomorrow at 4pm",
        "language": "en",
    },
    {
        "tier": "reinforce",
        "learner_first_name": "Aiden",
        "assignment_name": "Reading Comprehension — The Lighthouse",
        "due_at_friendly": "today",
        "language": "en",
    },
    {
        "tier": "72h",
        "learner_first_name": "Annelie",
        "assignment_name": "Afrikaans Skryfwerk — My Gunstelingdier",
        "due_at_friendly": "Vrydag om 17:00",
        "language": "af",
    },
]


def main() -> int:
    for i, case in enumerate(CASES, start=1):
        print(f"\n========== {i}. tier={case['tier']} lang={case['language']} ==========")
        try:
            n = compose_nudge(**case)
        except Exception as exc:
            msg = str(exc)
            print(f"ERROR: {exc!r}", file=sys.stderr)
            if "401" in msg or "authentication" in msg.lower() or "invalid_api_key" in msg.lower():
                print(
                    "\nLooks like the Anthropic API key is rejected.\n"
                    "Go to https://console.anthropic.com/settings/keys, "
                    "create a new key, and paste it into .env as ANTHROPIC_API_KEY.",
                    file=sys.stderr,
                )
            return 1

        print(n.text)
        print()
        print(f"  status:        {n.status}")
        if n.drift_reasons:
            print(f"  drift_reasons: {n.drift_reasons}")
        print(
            f"  tokens: input={n.usage_input_tokens} "
            f"output={n.usage_output_tokens} "
            f"cache_create={n.cache_creation_input_tokens} "
            f"cache_read={n.cache_read_input_tokens}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
