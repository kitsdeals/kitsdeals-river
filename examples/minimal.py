"""Minimal example: watch the river, print every deal that matches a filter.

Run with:
    pip install -e .
    python examples/minimal.py
"""
from __future__ import annotations

import asyncio

from kitsdeals_river import RiverEvent, RiverWatcher, StdoutNotifier
from kitsdeals_river.profile import WatchFilter


async def main():
    notifier = StdoutNotifier()

    watcher = RiverWatcher(
        # Filter at the server: TVs from LG/Sony/Samsung, ≥25% off, ≤$2000.
        # Sophisticated agents can omit this and filter client-side.
        filter=WatchFilter(
            category="tv",
            brand_in=["LG", "Sony", "Samsung"],
            min_discount_pct=25,
            max_price_cents=200000,
        ),
    )

    @watcher.on_event
    async def handle(event: RiverEvent):
        if not event.is_deal:
            return
        deal = event.data
        msg = (
            f"{deal.get('brand', '?')} — {deal.get('product_type', '?')} — "
            f"${deal.get('current_price_cents', 0) / 100:.2f} "
            f"({deal.get('discount_pct', 0)}% off)"
        )
        await notifier.send(msg, deal)

    await watcher.run_async()


if __name__ == "__main__":
    asyncio.run(main())
