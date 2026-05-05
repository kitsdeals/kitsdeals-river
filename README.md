# kitsdeals-river

Python SDK for consuming the [Kit's Deals](https://kitsdeals.com) river — a
real-time, structured stream of approved deals built for agent consumers.

> **The interaction model is agent-mediated.** You're not expected to edit
> YAML by hand. You tell your Claude/Code agent something like *"set up
> github.com/paraffinendeavors/kitsdeals-river for me — I'm looking for TVs
> and a laptop refresh"*, and the agent runs the onboarding skill (PR 37) to
> translate the conversation into a `~/.kitsdeals/profile.yaml` plus a
> running watcher daemon. The CLI commands and library API exist so the
> agent has reliable surfaces to call; humans rarely use them directly.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  RiverWatcher                                                    │
│    SSE consumer (Kit's Deals river v2)                           │
│    ↓                                                             │
│    Reconnect with exponential backoff                            │
│    Cursor persistence (~/.kitsdeals/cursor.json)                 │
│    Two-step catch-up: REST for gaps, SSE for forward             │
│    ↓                                                             │
│  Handler  (your code or the spawn_claude_evaluator from PR 37)   │
│    ↓                                                             │
│  Profile           DecisionsLog        Notifier                  │
│  ~/.kitsdeals/     ~/.kitsdeals/       telegram / stdout / …     │
│   profile.yaml      decisions.jsonl                              │
└──────────────────────────────────────────────────────────────────┘
```

## Status

| Component | Status |
|---|---|
| `RiverWatcher` — SSE + reconnect + cursor + filter + SIGHUP reload | ✓ |
| `Profile` — Pydantic schema, YAML load/save, dedup helpers | ✓ |
| `DecisionsLog` — JSONL append-only with size-based rotation | ✓ |
| `Notifier` — protocol + `StdoutNotifier` + `TelegramNotifier` | ✓ |
| `spawn_claude_evaluator` — agent-wake-up with structured prompt | ✓ |
| `skills/onboarding.md` — agent-facing setup interview | ✓ |
| `skills/update-profile.md` — agent-facing ongoing refinement | ✓ |
| CLI (`setup`, `run`, `reload`, `status`, `profile show/update/validate`) | ✓ |
| `slack`, `discord`, `http_post`, `subprocess` notifiers | ☐ PR 38 |
| Server-side `GET /v1/quickstart` agent funnel | ☐ PR 38 |
| Kit's watcher migrated to dual-emit (Telegram + Claude wake-up) | ☐ PR 38 |

## Install

```bash
pip install kitsdeals-river
```

(Pre-publish: `pip install -e .` from this directory.)

## Agent-driven setup (the expected install flow)

Paste this repo URL to your Claude Code agent and say:

> Set up Kit's Deals for me — I'm looking for X and Y.

The agent loads `skills/onboarding.md`, runs a short interview (4–6
questions max), translates your answers to a profile, and starts the
watcher. You don't see the YAML.

For ongoing changes — *"stop showing me TVs"*, *"I bought one of those"*,
*"only show me clear wins"* — your agent loads `skills/update-profile.md`
and translates the natural-language request into a structured CLI edit.
Live reload via `SIGHUP`, no restart required.

## CLI

The CLI is the agent-facing API to the profile and the watcher daemon.
Humans rarely run these directly; the skills tell the agent how to use
them.

```bash
kitsdeals-river setup --json '<full profile JSON>'      # write profile + start
kitsdeals-river run                                     # foreground watcher
kitsdeals-river status                                  # daemon health snapshot
kitsdeals-river reload                                  # SIGHUP a running watcher
kitsdeals-river profile show [--json]                   # inspect current profile
kitsdeals-river profile update --remove-watch "TVs"     # programmatic edits
kitsdeals-river profile validate                        # well-formedness check
```

See `skills/onboarding.md` and `skills/update-profile.md` for the agent-
facing recipes.

## Minimal example (without an agent in the loop)

```python
from kitsdeals_river import RiverWatcher, RiverEvent, StdoutNotifier
from kitsdeals_river.profile import WatchFilter
import asyncio

async def main():
    notifier = StdoutNotifier()
    watcher = RiverWatcher(
        filter=WatchFilter(
            category="tv",
            brand_in=["LG", "Sony"],
            min_discount_pct=25,
        ),
    )

    @watcher.on_event
    async def handle(event: RiverEvent):
        if event.is_deal:
            deal = event.data
            await notifier.send(
                f"{deal['brand']} — ${deal['current_price_cents']/100:.2f}",
                deal,
            )

    await watcher.run_async()

asyncio.run(main())
```

See `examples/minimal.py` for a runnable version.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE). Use freely, build on top, fork it for your
own personal-event runtime.

## Roadmap

This package is one channel of a broader pattern: **decoupled summarization**
— a centralized service does expensive synthesis work once, emits a
structured event stream, and agents do cheap per-event filtering against a
user profile. Same architecture applies to news, jobs, regulatory filings,
GitHub releases, real-estate listings. Kit's Deals is the first concrete
implementation; the SDK's profile + watcher + decisions-log shapes are
designed to translate.
