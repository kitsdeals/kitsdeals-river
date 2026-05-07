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


# ============================================================
# v0.4.0: notify_threshold + multi-watch helper tests
# ============================================================

from kitsdeals_river.cli import (  # noqa: E402
    _passes_threshold,
    _watch_for_deal,
    _sanitize_watch_label,
)
from kitsdeals_river.profile import Profile, Watch, WatchFilter  # noqa: E402


def test_passes_threshold_anything_is_a_passthrough():
    """anything tier never gates — everything notifies."""
    assert _passes_threshold({"deal_score": 0}, "anything") is True
    assert _passes_threshold({}, "anything") is True


def test_passes_threshold_good_floor():
    """good = deal_score >= 60."""
    assert _passes_threshold({"deal_score": 59.9}, "good") is False
    assert _passes_threshold({"deal_score": 60.0}, "good") is True
    assert _passes_threshold({"deal_score": 100}, "good") is True


def test_passes_threshold_clear_win_floor():
    """clear_win = deal_score >= 80."""
    assert _passes_threshold({"deal_score": 79.9}, "clear_win") is False
    assert _passes_threshold({"deal_score": 80.0}, "clear_win") is True


def test_passes_threshold_missing_score_below_anything_fails():
    """Defensive: deal without a score can't be promised to clear good
    or clear_win."""
    assert _passes_threshold({}, "good") is False
    assert _passes_threshold({"deal_score": None}, "clear_win") is False
    # but anything still passes
    assert _passes_threshold({}, "anything") is True


def test_watch_for_deal_picks_first_match():
    """When multiple watches are configured, the FIRST matching one
    wins — its threshold applies."""
    profile = Profile(
        name="Tom",
        watches=[
            Watch(label="AirPods 4", filter=WatchFilter(
                brand_in=["Apple"], product_name_contains="AirPods 4",
            )),
            Watch(label="All Apple", filter=WatchFilter(brand_in=["Apple"])),
        ],
    )
    deal_a = {"brand": "Apple", "product_name": "AirPods 4 Wireless Earbuds", "category": "electronics"}
    deal_b = {"brand": "Apple", "product_name": "iPad Mini", "category": "electronics"}

    assert _watch_for_deal(profile, deal_a).label == "AirPods 4"
    assert _watch_for_deal(profile, deal_b).label == "All Apple"


def test_watch_for_deal_filters_correctly():
    """Each filter dimension is honored: brand, condition, price,
    discount, product_name_contains."""
    profile = Profile(
        name="Tom",
        watches=[Watch(label="AP4", filter=WatchFilter(
            brand_in=["Apple"],
            condition_in=["new", "open_box"],
            max_price_cents=18000,
            min_discount_pct=10,
            product_name_contains="AirPods 4",
        ))],
    )
    base = {
        "brand": "Apple", "category": "electronics",
        "product_name": "AirPods 4 Wireless",
        "condition": "new", "current_price_cents": 14900, "discount_pct": 25,
    }
    assert _watch_for_deal(profile, base) is not None

    assert _watch_for_deal(profile, {**base, "brand": "Sony"}) is None
    assert _watch_for_deal(profile, {**base, "condition": "refurbished"}) is None
    assert _watch_for_deal(profile, {**base, "current_price_cents": 19000}) is None
    assert _watch_for_deal(profile, {**base, "discount_pct": 5}) is None
    assert _watch_for_deal(profile, {**base, "product_name": "AirPods Pro"}) is None


def test_sanitize_watch_label():
    assert _sanitize_watch_label("Apple AirPods 4") == "apple-airpods-4"
    assert _sanitize_watch_label("TVs (LG / Sony)") == "tvs-lg-sony"
    assert _sanitize_watch_label("   weird   spaces  ") == "weird-spaces"
    # All non-alnum collapses to "default" rather than empty
    assert _sanitize_watch_label("///") == "default"
    assert _sanitize_watch_label("") == "default"


# ============================================================
# v0.4.1: rich notification formatter tests
# ============================================================

from kitsdeals_river.cli import (  # noqa: E402
    format_deal_telegram_html,
    format_deal_plain,
)


def _full_deal():
    """A representative river-event payload (post PR 50). Most tests
    drop fields to validate optional-handling."""
    return {
        "id": "deal_abc123",
        "product_name": "Apple AirPods 4 Wireless Earbuds (Active Noise Cancellation)",
        "brand": "Apple",
        "sku": "MWWA3AM/A",
        "product_url": "https://www.amazon.com/dp/B0DJTPGHMR",
        "image_url": "https://m.media-amazon.com/images/...",
        "category": "electronics",
        "subcategory": "audio",
        "product_type": "wireless earbuds",
        "condition": "new",
        "merchant_name": "Amazon",
        "merchant_slug": "amazon",
        "current_price_cents": 9900,
        "original_price_cents": 12900,
        "discount_pct": 23,
        "currency": "USD",
        "deal_quality": "lowest price seen this year",
        "deal_score": 87.5,
        "expires_at": "2099-01-01T00:00:00Z",  # far future so countdown is stable
    }


def test_telegram_html_includes_title_prices_merchant_url():
    msg = format_deal_telegram_html(_full_deal())
    assert "<b>" in msg and "AirPods 4" in msg
    assert "$99.00" in msg and "$129.00" in msg
    assert "23% off" in msg
    assert "At Amazon" in msg
    # 87.5 rounds to 88 with :.0f formatting
    assert "score 88" in msg
    assert "lowest price seen this year" in msg
    # URL appears as an anchor for Telegram parsing
    assert '<a href="https://www.amazon.com/dp/B0DJTPGHMR">' in msg


def test_telegram_html_escapes_user_content():
    """Title with HTML metacharacters must be escaped — defense in
    depth even though server validates URL truth."""
    deal = _full_deal()
    deal["product_name"] = 'Acme "Extreme" Headphones <pwned>'
    msg = format_deal_telegram_html(deal)
    assert "&lt;pwned&gt;" in msg
    assert "<pwned>" not in msg


def test_telegram_html_handles_open_box_condition():
    deal = _full_deal()
    deal["condition"] = "open_box"
    msg = format_deal_telegram_html(deal)
    assert "open box" in msg


def test_telegram_html_omits_condition_when_new():
    """Don't bloat the meta line with 'new' since it's the default."""
    deal = _full_deal()
    deal["condition"] = "new"
    msg = format_deal_telegram_html(deal)
    assert "new" not in msg.lower().split("score")[0]  # not in meta line before "score"


def test_telegram_html_handles_missing_optionals():
    """Deal with only the required fields still produces a valid message."""
    deal = {
        "id": "x",
        "product_name": "Thing",
        "current_price_cents": 4999,
    }
    msg = format_deal_telegram_html(deal)
    assert "Thing" in msg
    assert "$49.99" in msg
    # No URL, no merchant, no expiry — just doesn't crash


def test_telegram_html_falls_back_to_brand_plus_type_for_title():
    """No product_name → brand + product_type."""
    deal = {"id": "x", "brand": "Sony", "product_type": "65 OLED TV"}
    msg = format_deal_telegram_html(deal)
    assert "Sony 65 OLED TV" in msg


def test_telegram_html_coupon_surfaces_when_present():
    deal = _full_deal()
    deal["coupon_code"] = "SAVE10"
    msg = format_deal_telegram_html(deal)
    assert "SAVE10" in msg


def test_telegram_html_expiry_countdown():
    """Future expiry shows days remaining; past expiry omitted."""
    from datetime import datetime, timedelta, timezone
    deal = _full_deal()
    # +5d 6h to avoid the off-by-one when timedelta-int conversion
    # truncates partial days at the day boundary.
    deal["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=5, hours=6)).isoformat()
    msg = format_deal_telegram_html(deal)
    assert "ends in 5 day" in msg

    deal["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    msg = format_deal_telegram_html(deal)
    assert "ends in" not in msg


def test_plain_format_no_html_tags():
    msg = format_deal_plain(_full_deal())
    assert "<b>" not in msg
    assert "<a" not in msg
    # Pipe-joined inline; same fields just no markup
    assert "AirPods 4" in msg
    assert "$99.00" in msg
    assert "Amazon" in msg
    assert "https://www.amazon.com/dp/B0DJTPGHMR" in msg


def test_plain_format_handles_missing_fields():
    msg = format_deal_plain({"id": "x", "product_name": "Thing"})
    assert "Thing" in msg
