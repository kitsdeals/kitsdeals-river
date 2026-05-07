"""SSE consumer for the Kit's Deals river.

The watcher manages the moving parts of staying connected to a long-lived
SSE stream so the calling code can focus on what to do per event:

- Connection + reconnect with exponential backoff
- Server-side filter via query params (PR 34)
- v2 event-envelope parsing (schema_version, event_type, event_id, data)
- Cursor persistence to disk so a restart doesn't re-process old events
- REST catch-up on reconnect for events missed during the disconnect
- SIGHUP-triggered profile reload (PR 37) so the agent can edit
  ~/.kitsdeals/profile.yaml and have changes pick up live

User code attaches a callback via ``@watcher.on_event(...)`` (or supplies
one in the constructor) and runs ``watcher.run()`` to block forever, or
``watcher.run_async()`` inside an existing asyncio loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .profile import Profile, WatchFilter

logger = logging.getLogger("kitsdeals_river.watcher")

DEFAULT_API_BASE = "https://api.kitsdeals.com"
DEFAULT_USER_AGENT = "kitsdeals-river/0.1.0"
RECONNECT_INITIAL_BACKOFF_SECONDS = 2.0
RECONNECT_MAX_BACKOFF_SECONDS = 30.0


# Type aliases — sync-style handler signatures by default; the runtime
# wraps sync handlers in run_in_executor so users don't have to choose
# between sync and async upfront.
SyncHandler = Callable[["RiverEvent"], None]
AsyncHandler = Callable[["RiverEvent"], Awaitable[None]]
Handler = SyncHandler | AsyncHandler


@dataclass(slots=True)
class RiverEvent:
    """A parsed v2 envelope. Attribute access for ergonomic handler code.

    The river guide's envelope shape is:
        { schema_version, event_type, event_id, occurred_at, data }
    """

    schema_version: int
    event_type: str
    event_id: str
    occurred_at: str
    data: dict[str, Any]
    raw: dict[str, Any]  # full envelope for debugging

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any]) -> "RiverEvent":
        return cls(
            schema_version=envelope.get("schema_version", 1),
            event_type=envelope.get("event_type", "unknown"),
            event_id=envelope.get("event_id", ""),
            occurred_at=envelope.get("occurred_at", ""),
            data=envelope.get("data") or {},
            raw=envelope,
        )

    @property
    def is_deal(self) -> bool:
        return self.event_type in ("deal", "deal_updated")

    @property
    def is_expiry(self) -> bool:
        return self.event_type == "deal_expired"


class Cursor:
    """Persistent file-backed cursor.

    Stores ``last_event_id`` and ``last_occurred_at``. The first lets
    consumers use the SSE ``Last-Event-ID`` semantic for short reconnects;
    the second drives REST catch-up via ``GET /v1/deals?since=...`` for
    longer gaps.

    Atomic-ish update via tmp-file + rename. Cursor file is small; no
    rotation needed.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupted — treat as fresh start. Better than crashing on every
            # boot because of one bad write.
            logger.warning("cursor file unreadable, treating as empty: %s", self.path)
            return {}

    def save(self, *, last_event_id: str, last_occurred_at: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"last_event_id": last_event_id, "last_occurred_at": last_occurred_at},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)


class RiverWatcher:
    """High-level SSE consumer for the Kit's Deals river.

    Construction is parameter-rich on purpose: making the dependencies
    explicit keeps the test surface clean (every external thing — HTTP
    client, cursor, sleep — is injectable).
    """

    def __init__(
        self,
        *,
        api_base: str = DEFAULT_API_BASE,
        filter: WatchFilter | None = None,
        cursor_path: str | Path = "~/.kitsdeals/cursor.json",
        user_agent: str = DEFAULT_USER_AGENT,
        on_event: Handler | None = None,
        # PR 37: reload_profile is invoked on SIGHUP. Returns the new
        # filter so the watcher can swap mid-flight without a restart.
        # Caller usually wires this to Profile.load(...).filter via a
        # closure, but anything that returns a WatchFilter (or None to
        # clear filtering) works.
        reload_profile: Callable[[], WatchFilter | None] | None = None,
        # SDK v0.4.0: when True, on first start (cursor empty) the
        # watcher does a one-shot REST query against /v1/deals matching
        # the watch's coarse filters (category / max_price /
        # min_discount) and dispatches each currently-live deal through
        # the same handler as live SSE. Closes the gap Kit's audit
        # flagged: a freshly-configured watch otherwise misses every
        # currently-live matching deal. The handler still applies the
        # downstream personalization filters (matches_owned,
        # suppress.keywords_blacklist, etc.) so this is a "show me
        # what's already on the river right now" feature, not a
        # filter-bypass.
        initial_backfill: bool = False,
        initial_backfill_window_days: int = 14,
        # Test/extension hooks: each is parameterized so unit tests can
        # inject mocks without monkeypatching globals.
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.filter = filter
        self.cursor = Cursor(cursor_path)
        self.user_agent = user_agent
        self._on_event: Handler | None = on_event
        self._reload_profile = reload_profile
        self._initial_backfill = initial_backfill
        self._initial_backfill_window_days = initial_backfill_window_days
        self._initial_backfill_done = False
        self._http_client_factory = http_client_factory
        self._sleep = sleep
        self._stop = asyncio.Event()
        # SIGHUP raises this; the consume loop checks between events and
        # tears down the connection so the next iteration picks up the
        # new filter on a fresh connect.
        self._reload_requested = asyncio.Event()

    # -- Handler registration -----------------------------------------------

    def on_event(self, fn: Handler) -> Handler:
        """Decorator: register the per-event handler.

        Sync or async — both work. Wrapped in a thread for sync handlers
        so the SSE socket isn't blocked while user code runs.
        """
        self._on_event = fn
        return fn

    # -- Run loop -----------------------------------------------------------

    def run(self) -> None:
        """Block forever, reconnecting on drops. Use this when the watcher
        is the only thing on the event loop. Wires SIGHUP → reload and
        SIGTERM/SIGINT → stop so daemon control works the unix way."""
        async def _wrap():
            loop = asyncio.get_running_loop()
            # SIGHUP only exists on POSIX; the add_signal_handler call is
            # also POSIX-only. On Windows we just silently skip — the user
            # can still drive reload programmatically via request_reload().
            try:
                loop.add_signal_handler(signal.SIGHUP, self.request_reload)
                loop.add_signal_handler(signal.SIGTERM, self.stop)
                loop.add_signal_handler(signal.SIGINT, self.stop)
            except (AttributeError, NotImplementedError):
                logger.info("signal handlers unavailable on this platform")
            await self.run_async()

        try:
            asyncio.run(_wrap())
        except KeyboardInterrupt:
            logger.info("interrupted; stopping")

    async def run_async(self) -> None:
        """Same loop for callers that already manage the event loop."""
        backoff = RECONNECT_INITIAL_BACKOFF_SECONDS
        # SDK v0.4.0: optional initial backfill. Runs ONCE when the cursor
        # is empty (i.e., first start of this watch) — surfaces currently-
        # live matching deals through the handler before we open SSE. Skip
        # silently when disabled or when there's already a cursor (i.e.
        # this isn't a fresh start).
        if self._initial_backfill and not self._initial_backfill_done:
            try:
                await self._do_initial_backfill()
            except Exception as e:  # noqa: BLE001 - defensive, don't kill the watcher
                logger.warning("initial backfill failed: %s; continuing to live SSE", e)
            self._initial_backfill_done = True
        while not self._stop.is_set():
            self._maybe_apply_reload()
            try:
                await self._catch_up_via_rest()
                await self._consume_sse()
                # Clean disconnect (server closed) — reset backoff
                backoff = RECONNECT_INITIAL_BACKOFF_SECONDS
            except (httpx.HTTPError, httpx.StreamError, OSError) as e:
                logger.warning("connection error: %s; reconnecting in %.1fs", e, backoff)
                try:
                    await self._sleep(backoff)
                except asyncio.CancelledError:
                    break
                backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF_SECONDS)
            except asyncio.CancelledError:
                break

    def stop(self) -> None:
        """Ask the run loop to shut down. Returns immediately; the loop
        exits at the next iteration."""
        self._stop.set()

    def request_reload(self) -> None:
        """Ask the run loop to re-read the profile and apply a new filter
        on the next connect. Safe to call from a signal handler — it just
        sets a flag.
        """
        logger.info("reload requested; will apply on next connect")
        self._reload_requested.set()

    def _maybe_apply_reload(self) -> None:
        if not self._reload_requested.is_set() or self._reload_profile is None:
            self._reload_requested.clear()
            return
        try:
            new_filter = self._reload_profile()
            self.filter = new_filter
            logger.info("profile reloaded; new filter: %s", new_filter)
        except Exception:
            # Don't let a bad profile take down the watcher; keep the
            # old filter and try again on the next reload signal.
            logger.exception("profile reload failed; keeping previous filter")
        finally:
            self._reload_requested.clear()

    # -- Internals ----------------------------------------------------------

    def _make_client(self) -> httpx.AsyncClient:
        if self._http_client_factory:
            return self._http_client_factory()
        # No total timeout — SSE streams are designed to stay open. Read
        # timeout caps the keepalive cadence (server sends one every 30s,
        # so 60s gives one full miss before we reconnect).
        return httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
            headers={"User-Agent": self.user_agent},
        )

    async def _catch_up_via_rest(self) -> None:
        """Fetch deals published after our cursor via REST, then update the
        cursor before opening SSE.

        This is the two-step pattern from the river guide — GET /v1/deals
        for backfill, SSE for the forward stream. Cleanly separates the
        replay concern from the live concern.
        """
        state = self.cursor.load()
        since = state.get("last_occurred_at")
        if not since:
            # Fresh start — skip catch-up. The first SSE 'connected' frame
            # gives us our first cursor.
            return

        params: dict[str, str] = {"since": since, "limit": "50"}
        # Don't pass the SSE filter to /v1/deals — that endpoint has its
        # own filter shape and we'd risk losing matches if they don't
        # align perfectly. Catch-up is a small backfill window; the
        # downstream handler does the personalization filter anyway.
        url = f"{self.api_base}/v1/deals"
        async with self._make_client() as client:
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as e:
                logger.warning("catch-up GET failed: %s; skipping", e)
                return

        deals = payload.get("data", {}).get("deals") or []
        logger.info("catch-up: %d deals since %s", len(deals), since)

        # Walk in chronological order so the cursor advances monotonically
        # and the handler sees them in the same order live SSE would.
        last_id = state.get("last_event_id", "")
        last_at = since
        for deal in sorted(deals, key=lambda d: d.get("published_at") or ""):
            envelope = {
                "schema_version": 1,
                "event_type": "deal",
                "event_id": deal.get("id", ""),
                "occurred_at": deal.get("published_at", ""),
                "data": deal,
            }
            event = RiverEvent.from_envelope(envelope)
            await self._dispatch(event)
            last_id = event.event_id
            last_at = event.occurred_at
        if deals:
            self.cursor.save(last_event_id=last_id, last_occurred_at=last_at)

    async def _do_initial_backfill(self) -> None:
        """One-shot REST backfill at first start.

        Fetches recent-window deals matching the watch's coarse server-
        side filters (category / max_price / min_discount — the ones
        /v1/deals supports today) and dispatches each through the same
        handler that processes live SSE events. The handler's downstream
        personalization filters (matches_owned, matches_blacklist, etc.)
        still apply, so this is "show me what's already on the river"
        not "bypass my filters."

        Runs only when the cursor is empty (fresh first start). Sets the
        cursor to the most-recent deal seen so SSE doesn't re-deliver
        the same events.
        """
        if self.cursor.load().get("last_occurred_at"):
            # Not a fresh start — defer to the normal _catch_up_via_rest
            # path which uses since=<cursor>.
            return

        # Build coarse filter from the watch. /v1/deals supports a
        # smaller vocabulary than /v1/river — just the overlapping
        # subset is used here. The downstream handler does the rest
        # (brand_in, condition_in, product_name_contains).
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self._initial_backfill_window_days)
        ).isoformat()
        params: dict[str, str] = {"since": cutoff, "limit": "10"}
        if self.filter:
            if self.filter.category:
                params["category"] = self.filter.category
            if self.filter.max_price_cents is not None:
                params["max_price"] = str(self.filter.max_price_cents)
            if self.filter.min_discount_pct is not None:
                params["min_discount"] = (
                    str(int(self.filter.min_discount_pct))
                    if float(self.filter.min_discount_pct).is_integer()
                    else str(self.filter.min_discount_pct)
                )

        url = f"{self.api_base}/v1/deals"
        async with self._make_client() as client:
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as e:
                logger.warning("initial backfill GET failed: %s; skipping", e)
                return

        deals = payload.get("data", {}).get("deals") or []
        logger.info(
            "initial backfill: %d deal(s) within %dd window",
            len(deals),
            self._initial_backfill_window_days,
        )

        last_id = ""
        last_at = ""
        for deal in sorted(deals, key=lambda d: d.get("published_at") or ""):
            envelope = {
                "schema_version": 1,
                "event_type": "deal",
                "event_id": deal.get("id", ""),
                "occurred_at": deal.get("published_at", ""),
                "data": deal,
            }
            event = RiverEvent.from_envelope(envelope)
            await self._dispatch(event)
            last_id = event.event_id
            last_at = event.occurred_at
        if deals:
            # Set cursor so SSE doesn't replay these events on connect.
            # Subsequent ticks will use the normal _catch_up_via_rest.
            self.cursor.save(last_event_id=last_id, last_occurred_at=last_at)

    async def _consume_sse(self) -> None:
        """Open and drain a single SSE connection. Returns when the server
        closes; the run loop reconnects."""
        url = f"{self.api_base}/v1/river"
        params = self.filter.to_query_params() if self.filter else {}
        state = self.cursor.load()
        headers: dict[str, str] = {}
        if state.get("last_event_id"):
            # Native EventSource resume hint. The server's current /v1/river
            # implementation doesn't act on this header (it's live-only) but
            # passing it costs nothing and forward-compats with any future
            # server-side replay buffer.
            headers["Last-Event-ID"] = state["last_event_id"]

        async with self._make_client() as client:
            async with client.stream("GET", url, params=params, headers=headers) as response:
                response.raise_for_status()
                async for envelope, event_id in _parse_sse(response):
                    event = RiverEvent.from_envelope(envelope)
                    await self._dispatch(event)
                    if event.event_id and event.occurred_at:
                        self.cursor.save(
                            last_event_id=event.event_id,
                            last_occurred_at=event.occurred_at,
                        )
                    # PR 37: between-events check for stop / reload. Tearing
                    # the connection here is the cleanest way to apply a new
                    # filter — the loop reconnects with the updated query
                    # params on the next iteration.
                    if self._stop.is_set() or self._reload_requested.is_set():
                        return

    async def _dispatch(self, event: RiverEvent) -> None:
        if self._on_event is None:
            logger.debug("no handler registered; dropping %s", event.event_type)
            return
        try:
            result = self._on_event(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            # Handler exceptions must NOT bring down the run loop. The
            # whole point of this layer is to keep the connection alive.
            logger.exception("handler raised on event %s; continuing", event.event_id)


# ---------------------------------------------------------------------------
# SSE parser
# ---------------------------------------------------------------------------

async def _parse_sse(response: httpx.Response):
    """Minimal SSE parser. Yields (envelope_dict, event_id) tuples."""
    event_type = "message"
    data_lines: list[str] = []
    last_id = ""

    async for line in response.aiter_lines():
        if line == "":
            # End of message frame. Emit if we have data.
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    envelope = json.loads(raw)
                except json.JSONDecodeError:
                    # Bad frame — log + skip. Don't tear down the stream.
                    logger.warning("malformed SSE data frame; skipping")
                    envelope = None
                if envelope is not None:
                    if "event_type" not in envelope and event_type:
                        envelope["event_type"] = event_type
                    yield envelope, last_id
            event_type = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            # Comment / keepalive
            continue
        # Field parsing
        if ":" not in line:
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            last_id = value
        # Other field types (retry, etc.) ignored for v1
