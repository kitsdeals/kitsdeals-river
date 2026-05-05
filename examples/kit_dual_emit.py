"""Kit's dual-emit watcher pattern.

What this is: the reference for migrating an existing systemd-managed
"watch the river, post to Telegram" daemon onto the SDK while *also*
adding the agent-evaluator step. Kit's existing setup posted every
matched deal to a Telegram channel for human visibility; this version
keeps that AND wakes a Claude Code session per deal for autonomous
notify/skip/defer decisions.

Why dual-emit:
- Telegram channel = observability surface (Tom watches passively)
- Claude evaluator = autonomous action surface (decides + acts)

They serve different audiences. The channel post happens for every
match (no profile / decision logic — it's just visibility). The
evaluator runs the personalization + dedup + notification logic.

Run with:
    pip install -e ../  # from this directory
    export TELEGRAM_BOT_TOKEN=...
    export TELEGRAM_CHAT_ID=...
    export TELEGRAM_OPS_TOPIC_ID=...   # optional forum topic
    python examples/kit_dual_emit.py

Environment:
    KITSDEALS_PROFILE       Path to profile.yaml (default ~/.kitsdeals/profile.yaml)
    TELEGRAM_BOT_TOKEN      Bot token for the observability channel
    TELEGRAM_CHAT_ID        Group chat id
    TELEGRAM_OPS_TOPIC_ID   Forum topic id within the chat (optional)
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from kitsdeals_river import (
    DecisionsLog,
    Profile,
    RiverEvent,
    RiverWatcher,
    TelegramNotifier,
    spawn_evaluator,
)

logging.basicConfig(level=os.environ.get("KITSDEALS_LOG_LEVEL", "INFO"))
logger = logging.getLogger("kit_dual_emit")

PROFILE_PATH = Path(os.environ.get("KITSDEALS_PROFILE", "~/.kitsdeals/profile.yaml")).expanduser()
DECISIONS_PATH = Path("~/.kitsdeals/decisions.jsonl").expanduser()
CURSOR_PATH = Path("~/.kitsdeals/cursor.json").expanduser()


async def main() -> None:
    profile = Profile.load(PROFILE_PATH)
    decisions = DecisionsLog(DECISIONS_PATH)

    # Observability channel — every matched deal lands here regardless of
    # what the evaluator decides. Tom watches passively; this isn't the
    # decision surface, it's the visibility surface.
    observability = TelegramNotifier(
        bot_token_env="TELEGRAM_BOT_TOKEN",
        chat_id_env="TELEGRAM_CHAT_ID",
        topic_id_env="TELEGRAM_OPS_TOPIC_ID",
    )

    # Action channel — same Telegram chat by default, but the evaluator
    # decides whether to actually send. Use a different topic id if you
    # want notify-actions separated from observability.
    action_channel = TelegramNotifier(
        bot_token_env="TELEGRAM_BOT_TOKEN",
        chat_id_env="TELEGRAM_CHAT_ID",
        topic_id_env="TELEGRAM_OPS_TOPIC_ID",
    )

    # The user's agent command + prompt-passing args come from the
    # profile. The agent that ran onboarding wrote its own invocation
    # in here.
    if not profile.agent:
        raise SystemExit(
            "profile.agent block missing. Run onboarding to set it, or "
            "use examples/minimal.py if you don't want the evaluator step."
        )
    agent_cmd: str | list[str] = profile.agent.command
    agent_prompt_args = profile.agent.prompt_args

    # Pick the watch + filter from the profile. For simplicity, this
    # example uses the first watch — production code might run one
    # watcher per watch and merge their handler outputs.
    if not profile.watches:
        raise SystemExit("profile has no watches; nothing to subscribe to")
    watch = profile.watches[0]

    watcher = RiverWatcher(
        api_base=os.environ.get("KITSDEALS_API_BASE", "https://api.kitsdeals.com"),
        filter=watch.filter,
        cursor_path=CURSOR_PATH,
    )

    @watcher.on_event
    async def handle(event: RiverEvent) -> None:
        if not event.is_deal:
            return
        deal = event.data

        # Hard suppressions: never bother either surface for these
        if profile.matches_owned(deal) or profile.matches_blacklist(deal):
            return

        # === Observability: always emit ===
        brand = deal.get("brand", "?")
        ptype = deal.get("product_type", "")
        price = deal.get("current_price_cents", 0) / 100
        pct = deal.get("discount_pct", 0)
        merchant = deal.get("merchant_slug", "?")
        observability_msg = (
            f"📰 <b>{brand}</b> {ptype} — ${price:.2f} ({pct}% off) at {merchant}"
        )
        # Fire-and-forget: a Telegram failure shouldn't block the evaluator.
        asyncio.create_task(observability.send(observability_msg, deal))

        # === Action: agent decides notify / skip / defer ===
        try:
            decision = await spawn_evaluator(
                profile=profile,
                event=event,
                matched_watch=watch,
                decisions_log=decisions,
                command=agent_cmd,
                prompt_args=agent_prompt_args,
                timeout_seconds=profile.agent.timeout_seconds,
            )
        except Exception as e:
            logger.exception("evaluator failed for %s: %s", deal.get("id"), e)
            decisions.append(
                deal_id=deal.get("id", ""),
                decision="defer",
                reason=f"evaluator_error: {e}",
                canonical_product_id=deal.get("priority_product_id"),
            )
            return

        # Persist what the evaluator decided BEFORE actioning, so even
        # if the action fails the dedup signal is intact for the next run.
        decisions.append(
            deal_id=deal.get("id", ""),
            decision=decision.decision,
            reason=decision.reason,
            message_sent=decision.message if decision.decision == "notify" else None,
            canonical_product_id=deal.get("priority_product_id"),
            extra={"proposed_updates": decision.proposed_profile_updates},
        )

        if decision.decision == "notify" and decision.message:
            await action_channel.send(decision.message, deal)

    await watcher.run_async()


if __name__ == "__main__":
    asyncio.run(main())
