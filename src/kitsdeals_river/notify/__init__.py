"""Notification adapters.

The base ``Notifier`` protocol is intentionally small — one async
``send(message, deal)`` method — so implementing a new channel is a
~30-line file. v0.3 ships six built-ins: stdout, telegram, slack,
discord, http_post, subprocess.

Adapters never raise on transport failure — they log and return False so
the watcher can fall through to the next channel (or just continue).
"""
from .base import Notifier
from .discord import DiscordNotifier
from .http_post import HttpPostNotifier
from .slack import SlackNotifier
from .stdout import StdoutNotifier
from .subprocess import SubprocessNotifier
from .telegram import TelegramNotifier

__all__ = [
    "Notifier",
    "DiscordNotifier",
    "HttpPostNotifier",
    "SlackNotifier",
    "StdoutNotifier",
    "SubprocessNotifier",
    "TelegramNotifier",
]
