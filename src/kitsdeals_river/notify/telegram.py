"""Telegram notifier — sends via the Bot API."""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("kitsdeals_river.notify.telegram")


class TelegramNotifier:
    """Send notifications to a Telegram chat (or forum topic) via Bot API.

    Credentials come from environment variables — passing tokens directly
    is supported for tests but discouraged for production. The variable
    names are configurable so multiple bots can coexist on one host.

    Network failures are logged and swallowed (return False); the watcher
    keeps running.
    """

    def __init__(
        self,
        *,
        bot_token_env: str = "TELEGRAM_BOT_TOKEN",
        chat_id_env: str = "TELEGRAM_CHAT_ID",
        topic_id_env: str = "TELEGRAM_TOPIC_ID",
        bot_token: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bot_token = bot_token if bot_token is not None else os.environ.get(bot_token_env)
        self._chat_id = chat_id if chat_id is not None else os.environ.get(chat_id_env)
        self._topic_id = topic_id if topic_id is not None else os.environ.get(topic_id_env)
        self._client = client  # injectable for tests

    async def send(self, message: str, deal: dict[str, Any]) -> bool:
        if not self._bot_token or not self._chat_id:
            logger.warning("telegram not configured (missing token or chat id); dropping")
            return False

        body: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "HTML",
            # SDK v0.4.1: enable Telegram's link preview so the deal's
            # product_url unfurls into a card (image + title + site).
            # That's what makes the notification feel "complete" instead
            # of a tiny one-liner. Telegram unfurls the FIRST URL in the
            # message; the formatter puts product_url near the top.
            "disable_web_page_preview": False,
        }
        if self._topic_id:
            try:
                body["message_thread_id"] = int(self._topic_id)
            except ValueError:
                logger.warning("telegram topic_id %r is not int; ignoring", self._topic_id)

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        client = self._client or httpx.AsyncClient(timeout=10.0)
        owns_client = self._client is None
        try:
            response = await client.post(url, json=body)
            try:
                payload = response.json()
            except ValueError:
                logger.warning("telegram non-json response (status=%s)", response.status_code)
                return False
            if not payload.get("ok"):
                logger.warning("telegram API error: %s", payload.get("description"))
                return False
            return True
        except httpx.HTTPError as e:
            logger.warning("telegram send failed: %s", e)
            return False
        finally:
            if owns_client:
                await client.aclose()
