"""Generic HTTP POST notifier — for any inbound HTTPS endpoint.

For agents on platforms that publish a callback URL (Gemini, Cloudflare
Workers, Lambda, your own service). Posts a JSON body with both the
rendered ``message`` and the full ``deal`` payload, so the receiver
can render its own copy if desired.

Supports custom headers (Authorization tokens, signing keys) so the
receiver can verify the source.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("kitsdeals_river.notify.http_post")


class HttpPostNotifier:
    """Post a JSON-encoded notification to a custom URL.

    Body shape:
        { "message": "<rendered text>", "deal": <full deal dict> }

    Caller-supplied ``headers`` are included on every request; common
    use is ``{"Authorization": "Bearer <token>"}`` so the receiver can
    confirm the request is from the configured watcher.

    Treats any 2xx as success.
    """

    def __init__(
        self,
        *,
        url_env: str = "HTTP_POST_URL",
        url: str | None = None,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url if url is not None else os.environ.get(url_env)
        self._headers = headers or {}
        self._client = client

    async def send(self, message: str, deal: dict[str, Any]) -> bool:
        if not self._url:
            logger.warning("http_post URL not configured; dropping")
            return False

        body = {"message": message, "deal": deal}
        client = self._client or httpx.AsyncClient(timeout=10.0)
        owns_client = self._client is None
        try:
            response = await client.post(self._url, json=body, headers=self._headers)
            if not (200 <= response.status_code < 300):
                logger.warning(
                    "http_post failed: %s %s",
                    response.status_code,
                    response.text[:200],
                )
                return False
            return True
        except httpx.HTTPError as e:
            logger.warning("http_post failed: %s", e)
            return False
        finally:
            if owns_client:
                await client.aclose()
