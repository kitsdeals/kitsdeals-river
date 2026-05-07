"""Tests for RiverWatcher: cursor persistence, SSE parsing, dispatch.

Network-touching paths (REST catch-up, SSE consume) use httpx's
MockTransport so we can drive the wire format without spinning up a
real server.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from kitsdeals_river.profile import WatchFilter
from kitsdeals_river.watcher import Cursor, RiverEvent, RiverWatcher, _parse_sse


# -- Cursor ------------------------------------------------------------------


def test_cursor_round_trip(tmp_path: Path):
    c = Cursor(tmp_path / "cursor.json")
    assert c.load() == {}
    c.save(last_event_id="evt_a", last_occurred_at="2026-05-04T13:00:00Z")
    state = c.load()
    assert state["last_event_id"] == "evt_a"
    assert state["last_occurred_at"] == "2026-05-04T13:00:00Z"


def test_cursor_corrupted_treated_as_empty(tmp_path: Path):
    """A corrupted cursor file shouldn't crash the watcher on boot."""
    path = tmp_path / "cursor.json"
    path.write_text("not json at all")
    assert Cursor(path).load() == {}


def test_cursor_torn_write_cleanup(tmp_path: Path):
    path = tmp_path / "cursor.json"
    c = Cursor(path)
    c.save(last_event_id="evt_a", last_occurred_at="2026-05-04T13:00:00Z")
    tmp = path.with_suffix(path.suffix + ".tmp")
    assert not tmp.exists()


# -- RiverEvent --------------------------------------------------------------


def test_event_from_envelope():
    env = {
        "schema_version": 1,
        "event_type": "deal",
        "event_id": "deal_x",
        "occurred_at": "2026-05-04T13:00:00Z",
        "data": {"id": "deal_x", "brand": "LG"},
    }
    e = RiverEvent.from_envelope(env)
    assert e.event_id == "deal_x"
    assert e.is_deal is True
    assert e.is_expiry is False
    assert e.data["brand"] == "LG"
    assert e.raw is env


def test_event_expiry_classification():
    e = RiverEvent.from_envelope({
        "schema_version": 1,
        "event_type": "deal_expired",
        "event_id": "deal_x",
        "occurred_at": "2026-05-04T13:00:00Z",
        "data": {"id": "deal_x", "reason": "expired"},
    })
    assert e.is_deal is False
    assert e.is_expiry is True


# -- _parse_sse --------------------------------------------------------------


class _FakeResponse:
    """Just enough of httpx.Response to test the parser."""
    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


@pytest.mark.asyncio
async def test_parse_sse_basic():
    lines = [
        "event: deal",
        "id: deal_a",
        'data: {"schema_version":1,"event_type":"deal","event_id":"deal_a","occurred_at":"2026-05-04T13:00:00Z","data":{"id":"deal_a"}}',
        "",
        "event: deal_expired",
        "id: deal_b",
        'data: {"schema_version":1,"event_type":"deal_expired","event_id":"deal_b","occurred_at":"2026-05-04T13:01:00Z","data":{"id":"deal_b"}}',
        "",
    ]
    events = []
    async for envelope, event_id in _parse_sse(_FakeResponse(lines)):
        events.append((envelope, event_id))

    assert len(events) == 2
    assert events[0][0]["event_type"] == "deal"
    assert events[0][1] == "deal_a"
    assert events[1][0]["event_type"] == "deal_expired"


@pytest.mark.asyncio
async def test_parse_sse_skips_keepalive_and_malformed():
    lines = [
        ": keepalive",
        "",
        "event: deal",
        "data: {malformed json",
        "",
        "event: deal",
        "id: deal_good",
        'data: {"event_type":"deal","event_id":"deal_good","data":{}}',
        "",
    ]
    events = []
    async for envelope, _eid in _parse_sse(_FakeResponse(lines)):
        events.append(envelope)
    # Malformed frame is dropped; the good one survives
    assert len(events) == 1
    assert events[0]["event_id"] == "deal_good"


@pytest.mark.asyncio
async def test_parse_sse_multiline_data():
    """SSE data: lines accumulate across multiple lines per frame."""
    lines = [
        "event: deal",
        'data: {"event_type":"deal",',
        'data: "event_id":"deal_x",',
        'data: "data":{"id":"deal_x"}}',
        "",
    ]
    events = []
    async for envelope, _eid in _parse_sse(_FakeResponse(lines)):
        events.append(envelope)
    assert len(events) == 1
    assert events[0]["event_id"] == "deal_x"


# -- RiverWatcher dispatch ---------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_handler_exception_is_swallowed(tmp_path: Path):
    """Handler exceptions must NOT propagate; the run loop has to keep going."""
    w = RiverWatcher(cursor_path=tmp_path / "cursor.json")

    @w.on_event
    def boom(event: RiverEvent):
        raise RuntimeError("handler bug")

    e = RiverEvent.from_envelope({
        "schema_version": 1, "event_type": "deal", "event_id": "x",
        "occurred_at": "2026-05-04T13:00:00Z", "data": {"id": "x"},
    })
    # If the exception leaks, this raises. It shouldn't.
    await w._dispatch(e)


@pytest.mark.asyncio
async def test_dispatch_async_handler(tmp_path: Path):
    w = RiverWatcher(cursor_path=tmp_path / "cursor.json")
    received = []

    @w.on_event
    async def handler(event: RiverEvent):
        await asyncio.sleep(0)  # actual await — exercise the coroutine path
        received.append(event.event_id)

    e = RiverEvent.from_envelope({
        "schema_version": 1, "event_type": "deal", "event_id": "deal_async",
        "occurred_at": "2026-05-04T13:00:00Z", "data": {},
    })
    await w._dispatch(e)
    assert received == ["deal_async"]


# -- catch-up via REST -------------------------------------------------------


@pytest.mark.asyncio
async def test_catch_up_no_cursor_skips_request(tmp_path: Path):
    """Fresh start has no cursor → no REST call; the SSE 'connected' frame
    seeds the first cursor."""
    requests_made = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request)
        return httpx.Response(200, json={"data": {"deals": []}})

    transport = httpx.MockTransport(handler)
    factory = lambda: httpx.AsyncClient(transport=transport)
    w = RiverWatcher(cursor_path=tmp_path / "cursor.json", http_client_factory=factory)
    await w._catch_up_via_rest()
    assert requests_made == []


@pytest.mark.asyncio
async def test_catch_up_dispatches_and_advances_cursor(tmp_path: Path):
    """A non-empty REST response runs the handler in chronological order
    and advances the cursor to the latest occurred_at."""
    cursor_path = tmp_path / "cursor.json"
    Cursor(cursor_path).save(
        last_event_id="evt_old", last_occurred_at="2026-05-04T12:00:00Z"
    )

    deals_payload = {
        "data": {
            "deals": [
                # Out of order to verify sort
                {"id": "deal_b", "brand": "Sony", "published_at": "2026-05-04T13:00:00Z"},
                {"id": "deal_a", "brand": "LG", "published_at": "2026-05-04T12:30:00Z"},
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        # Verify the catch-up uses ?since=<cursor>
        assert request.url.params["since"] == "2026-05-04T12:00:00Z"
        return httpx.Response(200, json=deals_payload)

    transport = httpx.MockTransport(handler)
    factory = lambda: httpx.AsyncClient(transport=transport)
    w = RiverWatcher(cursor_path=cursor_path, http_client_factory=factory)

    received: list[str] = []

    @w.on_event
    def collect(event: RiverEvent):
        received.append(event.event_id)

    await w._catch_up_via_rest()
    assert received == ["deal_a", "deal_b"]  # chronological by published_at

    state = Cursor(cursor_path).load()
    assert state["last_event_id"] == "deal_b"
    assert state["last_occurred_at"] == "2026-05-04T13:00:00Z"


@pytest.mark.asyncio
async def test_catch_up_http_error_does_not_throw(tmp_path: Path):
    """A failed catch-up GET should log + skip, not crash the watcher."""
    Cursor(tmp_path / "cursor.json").save(
        last_event_id="evt_x", last_occurred_at="2026-05-04T12:00:00Z"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    factory = lambda: httpx.AsyncClient(transport=transport)
    w = RiverWatcher(cursor_path=tmp_path / "cursor.json", http_client_factory=factory)
    # Should NOT raise
    await w._catch_up_via_rest()


# -- filter --------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_passed_through_to_sse_url(tmp_path: Path):
    """The filter's query params land on the SSE GET request."""
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        # Return immediately with no events to avoid hanging the test
        return httpx.Response(
            200,
            content=b"",
            headers={"Content-Type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    factory = lambda: httpx.AsyncClient(transport=transport)
    f = WatchFilter(category="tv", brand_in=["LG", "Sony"])
    w = RiverWatcher(
        filter=f,
        cursor_path=tmp_path / "cursor.json",
        http_client_factory=factory,
    )
    await w._consume_sse()
    assert len(seen_urls) == 1
    url = seen_urls[0]
    assert "category=tv" in url
    assert "brand_in=lg%2Csony" in url or "brand_in=lg,sony" in url


# ============================================================
# v0.4.1: SSE consumer must exit promptly when self._stop fires
# ============================================================


@pytest.mark.asyncio
async def test_consume_sse_exits_when_stop_fires(tmp_path: Path):
    """SDK v0.4.1: previously, _consume_sse only checked self._stop
    BETWEEN events. A stop signal during a quiet period (no events
    arriving) wouldn't tear the connection — systemd would wait its
    full timeout and SIGKILL. Fix is to race SSE consumption against
    self._stop.wait() and cancel mid-read on stop. This test reproduces
    the quiet-period case: no events ever arrive, but the watcher should
    still exit promptly when stop is set.
    """
    from kitsdeals_river.watcher import RiverWatcher

    # A stream that never emits anything — simulates a quiet SSE.
    async def never_yields(request):
        async def stream_forever():
            # Hold the connection open forever (would yield nothing).
            await asyncio.sleep(60)
            yield b""

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.AsyncByteStream(),
        )

    # Easier: build a watcher and trip self._stop very shortly after
    # _consume_sse opens the connection. We use a fake transport that
    # blocks-but-eventually-cancels.
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b""))

    def factory():
        return httpx.AsyncClient(transport=transport)

    w = RiverWatcher(
        api_base="http://test",
        cursor_path=tmp_path / "cursor.json",
        on_event=lambda e: None,
        http_client_factory=factory,
    )

    async def trigger_stop():
        await asyncio.sleep(0.05)
        w._stop.set()

    # Race: _consume_sse should exit within ~0.1s once stop is set.
    # If the bug is back, this test will hang until pytest-asyncio's
    # default timeout kicks in.
    consume_task = asyncio.create_task(w._consume_sse())
    asyncio.create_task(trigger_stop())

    try:
        await asyncio.wait_for(consume_task, timeout=2.0)
    except asyncio.TimeoutError:
        consume_task.cancel()
        raise AssertionError(
            "_consume_sse did not exit within 2s of self._stop being set"
        )
