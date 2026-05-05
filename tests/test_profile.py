"""Tests for Profile schema, load/save, and matching helpers."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from kitsdeals_river.profile import (
    NotifyConfig,
    OwnedProduct,
    Profile,
    SuppressConfig,
    Watch,
    WatchFilter,
)


def _make_profile() -> Profile:
    return Profile(
        name="Tom",
        tone="Terse, factual.",
        notify=NotifyConfig(channel="stdout"),
        watches=[
            Watch(
                label="TVs",
                filter=WatchFilter(category="tv", brand_in=["LG", "Sony"]),
                notify_threshold="clear_win",
            ),
        ],
        suppress=SuppressConfig(
            already_owned=[
                OwnedProduct(canonical_product_id="prod_lg_c4_65"),
                OwnedProduct(product_name="MacBook Air M2"),
            ],
            brands_blacklist=["BadBrand"],
        ),
    )


def test_round_trip_yaml(tmp_path: Path):
    """Profile saves to YAML and reloads identically."""
    p = _make_profile()
    path = tmp_path / "profile.yaml"
    p.save(path)

    loaded = Profile.load(path)
    assert loaded.name == "Tom"
    assert len(loaded.watches) == 1
    assert loaded.watches[0].label == "TVs"
    assert loaded.watches[0].filter.brand_in == ["lg", "sony"]  # lowercased
    assert loaded.suppress.already_owned[0].canonical_product_id == "prod_lg_c4_65"


def test_filter_lowercasing():
    """Brand and condition lists normalize to lowercase on construction."""
    f = WatchFilter(brand_in=["LG", "Sony", "samsung"], condition_in=["NEW", "Open_Box"])
    assert f.brand_in == ["lg", "sony", "samsung"]
    assert f.condition_in == ["new", "open_box"]


def test_filter_to_query_params():
    """to_query_params produces the SSE-shaped query string."""
    f = WatchFilter(
        category="tv",
        brand_in=["LG", "Sony"],
        max_price_cents=200000,
        min_discount_pct=25,
    )
    params = f.to_query_params()
    assert params["category"] == "tv"
    assert params["brand_in"] == "lg,sony"
    assert params["max_price_cents"] == "200000"
    assert params["min_discount_pct"] == "25"


def test_save_writes_meta_block(tmp_path: Path):
    """save() writes a top-level _meta with last_edited_by tracking."""
    p = _make_profile()
    path = tmp_path / "profile.yaml"
    p.save(path, edited_by="agent")

    # Read raw to assert the YAML key is `_meta` not `meta`
    raw = yaml.safe_load(path.read_text())
    assert "_meta" in raw
    assert raw["_meta"]["last_edited_by"] == "agent"
    assert raw["_meta"]["schema_version"] == 1


def test_save_marks_human_edited(tmp_path: Path):
    """The escape-hatch case where a human bypassed the agent."""
    p = _make_profile()
    path = tmp_path / "profile.yaml"
    p.save(path, edited_by="human")

    raw = yaml.safe_load(path.read_text())
    assert raw["_meta"]["last_edited_by"] == "human"


def test_torn_write_safety(tmp_path: Path):
    """save() goes through a tmp file + rename; partial writes don't leave
    corruption behind. Sanity check that the tmp doesn't linger after a
    successful write."""
    p = _make_profile()
    path = tmp_path / "profile.yaml"
    p.save(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    assert not tmp.exists()
    assert path.exists()


def test_matches_owned_canonical():
    """canonical_product_id equality is the preferred dedup signal."""
    p = _make_profile()
    deal = {"id": "deal_x", "priority_product_id": "prod_lg_c4_65"}
    assert p.matches_owned(deal) is True


def test_matches_owned_substring_fallback():
    """When canonical_product_id is missing, fall back to product_name match."""
    p = _make_profile()
    # Note: priority_product_id is None, so the substring path runs.
    deal = {
        "id": "deal_y",
        "priority_product_id": None,
        "product_name": "Apple MacBook Air M2 13-inch",
    }
    assert p.matches_owned(deal) is True


def test_matches_owned_does_not_substring_when_canonical_present():
    """If canonical_product_id is present but doesn't match, we don't
    fall through to substring (canonical equality is the source of truth
    when available)."""
    p = _make_profile()
    deal = {
        "id": "deal_z",
        "priority_product_id": "prod_some_other_tv",
        "product_name": "MacBook Air M2",  # would match if substring ran
    }
    assert p.matches_owned(deal) is False


def test_matches_blacklist_brand():
    p = _make_profile()
    assert p.matches_blacklist({"brand": "BadBrand", "product_name": "X"}) is True
    assert p.matches_blacklist({"brand": "GoodBrand", "product_name": "X"}) is False


def test_matches_blacklist_keyword():
    p = Profile(
        name="x",
        suppress=SuppressConfig(keywords_blacklist=["counterfeit"]),
    )
    assert p.matches_blacklist({"brand": "X", "product_name": "Counterfeit Watch"}) is True


def test_unknown_field_rejected():
    """extra='forbid' on the schema means typos surface immediately rather
    than silently dropping data."""
    with pytest.raises(ValidationError):
        Profile.model_validate({"name": "x", "totally_made_up_field": True})


def test_load_missing_file_raises(tmp_path: Path):
    """A missing profile file is a clear caller error, not silent default."""
    with pytest.raises(FileNotFoundError):
        Profile.load(tmp_path / "does-not-exist.yaml")
