"""Tests for spawn_claude_evaluator + EvaluatorDecision parsing + prompt build."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kitsdeals_river.decisions import DecisionsLog
from kitsdeals_river.evaluator import (
    EvaluatorDecision,
    ProcessRunner,
    build_prompt,
    spawn_claude_evaluator,
)
from kitsdeals_river.profile import (
    OwnedProduct,
    Profile,
    SuppressConfig,
    Watch,
    WatchFilter,
)
from kitsdeals_river.watcher import RiverEvent


def _make_profile() -> Profile:
    return Profile(
        name="Tom",
        tone="Terse",
        watches=[Watch(label="TVs", filter=WatchFilter(category="tv"))],
        suppress=SuppressConfig(
            already_owned=[OwnedProduct(canonical_product_id="prod_lg_c4")],
            brands_blacklist=["BadBrand"],
        ),
    )


def _make_event() -> RiverEvent:
    return RiverEvent.from_envelope({
        "schema_version": 1,
        "event_type": "deal",
        "event_id": "deal_x",
        "occurred_at": "2026-05-04T13:00:00Z",
        "data": {
            "id": "deal_x",
            "brand": "LG",
            "current_price_cents": 129900,
            "discount_pct": 30,
        },
    })


# -- EvaluatorDecision.parse -------------------------------------------------


def test_parse_clean_json():
    out = '{"decision":"notify","reason":"great","message":"hi","proposed_profile_updates":[]}'
    d = EvaluatorDecision.parse(out)
    assert d.decision == "notify"
    assert d.reason == "great"
    assert d.message == "hi"


def test_parse_with_prefix_thinking():
    """Claude sometimes prefixes thinking before the answer. Parser picks
    the last balanced JSON object."""
    out = """\
Let me think about this. The deal looks good but I should check the recent
decisions. After reviewing, my decision is:

{"decision": "notify", "reason": "lowest in 6mo", "message": "LG C5 at $1799", "proposed_profile_updates": []}
"""
    d = EvaluatorDecision.parse(out)
    assert d.decision == "notify"
    assert d.message == "LG C5 at $1799"


def test_parse_with_nested_objects():
    """Walk-back-to-balance handles nested {} correctly."""
    out = '{"decision":"notify","reason":"x","proposed_profile_updates":[{"reason":"a","change":"b"}]}'
    d = EvaluatorDecision.parse(out)
    assert d.proposed_profile_updates == [{"reason": "a", "change": "b"}]


def test_parse_invalid_decision_value_raises():
    out = '{"decision": "go_for_it", "reason": "x"}'
    with pytest.raises(ValueError, match="decision must be"):
        EvaluatorDecision.parse(out)


def test_parse_no_json_raises():
    with pytest.raises(ValueError, match="no JSON"):
        EvaluatorDecision.parse("just prose, no JSON anywhere")


def test_parse_unbalanced_raises():
    """Partial / cut-off output raises rather than producing nonsense."""
    out = '{"decision": "notify", "reason": "x"'  # missing closing brace
    with pytest.raises(ValueError):
        EvaluatorDecision.parse(out)


# -- build_prompt ------------------------------------------------------------


def test_build_prompt_includes_identity():
    p = _make_profile()
    e = _make_event()
    prompt = build_prompt(
        profile=p,
        event=e,
        matched_watch=p.watches[0],
        recent_decisions=[],
    )
    assert "Tom" in prompt
    assert "Terse" in prompt
    assert "TVs" in prompt


def test_build_prompt_includes_deal_payload():
    p = _make_profile()
    e = _make_event()
    prompt = build_prompt(
        profile=p,
        event=e,
        matched_watch=p.watches[0],
        recent_decisions=[],
    )
    assert "129900" in prompt
    assert "LG" in prompt


def test_build_prompt_includes_hard_suppressions():
    p = _make_profile()
    prompt = build_prompt(
        profile=p,
        event=_make_event(),
        matched_watch=p.watches[0],
        recent_decisions=[],
    )
    assert "prod_lg_c4" in prompt
    assert "BadBrand" in prompt


def test_build_prompt_renders_recent_decisions():
    p = _make_profile()
    decisions = [
        {"deal_id": "deal_a", "decision": "notify", "reason": "x"},
        {"deal_id": "deal_b", "decision": "skip", "reason": "duplicate"},
    ]
    prompt = build_prompt(
        profile=p,
        event=_make_event(),
        matched_watch=p.watches[0],
        recent_decisions=decisions,
    )
    assert "deal_a" in prompt
    assert "deal_b" in prompt


def test_build_prompt_handles_empty_decisions_log():
    p = _make_profile()
    prompt = build_prompt(
        profile=p,
        event=_make_event(),
        matched_watch=p.watches[0],
        recent_decisions=[],
    )
    assert "no prior decisions" in prompt


# -- spawn_claude_evaluator with fake runner --------------------------------


class _FakeRunner(ProcessRunner):
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.calls: list[list[str]] = []

    async def run(self, args, *, timeout_seconds):
        self.calls.append(args)
        return self.stdout, self.stderr, self.returncode


@pytest.mark.asyncio
async def test_spawn_evaluator_happy_path(tmp_path: Path):
    log = DecisionsLog(tmp_path / "decisions.jsonl")
    runner = _FakeRunner(
        stdout='{"decision": "notify", "reason": "great", "message": "hi", "proposed_profile_updates": []}'
    )
    p = _make_profile()
    decision = await spawn_claude_evaluator(
        profile=p,
        event=_make_event(),
        matched_watch=p.watches[0],
        decisions_log=log,
        runner=runner,
    )
    assert decision.decision == "notify"
    assert decision.message == "hi"
    # Verify the spawned command shape
    assert runner.calls[0][0] == "claude"
    assert "--no-input" in runner.calls[0]
    assert "--task" in runner.calls[0]


@pytest.mark.asyncio
async def test_spawn_evaluator_nonzero_returncode_raises(tmp_path: Path):
    log = DecisionsLog(tmp_path / "decisions.jsonl")
    runner = _FakeRunner(stdout="", stderr="claude crashed", returncode=1)
    p = _make_profile()
    with pytest.raises(RuntimeError, match="returncode=1"):
        await spawn_claude_evaluator(
            profile=p,
            event=_make_event(),
            matched_watch=p.watches[0],
            decisions_log=log,
            runner=runner,
        )


@pytest.mark.asyncio
async def test_spawn_evaluator_invalid_json_raises(tmp_path: Path):
    log = DecisionsLog(tmp_path / "decisions.jsonl")
    runner = _FakeRunner(stdout="not json at all")
    p = _make_profile()
    with pytest.raises(ValueError):
        await spawn_claude_evaluator(
            profile=p,
            event=_make_event(),
            matched_watch=p.watches[0],
            decisions_log=log,
            runner=runner,
        )


@pytest.mark.asyncio
async def test_spawn_evaluator_passes_recent_decisions(tmp_path: Path):
    log = DecisionsLog(tmp_path / "decisions.jsonl")
    log.append(deal_id="deal_old", decision="skip", reason="duplicate")
    runner = _FakeRunner(stdout='{"decision":"skip","reason":"already saw"}')
    p = _make_profile()
    await spawn_claude_evaluator(
        profile=p,
        event=_make_event(),
        matched_watch=p.watches[0],
        decisions_log=log,
        runner=runner,
    )
    # The prompt (last arg after --task) should contain the prior decision
    task_arg = runner.calls[0][-1]
    assert "deal_old" in task_arg
