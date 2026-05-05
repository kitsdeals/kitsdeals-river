"""Slack notifier — posts via an incoming webhook URL.

Why incoming webhooks vs. the bot API: incoming webhooks are the
zero-setup path. The user creates a webhook URL in Slack's UI, copies
it into an env var, done. The bot API needs OAuth + scopes + workspace
install, which is appropriate for a multi-channel app but overkill
for "ping me when a deal lands."

Channel + tone are baked into the webhook URL on the Slack side, so
this notifier doesn't need them as parameters.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("kitsdeals_river.notify.slack")


class SlackNotifier:
    """Post matched-deal notifications to a Slack incoming webhook."""

    def __init__(
        self,
        *,
        webhook_url_env: str = "SLACK_WEBHOOK_URL",
        webhook_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url = (
            webhook_url if webhook_url is not None else os.environ.get(webhook_url_env)
        )
        self._client = client

    async def send(self, message: str, deal: dict[str, Any]) -> bool:
        if not self._webhook_url:
            logger.warning("slack webhook URL not configured; dropping")
            return False

        client = self._client or httpx.AsyncClient(timeout=10.0)
        owns_client = self._client is None
        try:
            response = await client.post(self._webhook_url, json={"text": message})
            # Slack returns "ok" as text/plain on success; on error returns a
            # 4xx with a body like "no_service" or "invalid_payload".
            if response.status_code != 200:
                logger.warning(
                    "slack post failed: %s %s",
                    response.status_code,
                    response.text[:200],
                )
                return False
            return True
        except httpx.HTTPError as e:
            logger.warning("slack post failed: %s", e)
            return False
        finally:
            if owns_client:
                await client.aclose()
