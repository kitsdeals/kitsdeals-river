"""Tests for notify adapters."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest

from kitsdeals_river.notify import (
    DiscordNotifier,
    HttpPostNotifier,
    SlackNotifier,
    StdoutNotifier,
    SubprocessNotifier,
    TelegramNotifier,
)


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


# ---- Slack ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_unconfigured_returns_false():
    n = SlackNotifier(webhook_url=None)
    ok = await n.send("hi", {"id": "deal_x"})
    assert ok is False


@pytest.mark.asyncio
async def test_slack_send_posts_text():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode())
        return httpx.Response(200, content=b"ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n = SlackNotifier(webhook_url="https://hooks.slack.example/abc", client=client)
    ok = await n.send("Hello world", {"id": "deal_x"})
    assert ok is True
    assert captured["body"] == {"text": "Hello world"}


@pytest.mark.asyncio
async def test_slack_non_200_returns_false():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"no_service")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n = SlackNotifier(webhook_url="https://hooks.slack.example/abc", client=client)
    ok = await n.send("hi", {"id": "deal_x"})
    assert ok is False


# ---- Discord -------------------------------------------------------------


@pytest.mark.asyncio
async def test_discord_send_posts_204():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode())
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n = DiscordNotifier(
        webhook_url="https://discord.example/webhook/abc",
        username="Tester",
        client=client,
    )
    ok = await n.send("Hi all", {"id": "deal_y"})
    assert ok is True
    assert captured["body"]["content"] == "Hi all"
    assert captured["body"]["username"] == "Tester"


@pytest.mark.asyncio
async def test_discord_unconfigured_returns_false():
    n = DiscordNotifier(webhook_url=None)
    ok = await n.send("hi", {"id": "deal_x"})
    assert ok is False


# ---- HttpPost ------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_post_includes_message_and_deal():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode())
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, content=b"ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n = HttpPostNotifier(
        url="https://agent.example/deals",
        headers={"Authorization": "Bearer abc123"},
        client=client,
    )
    ok = await n.send("LG TV $1799", {"id": "deal_z", "brand": "LG"})
    assert ok is True
    assert captured["body"]["message"] == "LG TV $1799"
    assert captured["body"]["deal"]["brand"] == "LG"
    assert captured["headers"]["authorization"] == "Bearer abc123"


@pytest.mark.asyncio
async def test_http_post_4xx_returns_false():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"forbidden")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n = HttpPostNotifier(url="https://agent.example/deals", client=client)
    ok = await n.send("hi", {"id": "deal_x"})
    assert ok is False


# ---- Subprocess ----------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_runs_command(tmp_path: Path):
    """Smoke: write a side-effect to disk and verify the subprocess ran."""
    out = tmp_path / "out.txt"
    n = SubprocessNotifier(
        command=[sys.executable, "-c", f"open({str(out)!r}, 'w').write('{{deal_id}}')"]
    )
    ok = await n.send("ignored", {"id": "deal_a"})
    assert ok is True
    assert out.read_text() == "deal_a"


@pytest.mark.asyncio
async def test_subprocess_substitutes_placeholders(tmp_path: Path):
    out = tmp_path / "out.txt"
    n = SubprocessNotifier(
        command=[
            sys.executable, "-c",
            f"import sys; open({str(out)!r}, 'w').write(sys.argv[1] + '|' + sys.argv[2])",
            "{message}",
            "{deal_id}",
        ]
    )
    ok = await n.send("Hello", {"id": "deal_b"})
    assert ok is True
    assert out.read_text() == "Hello|deal_b"


@pytest.mark.asyncio
async def test_subprocess_stdin_pipes_deal_json(tmp_path: Path):
    out = tmp_path / "stdin.json"
    n = SubprocessNotifier(
        command=[
            sys.executable, "-c",
            f"import sys; open({str(out)!r}, 'w').write(sys.stdin.read())",
        ],
        stdin=True,
    )
    ok = await n.send("ignored", {"id": "deal_c", "brand": "LG"})
    assert ok is True
    parsed = json.loads(out.read_text())
    assert parsed["id"] == "deal_c"
    assert parsed["brand"] == "LG"


@pytest.mark.asyncio
async def test_subprocess_command_not_found_returns_false():
    n = SubprocessNotifier(command=["this-command-definitely-does-not-exist-zzzz"])
    ok = await n.send("hi", {"id": "deal_x"})
    assert ok is False


@pytest.mark.asyncio
async def test_subprocess_nonzero_exit_returns_false():
    n = SubprocessNotifier(command=[sys.executable, "-c", "import sys; sys.exit(1)"])
    ok = await n.send("hi", {"id": "deal_x"})
    assert ok is False


def test_subprocess_empty_command_raises():
    with pytest.raises(ValueError):
        SubprocessNotifier(command=[])
