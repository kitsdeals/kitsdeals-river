"""kitsdeals-river — Python SDK for consuming the Kit's Deals river.

See README.md for the install + onboarding flow. The expected interaction
model is agent-mediated: a human tells their Claude/Code agent to set up
this package, and the agent runs the onboarding skill (PR 37) to translate
the conversation into a profile.yaml + a running watcher daemon.
"""

from .decisions import DecisionsLog
from .notify import Notifier, StdoutNotifier, TelegramNotifier
from .profile import (
    Profile,
    ProfileMeta,
    Watch,
    WatchFilter,
    NotifyConfig,
    SuppressConfig,
    OwnedProduct,
)
from .watcher import RiverEvent, RiverWatcher, Cursor

__version__ = "0.1.0"

__all__ = [
    "RiverWatcher",
    "RiverEvent",
    "Cursor",
    "Profile",
    "ProfileMeta",
    "Watch",
    "WatchFilter",
    "NotifyConfig",
    "SuppressConfig",
    "OwnedProduct",
    "DecisionsLog",
    "Notifier",
    "StdoutNotifier",
    "TelegramNotifier",
]
