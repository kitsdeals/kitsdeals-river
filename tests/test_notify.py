"""Tests for notify adapters: stdout (full), telegram (mocked transport)."""
from __future__ import annotations

import io

import httpx
import pytest

from kitsdeals_river.notify import StdoutNotifier, TelegramNotifier


@pytest.mark.asyncio
async def test_stdout_writes_to_stream():
    buf = io.StringIO()
    n = StdoutNotifier(stream=buf)
    ok = await n.send("Great deal", {"id": "deal_x", "brand": "LG"})
    assert ok is True
    assert "deal_x" in buf.getvalue()
    assert "Great deal" in buf.getvalue()


@pytest.mark.asyncio
async def test_telegram_unconfigured_returns_false():
    """Without a token, send returns False rather than raising — keeps the
    watcher running even when the operator forgot to set env vars."""
    n = TelegramNotifier(bot_token=None, chat_id=None)
    ok = await n.send("hi", {"id": "deal_x"})
    assert ok is False


@pytest.mark.asyncio
async def test_telegram_send_calls_api():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = request.read().decode()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n = TelegramNotifier(bot_token="testtoken", chat_id="123", client=client)
    ok = await n.send("Hello <b>world</b>", {"id": "deal_x"})
    assert ok is True
    assert "bot" in captured["url"] and "sendMessage" in captured["url"]
    assert "Hello" in captured["json"]
    assert "123" in captured["json"]


@pytest.mark.asyncio
async def test_telegram_api_error_returns_false():
    """A non-ok Telegram response returns False rather than raising."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "Chat not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n = TelegramNotifier(bot_token="t", chat_id="999", client=client)
    ok = await n.send("hi", {"id": "deal_x"})
    assert ok is False


@pytest.mark.asyncio
async def test_telegram_network_error_returns_false():
    """Connection errors return False instead of bubbling up."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n = TelegramNotifier(bot_token="t", chat_id="1", client=client)
    ok = await n.send("hi", {"id": "deal_x"})
    assert ok is False


@pytest.mark.asyncio
async def test_telegram_topic_id_added_when_set():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n = TelegramNotifier(bot_token="t", chat_id="1", topic_id="42", client=client)
    await n.send("hi", {"id": "deal_x"})
    assert '"message_thread_id":42' in captured["body"] or '"message_thread_id": 42' in captured["body"]
