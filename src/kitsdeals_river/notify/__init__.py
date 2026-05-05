"""Notification adapters.

The base ``Notifier`` protocol is intentionally small — one async
``send(message, deal)`` method — so implementing a new channel is a
~30-line file. v1 ships ``stdout`` and ``telegram``; PR 38 will add
``slack``, ``discord``, ``http_post``, and ``subprocess``.

Adapters never raise on transport failure — they log and return False so
the watcher can fall through to the next channel (or just continue).
"""
from .base import Notifier
from .stdout import StdoutNotifier
from .telegram import TelegramNotifier

__all__ = ["Notifier", "StdoutNotifier", "TelegramNotifier"]
