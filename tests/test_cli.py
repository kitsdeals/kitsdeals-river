"""Tests for the CLI surface — setup, profile show/update/validate, status."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from kitsdeals_river.cli import main


VALID_SPEC = {
    "name": "Tom",
    "tone": "Terse, factual.",
    "notify": {"channel": "stdout"},
    "watches": [
        {
            "label": "TVs",
            "filter": {"category": "tv", "brand_in": ["LG", "Sony"]},
            "notify_threshold": "clear_win",
        }
    ],
    "suppress": {
        "already_owned": [{"product_name": "LG C4 65 OLED"}],
    },
}


def _profile_args(path: Path):
    return ["--profile", str(path)]


# -- setup -------------------------------------------------------------------


def test_setup_writes_profile(tmp_path: Path, capsys):
    profile_path = tmp_path / "profile.yaml"
    rc = main(["--profile", str(profile_path), "setup", "--json", json.dumps(VALID_SPEC)])
    assert rc == 0

    raw = yaml.safe_load(profile_path.read_text())
    assert raw["name"] == "Tom"
    assert raw["_meta"]["last_edited_by"] == "agent"


def test_setup_invalid_json_returns_1(tmp_path: Path, capsys):
    profile_path = tmp_path / "profile.yaml"
    rc = main(["--profile", str(profile_path), "setup", "--json", "{not valid"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "not valid JSON" in captured.err


def test_setup_invalid_spec_returns_1(tmp_path: Path, capsys):
    profile_path = tmp_path / "profile.yaml"
    rc = main(["--profile", str(profile_path), "setup", "--json", json.dumps({"name": 123})])
    assert rc == 1
    captured = capsys.readouterr()
    assert "validation" in captured.err.lower()


def test_setup_from_file(tmp_path: Path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(VALID_SPEC))
    profile_path = tmp_path / "profile.yaml"
    rc = main(
        ["--profile", str(profile_path), "setup", "--from-file", str(spec_path)]
    )
    assert rc == 0
    assert profile_path.exists()


def test_setup_no_input_returns_1(tmp_path: Path, capsys):
    rc = main(["--profile", str(tmp_path / "profile.yaml"), "setup"])
    assert rc == 1


# -- profile show -----------------------------------------------------------


def test_profile_show_human_format(tmp_path: Path, capsys):
    profile_path = tmp_path / "profile.yaml"
    main(["--profile", str(profile_path), "setup", "--json", json.dumps(VALID_SPEC)])
    capsys.readouterr()  # drain

    rc = main(["--profile", str(profile_path), "profile", "show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Tom" in out
    assert "TVs" in out
    assert "clear_win" in out


def test_profile_show_json(tmp_path: Path, capsys):
    profile_path = tmp_path / "profile.yaml"
    main(["--profile", str(profile_path), "setup", "--json", json.dumps(VALID_SPEC)])
    capsys.readouterr()

    rc = main(["--profile", str(profile_path), "profile", "show", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["name"] == "Tom"
    # Output uses by_alias=True so _meta is the key
    assert "_meta" in parsed


def test_profile_show_missing_returns_1(tmp_path: Path, capsys):
    rc = main(["--profile", str(tmp_path / "nope.yaml"), "profile", "show"])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


# -- profile validate -------------------------------------------------------


def test_profile_validate_ok(tmp_path: Path, capsys):
    profile_path = tmp_path / "profile.yaml"
    main(["--profile", str(profile_path), "setup", "--json", json.dumps(VALID_SPEC)])
    capsys.readouterr()

    rc = main(["--profile", str(profile_path), "profile", "validate"])
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_profile_validate_corrupted_returns_1(tmp_path: Path, capsys):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("name:\n  not_a_string: true\n")  # bad shape
    rc = main(["--profile", str(profile_path), "profile", "validate"])
    assert rc == 1


# -- profile update --------------------------------------------------------


def test_profile_update_remove_watch(tmp_path: Path, capsys):
    profile_path = tmp_path / "profile.yaml"
    main(["--profile", str(profile_path), "setup", "--json", json.dumps(VALID_SPEC)])
    capsys.readouterr()

    rc = main([
        "--profile", str(profile_path),
        "profile", "update",
        "--remove-watch", "TVs",
        "--no-reload",
    ])
    assert rc == 0

    raw = yaml.safe_load(profile_path.read_text())
    assert raw["watches"] == []


def test_profile_update_remove_nonexistent_returns_1(tmp_path: Path, capsys):
    profile_path = tmp_path / "profile.yaml"
    main(["--profile", str(profile_path), "setup", "--json", json.dumps(VALID_SPEC)])
    capsys.readouterr()

    rc = main([
        "--profile", str(profile_path),
        "profile", "update",
        "--remove-watch", "Laptops",
        "--no-reload",
    ])
    assert rc == 1


def test_profile_update_add_watch_replaces_by_label(tmp_path: Path, capsys):
    profile_path = tmp_path / "profile.yaml"
    main(["--profile", str(profile_path), "setup", "--json", json.dumps(VALID_SPEC)])
    capsys.readouterr()

    new_watch = {
        "label": "TVs",
        "filter": {"category": "tv", "brand_in": ["LG"], "min_discount_pct": 35},
        "notify_threshold": "anything",
    }
    rc = main([
        "--profile", str(profile_path),
        "profile", "update",
        "--add-watch", json.dumps(new_watch),
        "--no-reload",
    ])
    assert rc == 0

    raw = yaml.safe_load(profile_path.read_text())
    # Still 1 watch (replaced, not appended)
    assert len(raw["watches"]) == 1
    assert raw["watches"][0]["filter"]["min_discount_pct"] == 35
    assert raw["watches"][0]["notify_threshold"] == "anything"


def test_profile_update_blacklist_brand(tmp_path: Path, capsys):
    profile_path = tmp_path / "profile.yaml"
    main(["--profile", str(profile_path), "setup", "--json", json.dumps(VALID_SPEC)])
    capsys.readouterr()

    rc = main([
        "--profile", str(profile_path),
        "profile", "update",
        "--blacklist-brand", "BadBrand",
        "--blacklist-brand", "WorseBrand",
        "--no-reload",
    ])
    assert rc == 0

    raw = yaml.safe_load(profile_path.read_text())
    assert "BadBrand" in raw["suppress"]["brands_blacklist"]
    assert "WorseBrand" in raw["suppress"]["brands_blacklist"]


def test_profile_update_no_apply_dry_runs(tmp_path: Path, capsys):
    profile_path = tmp_path / "profile.yaml"
    main(["--profile", str(profile_path), "setup", "--json", json.dumps(VALID_SPEC)])
    before = profile_path.read_text()
    capsys.readouterr()

    rc = main([
        "--profile", str(profile_path),
        "profile", "update",
        "--remove-watch", "TVs",
        "--no-apply",
    ])
    assert rc == 0
    # File unchanged
    assert profile_path.read_text() == before
    # Diff printed as JSON
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "before" in parsed
    assert "after" in parsed


def test_profile_update_no_changes_reports(tmp_path: Path, capsys):
    """Calling update with no actual edits is a no-op + clear message."""
    profile_path = tmp_path / "profile.yaml"
    main(["--profile", str(profile_path), "setup", "--json", json.dumps(VALID_SPEC)])
    capsys.readouterr()

    rc = main([
        "--profile", str(profile_path),
        "profile", "update",
        "--no-reload",
    ])
    assert rc == 0
    assert "No changes" in capsys.readouterr().out


# -- status -----------------------------------------------------------------


def test_status_when_no_watcher_running(tmp_path: Path, capsys):
    rc = main([
        "status",
        "--pid-file", str(tmp_path / "nope.pid"),
        "--cursor", str(tmp_path / "nope.cursor"),
        "--decisions", str(tmp_path / "nope.decisions"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stopped" in out


def test_status_json_output(tmp_path: Path, capsys):
    rc = main([
        "status",
        "--json",
        "--pid-file", str(tmp_path / "nope.pid"),
        "--cursor", str(tmp_path / "nope.cursor"),
        "--decisions", str(tmp_path / "nope.decisions"),
    ])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["running"] is False
    assert parsed["decisions_count"] == 0


def test_status_reads_cursor(tmp_path: Path, capsys):
    cursor_path = tmp_path / "cursor.json"
    cursor_path.write_text(json.dumps({
        "last_event_id": "deal_x",
        "last_occurred_at": "2026-05-04T13:00:00Z",
    }))
    rc = main([
        "status",
        "--json",
        "--pid-file", str(tmp_path / "nope.pid"),
        "--cursor", str(cursor_path),
        "--decisions", str(tmp_path / "nope.decisions"),
    ])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["cursor"]["last_event_id"] == "deal_x"


# -- reload ----------------------------------------------------------------


def test_reload_no_pid_file_returns_1(tmp_path: Path, capsys):
    rc = main(["reload", "--pid-file", str(tmp_path / "missing.pid")])
    assert rc == 1
    assert "no watcher running" in capsys.readouterr().err
