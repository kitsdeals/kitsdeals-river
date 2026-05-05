"""Tests for DecisionsLog — append, tail, dedup, rotation."""
from __future__ import annotations

import json
from pathlib import Path

from kitsdeals_river.decisions import DecisionsLog


def test_append_and_tail(tmp_path: Path):
    log = DecisionsLog(tmp_path / "decisions.jsonl")
    log.append(deal_id="deal_a", decision="notify", reason="great deal")
    log.append(deal_id="deal_b", decision="skip", reason="dup")

    entries = log.tail()
    assert len(entries) == 2
    assert entries[0]["deal_id"] == "deal_a"
    assert entries[1]["decision"] == "skip"
    # All entries get an `at` timestamp
    assert entries[0]["at"]
    assert entries[1]["at"]


def test_tail_limit(tmp_path: Path):
    log = DecisionsLog(tmp_path / "decisions.jsonl")
    for i in range(20):
        log.append(deal_id=f"deal_{i}", decision="notify", reason="x")

    entries = log.tail(n=5)
    assert len(entries) == 5
    assert entries[0]["deal_id"] == "deal_15"
    assert entries[-1]["deal_id"] == "deal_19"


def test_has_recent_canonical(tmp_path: Path):
    log = DecisionsLog(tmp_path / "decisions.jsonl")
    log.append(
        deal_id="deal_a",
        decision="notify",
        reason="x",
        canonical_product_id="prod_lg_c4",
    )
    log.append(deal_id="deal_b", decision="skip", reason="orphan")

    assert log.has_recent_canonical("prod_lg_c4") is True
    assert log.has_recent_canonical("prod_unknown") is False
    # None / empty short-circuits to False (fallback to other dedup paths)
    assert log.has_recent_canonical(None) is False
    assert log.has_recent_canonical("") is False


def test_extra_fields_preserved(tmp_path: Path):
    log = DecisionsLog(tmp_path / "decisions.jsonl")
    log.append(
        deal_id="deal_a",
        decision="notify",
        reason="x",
        extra={"score": 87, "rank": 1},
    )

    entry = log.tail(1)[0]
    assert entry["score"] == 87
    assert entry["rank"] == 1


def test_extra_fields_cannot_clobber_canonical(tmp_path: Path):
    """`extra` shouldn't be able to overwrite `decision` or `at` etc."""
    log = DecisionsLog(tmp_path / "decisions.jsonl")
    log.append(
        deal_id="deal_a",
        decision="notify",
        reason="x",
        extra={"decision": "FAKE", "at": "FAKE", "another": "ok"},
    )
    entry = log.tail(1)[0]
    assert entry["decision"] == "notify"  # caller value wins
    assert entry["at"] != "FAKE"
    assert entry["another"] == "ok"


def test_corrupted_line_skipped(tmp_path: Path):
    """A malformed line shouldn't break tail() for the rest of the file."""
    path = tmp_path / "decisions.jsonl"
    log = DecisionsLog(path)
    log.append(deal_id="deal_a", decision="notify", reason="x")
    # Inject a corrupted line
    with path.open("a") as f:
        f.write("not valid json at all\n")
    log.append(deal_id="deal_b", decision="skip", reason="y")

    entries = log.tail()
    ids = [e["deal_id"] for e in entries]
    assert ids == ["deal_a", "deal_b"]


def test_rotation_when_size_exceeded(tmp_path: Path):
    """When the file exceeds max_bytes, rotate to .0.jsonl and start fresh."""
    path = tmp_path / "decisions.jsonl"
    log = DecisionsLog(path, max_bytes=200, keep_archives=3)

    # Each append is ~120 bytes; first append: ~120, second triggers rotation
    log.append(deal_id="deal_a", decision="notify", reason="first one before rotation")
    log.append(deal_id="deal_b", decision="notify", reason="second triggers rotation")
    log.append(deal_id="deal_c", decision="notify", reason="third in fresh file")

    # Old archived; current contains entries written after the rotation
    archive_0 = path.with_name(path.name + ".0.jsonl")
    assert archive_0.exists()
    # Current file has the post-rotation entries
    current_entries = log.tail()
    current_ids = [e["deal_id"] for e in current_entries]
    assert "deal_b" in current_ids or "deal_c" in current_ids
    # Archive has earlier entries
    archive_lines = archive_0.read_text().strip().splitlines()
    assert any(json.loads(line)["deal_id"] == "deal_a" for line in archive_lines)


def test_iter_all_walks_archives(tmp_path: Path):
    """iter_all() returns archives + current in chronological order. With
    keep_archives=3, oldest entries past that horizon are intentionally
    dropped — the test confirms we still surface what's retained, in order."""
    path = tmp_path / "decisions.jsonl"
    log = DecisionsLog(path, max_bytes=100, keep_archives=3)
    for i in range(6):
        log.append(deal_id=f"deal_{i}", decision="notify", reason=f"reason_{i}")

    all_entries = list(log.iter_all())
    ids = [e["deal_id"] for e in all_entries]
    # The most recent entries are guaranteed to be there; older ones may
    # have been rotated out of keep_archives. Order must be chronological.
    assert "deal_5" in ids, "most recent always retained"
    # Order is chronological (archives oldest-first, then current)
    indices = [int(d.split("_")[1]) for d in ids]
    assert indices == sorted(indices), f"out of order: {ids}"


def test_keep_archives_caps_count(tmp_path: Path):
    """Old archives beyond keep_archives get dropped."""
    path = tmp_path / "decisions.jsonl"
    log = DecisionsLog(path, max_bytes=80, keep_archives=2)
    # Force several rotations
    for i in range(10):
        log.append(deal_id=f"deal_{i}", decision="notify", reason="x")

    archives = sorted(tmp_path.glob("decisions.jsonl.*.jsonl"))
    # At most keep_archives files (we keep 0..keep_archives-1)
    assert len(archives) <= 2
