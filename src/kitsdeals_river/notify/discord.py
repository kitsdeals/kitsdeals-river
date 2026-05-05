"""Discord notifier — posts via a channel webhook.

Same shape as Slack: webhook URL is what the user creates in Discord's
"Edit Channel → Integrations → Webhooks" UI; we just POST to it. No
OAuth, no bot permission shuffle.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("kitsdeals_river.notify.discord")


class DiscordNotifier:
    """Post matched-deal notifications to a Discord channel webhook."""

    def __init__(
        self,
        *,
        webhook_url_env: str = "DISCORD_WEBHOOK_URL",
        webhook_url: str | None = None,
        username: str | None = "Kit's Deals",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url = (
            webhook_url if webhook_url is not None else os.environ.get(webhook_url_env)
        )
        self._username = username
        self._client = client

    async def send(self, message: str, deal: dict[str, Any]) -> bool:
        if not self._webhook_url:
            logger.warning("discord webhook URL not configured; dropping")
            return False

        body: dict[str, Any] = {"content": message}
        if self._username:
            body["username"] = self._username

        client = self._client or httpx.AsyncClient(timeout=10.0)
        owns_client = self._client is None
        try:
            response = await client.post(self._webhook_url, json=body)
            # Discord returns 204 No Content on success.
            if response.status_code not in (200, 204):
                logger.warning(
                    "discord post failed: %s %s",
                    response.status_code,
                    response.text[:200],
                )
                return False
            return True
        except httpx.HTTPError as e:
            logger.warning("discord post failed: %s", e)
            return False
        finally:
            if owns_client:
                await client.aclose()
