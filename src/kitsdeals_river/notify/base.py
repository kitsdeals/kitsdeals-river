"""Notifier protocol — minimal contract every adapter implements."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    """A notification channel.

    Implementations must:
      - Be safe to call repeatedly without rebuilding state
      - Never raise on transport failure (log + return False instead)
      - Treat ``deal`` as untrusted input (it's a dict from the river)
    """

    async def send(self, message: str, deal: dict[str, Any]) -> bool:
        """Send ``message`` for ``deal``. Returns True on apparent success."""
        ...
