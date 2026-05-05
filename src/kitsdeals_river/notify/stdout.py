"""Stdout notifier — for local dev, debugging, and CI."""
from __future__ import annotations

import sys
from typing import Any, TextIO


class StdoutNotifier:
    """Writes notifications to a text stream.

    Defaults to stdout, but accepts any writable text stream — useful in
    tests where you want to capture output without monkeypatching globals.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout

    async def send(self, message: str, deal: dict[str, Any]) -> bool:
        deal_id = deal.get("id", "?")
        self.stream.write(f"[kitsdeals] {deal_id}: {message}\n")
        self.stream.flush()
        return True
