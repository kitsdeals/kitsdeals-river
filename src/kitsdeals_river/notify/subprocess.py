"""Subprocess notifier — fire an arbitrary command per match.

Different from ``spawn_evaluator`` in ``evaluator.py``: that function
spawns the user's agent with a structured decision prompt and parses
JSON output. This notifier just runs a command, optionally piping the
deal as JSON on stdin or substituting fields into the argv. No
parsing, no decision protocol — fire-and-forget.

Use cases:
- Trigger a local script (``./on_deal.sh``) that does whatever the user wants
- Send via the OS native notification CLI (``notify-send`` on Linux,
  ``terminal-notifier`` on macOS)
- Fan out to a queue (``echo $DEAL_JSON | redis-cli -x lpush deals``)

The escape hatch when none of the bundled notifiers fit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
from typing import Any

logger = logging.getLogger("kitsdeals_river.notify.subprocess")


class SubprocessNotifier:
    """Run an arbitrary command per matched deal.

    ``command`` is a list of argv tokens. The literal token
    ``{message}`` in any token is substituted with the rendered message;
    ``{deal_json}`` is substituted with the deal payload as compact JSON;
    ``{deal_id}`` is substituted with the deal id (for filenames etc).

    If ``stdin`` is True, the deal is also piped as JSON on stdin.

    Failures (non-zero exit, command not found) are logged and return
    False; the watcher continues.
    """

    def __init__(
        self,
        *,
        command: list[str],
        stdin: bool = False,
        timeout_seconds: int = 30,
    ) -> None:
        if not command:
            raise ValueError("command must be a non-empty list")
        self._command = command
        self._stdin = stdin
        self._timeout = timeout_seconds

    async def send(self, message: str, deal: dict[str, Any]) -> bool:
        deal_json = json.dumps(deal, separators=(",", ":"))
        deal_id = str(deal.get("id", ""))

        argv = [
            tok.replace("{message}", message)
                .replace("{deal_json}", deal_json)
                .replace("{deal_id}", deal_id)
            for tok in self._command
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if self._stdin else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            logger.warning("subprocess command not found: %s (%s)", argv[0], e)
            return False
        except OSError as e:
            logger.warning("subprocess spawn failed: %s", e)
            return False

        stdin_data = deal_json.encode("utf-8") if self._stdin else None
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(stdin_data), timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("subprocess timed out: %s", shlex.join(argv))
            return False

        if proc.returncode != 0:
            logger.warning(
                "subprocess exited %s: %s; stderr: %s",
                proc.returncode,
                shlex.join(argv),
                stderr_b.decode(errors="replace")[:200],
            )
            return False
        return True
