"""Evaluator — wakes a Claude Code session per matched deal.

The agent gets a structured prompt with everything it needs to make a
personalized notify/skip/defer decision: identity (from profile), the
deal (the river event payload), recent decisions (so it can dedup and
learn from feedback), the user's hard suppressions, and the watch that
fired. The agent emits a single JSON object describing what it decided;
the watcher executes the action.

This split — agent decides, watcher acts — keeps credentials on the
watcher side, makes the agent's output testable in isolation, and means
a swap of the underlying model (Claude → another agent) only requires
the new model to honor the same JSON contract.

PR 38 will publish the matching skill files (skills/onboarding.md,
skills/update-profile.md) so a Claude Code session has docs to load.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any

from .decisions import DecisionsLog
from .profile import Profile, Watch
from .watcher import RiverEvent

logger = logging.getLogger("kitsdeals_river.evaluator")

# Agent timeout default. The Claude session needs to read the profile,
# scan recent decisions, evaluate the deal, and emit JSON — typically
# completes in <30s, but allow some headroom.
DEFAULT_TIMEOUT_SECONDS = 60


@dataclass
class EvaluatorDecision:
    """Parsed result from a Claude evaluator run."""

    decision: str  # "notify" | "defer" | "skip"
    reason: str
    message: str | None = None
    proposed_profile_updates: list[dict[str, Any]] = field(default_factory=list)
    raw_stdout: str = ""

    @classmethod
    def parse(cls, stdout: str) -> "EvaluatorDecision":
        """Parse the agent's JSON output. Tolerant of extra prose around
        the JSON (the agent might prefix or suffix). Picks the last balanced
        JSON object — Claude sometimes prefixes thinking before the answer.
        """
        # Find the last `{` and walk back to find a balanced object. Cheap
        # but reliable for the small payloads we expect (~few hundred bytes).
        text = stdout.strip()
        end = text.rfind("}")
        if end == -1:
            raise ValueError(f"no JSON object in output: {text[:200]!r}")
        # Walk back to find the matching `{`
        depth = 0
        start = -1
        for i in range(end, -1, -1):
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    start = i
                    break
        if start == -1:
            raise ValueError(f"unbalanced JSON in output: {text[:200]!r}")
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e}") from e

        decision = payload.get("decision")
        if decision not in ("notify", "defer", "skip"):
            raise ValueError(f"decision must be notify|defer|skip, got: {decision!r}")
        return cls(
            decision=decision,
            reason=str(payload.get("reason", "")),
            message=payload.get("message"),
            proposed_profile_updates=list(payload.get("proposed_profile_updates", [])),
            raw_stdout=stdout,
        )


def build_prompt(
    *,
    profile: Profile,
    event: RiverEvent,
    matched_watch: Watch,
    recent_decisions: list[dict[str, Any]],
) -> str:
    """Build the structured evaluator prompt.

    Pure function, deterministic for a given input — easy to test, easy
    to inspect when an agent goes off-script. Output is a long string;
    the caller pipes it to ``claude --task <prompt>`` (or equivalent).
    """
    suppress = profile.suppress
    already_owned_summary = "\n".join(
        f"  - canonical_product_id={o.canonical_product_id}, name={o.product_name}"
        for o in suppress.already_owned
    ) or "  (none)"
    blacklist_brands = ", ".join(suppress.brands_blacklist) or "(none)"
    blacklist_keywords = ", ".join(suppress.keywords_blacklist) or "(none)"
    pending_updates = profile.agent_proposed_updates.pending
    pending_summary = "\n".join(
        f"  - {u.reason}: {u.change}" for u in pending_updates
    ) or "  (none)"

    # Tail-format decisions log: one line per entry, newest LAST so the
    # agent reads in chronological order.
    decisions_summary = "\n".join(
        json.dumps(d, separators=(",", ":")) for d in recent_decisions
    ) or "  (no prior decisions)"

    return f"""\
You are evaluating a deal that just matched a personal watch on Kit's Deals.
Output a JSON decision; the watcher will execute any notification action.

# IDENTITY
Personalizing for: {profile.name}
Tone for any notification copy: {profile.tone}

# THE DEAL (live event from the kitsdeals river)
{json.dumps(event.data, indent=2)}

# WHY YOU SEE IT
Watch matched: "{matched_watch.label}"
Filter that fired: {matched_watch.filter.model_dump(exclude_none=True)}
Notify threshold for this watch: {matched_watch.notify_threshold}

# RECENT DECISIONS (chronological, oldest first)
Use these to avoid duplicate notifications and to learn from the user's
feedback. The "feedback" field captures their explicit response after a
notification fired; treat it as ground truth.
{decisions_summary}

# HARD SUPPRESSIONS (override watches when matched)
Already owns:
{already_owned_summary}
Blacklisted brands: {blacklist_brands}
Blacklisted keywords: {blacklist_keywords}

# ALREADY-PROPOSED UPDATES (don't propose duplicates)
{pending_summary}

# DECISION RULES (apply in order)
1. Hard-suppression match → skip
2. Same canonical_product_id appears in recent decisions within 7 days → skip (duplicate)
3. Apply notify_threshold:
   - clear_win:  notify only if unambiguously great (lowest in ~6 months,
                 established brand, in stock, no thin/msrp_dominant flags)
   - good:       notify if clearly above noise
   - anything:   notify always
4. If genuinely borderline → defer (logs for later review, no message sent)

# OUTPUT
Output a single JSON object on stdout, with NO surrounding prose:

{{
  "decision": "notify" | "defer" | "skip",
  "reason": "<one sentence, facts not chatter>",
  "message": "<2-3 sentences in {profile.name}'s tone>" or null,
  "proposed_profile_updates": [
    {{ "reason": "<observed pattern>", "change": "<concrete edit>" }}
  ]
}}
"""


async def spawn_claude_evaluator(
    *,
    profile: Profile,
    event: RiverEvent,
    matched_watch: Watch,
    decisions_log: DecisionsLog,
    command: str = "claude",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    extra_args: list[str] | None = None,
    recent_decisions_count: int = 50,
    runner: "ProcessRunner | None" = None,
) -> EvaluatorDecision:
    """Spawn the agent, parse its JSON output, and return the decision.

    Caller is responsible for executing the action (sending the
    notification, marking the deal seen, etc.) based on the returned
    EvaluatorDecision. This separation keeps secrets on the watcher
    side — the agent never sees Telegram tokens.

    Raises ValueError if the agent output can't be parsed; raises
    asyncio.TimeoutError if the agent exceeds ``timeout_seconds``.
    """
    recent = decisions_log.tail(recent_decisions_count)
    prompt = build_prompt(
        profile=profile,
        event=event,
        matched_watch=matched_watch,
        recent_decisions=recent,
    )

    args = [command]
    if extra_args:
        args.extend(extra_args)
    args.extend(["--no-input", "--task", prompt])

    runner = runner or _SubprocessRunner()
    stdout, stderr, returncode = await runner.run(args, timeout_seconds=timeout_seconds)

    if returncode != 0:
        # Don't trust stderr to be JSON-shaped, but surface it for debugging.
        logger.warning(
            "evaluator command failed (rc=%s): %s",
            returncode,
            stderr.strip()[:500],
        )
        raise RuntimeError(f"evaluator returncode={returncode}: {stderr.strip()[:200]}")

    return EvaluatorDecision.parse(stdout)


# ---------------------------------------------------------------------------
# ProcessRunner — abstraction so tests can inject a fake without spawning
# real subprocesses
# ---------------------------------------------------------------------------


class ProcessRunner:
    """Protocol for running a command and capturing stdout/stderr."""

    async def run(
        self, args: list[str], *, timeout_seconds: int
    ) -> tuple[str, str, int]:
        raise NotImplementedError


class _SubprocessRunner(ProcessRunner):
    async def run(
        self, args: list[str], *, timeout_seconds: int
    ) -> tuple[str, str, int]:
        logger.debug("spawning: %s", shlex.join(args))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        return stdout, stderr, proc.returncode or 0
