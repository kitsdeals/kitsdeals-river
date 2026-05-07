# kitsdeals-river

Python SDK for consuming the [Kit's Deals](https://kitsdeals.com) river — a
real-time, structured stream of approved deals built for agent consumers.

> **The interaction model is agent-mediated.** You're not expected to edit
> YAML by hand. You tell your Claude/Code agent something like *"set up
> github.com/kitsdeals/kitsdeals-river for me — I'm looking for TVs
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
| `Notifier` — protocol + 6 built-ins (stdout, telegram, slack, discord, http_post, subprocess) | ✓ |
| `spawn_evaluator` — agent-agnostic wake-up with structured prompt | ✓ |
| `skills/onboarding.md` — agent-facing setup interview | ✓ |
| `skills/update-profile.md` — agent-facing ongoing refinement | ✓ |
| CLI (`setup`, `run`, `reload`, `status`, `profile show/update/validate`) | ✓ |
| Server-side `GET /v1/quickstart` agent funnel | ✓ |
| `examples/kit_dual_emit.py` — reference dual-emit pattern (observability + agent action) | ✓ |

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
kitsdeals-river setup --json '<full profile JSON>'      # write profile to disk
kitsdeals-river run                                     # foreground watcher (start here, or via systemd)
kitsdeals-river run --initial-backfill                  # also surface currently-live matching deals on first start
kitsdeals-river status                                  # daemon health snapshot
kitsdeals-river reload                                  # SIGHUP a running watcher
kitsdeals-river profile show [--json]                   # inspect current profile
kitsdeals-river profile update --remove-watch "TVs"     # programmatic edits
kitsdeals-river profile validate                        # well-formedness check
```

`setup` writes the profile only — it does **not** start the watcher.
`run` (or a systemd unit invoking `run`) is what actually opens the SSE
connection. Treat them as separate concerns: one configures, the other
keeps the daemon alive.

See `skills/onboarding.md` and `skills/update-profile.md` for the agent-
facing recipes.

## Multiple watches

Profiles can declare multiple watches (`profile.watches[]`). When more
than one is present, `kitsdeals-river run` opens **one SSE connection
per watch** concurrently in a single process — each with its own
filter, its own cursor file (`<cursor>-<sanitized-label>.json`), and
its own `notify_threshold`. A deal that matches multiple watches is
delivered on each matching connection; the default handler dedupes by
deal id and routes to the first matching watch's threshold.

## Notify thresholds

Each watch declares a `notify_threshold` ∈ `{anything, good, clear_win}`
that gates which matched deals actually fire a notification. Mapping
against the server's deal-quality score (`deal_score`, 0–100):

| threshold | floor | when to use |
|---|---|---|
| `anything` | none — every match notifies | broad watches you actively want noisy |
| `good` | `deal_score ≥ 60` | typical default; filters out obvious lemons |
| `clear_win` | `deal_score ≥ 80` | only-the-best alerts; lots will be filtered |

Deals lacking `deal_score` only notify when `notify_threshold` is
`anything` — refuse-to-guess posture.

## Telegram delivery setup

The default notifier reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
from the environment. Two prerequisites:

1. **Create a bot** via Telegram's `@BotFather`: send `/newbot`, pick a
   display name and username, save the token BotFather replies with.
2. **The user must DM the bot first.** Telegram bots cannot send the
   first message — the user has to open a chat with their new bot and
   send `/start` (or any message). Skip this step and the watcher runs
   fine but `sendMessage` returns
   `Forbidden: bot can't initiate conversation with a user`.

Then retrieve the chat_id (`GET https://api.telegram.org/bot<TOKEN>/getUpdates`
returns `result[*].message.chat.id`) and put both values in the
environment file your service reads. For full step-by-step including
verification and forum-topic routing, see `skills/onboarding.md` — the
agent-facing version walks through every failure mode.

The default notification format includes brand, product name, current
+ original price, discount %, merchant, condition, deal score, optional
quality verdict, the product URL (Telegram unfurls into a card), and an
expiry countdown when known.

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
