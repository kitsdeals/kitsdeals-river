"""User profile — declarative description of what the human wants notified about.

The profile is machine-managed: an agent reads it, edits it, and writes it.
Humans rarely open the file directly; they tell their agent what they want
("set me up to watch TVs and laptops") and the agent translates that into
the structured form below.

Schema versioning: ``_meta.schema_version`` is bumped only on incompatible
changes. New fields are additive. Loaders for old versions fall through with
defaults applied.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


# -- Vocabulary constants (mirror the server-side validator) -----------------
# Source of truth lives in api/src/lib/deal-input-schema.js; we duplicate the
# values here so client-side validation can fail fast without a round-trip.
# When the server vocab changes, this list updates in lockstep.
VALID_CONDITIONS = ("new", "open_box", "refurbished", "pre_owned")
NotifyThreshold = Literal["clear_win", "good", "anything"]


class WatchFilter(BaseModel):
    """Server-side filter shape for /v1/river — see api docs for canonical."""

    category: str | None = None
    brand_in: list[str] | None = None
    brand: str | None = None
    condition_in: list[str] | None = None
    max_price_cents: int | None = None
    min_discount_pct: float | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("brand_in", "condition_in")
    @classmethod
    def _ensure_lowercase(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [s.lower().strip() for s in v if s and s.strip()]

    def to_query_params(self) -> dict[str, str]:
        """Convert to query-string params for `GET /v1/river?...`."""
        params: dict[str, str] = {}
        if self.category:
            params["category"] = self.category
        if self.brand_in:
            params["brand_in"] = ",".join(self.brand_in)
        if self.condition_in:
            params["condition_in"] = ",".join(self.condition_in)
        if self.max_price_cents is not None:
            params["max_price_cents"] = str(self.max_price_cents)
        if self.min_discount_pct is not None:
            # Emit "25" for 25.0, "12.5" for 12.5 — keeps URLs readable when
            # the discount is a whole number (the common case).
            params["min_discount_pct"] = (
                str(int(self.min_discount_pct))
                if float(self.min_discount_pct).is_integer()
                else str(self.min_discount_pct)
            )
        return params


class Watch(BaseModel):
    """One watch — a label, a filter, and a notification threshold."""

    label: str
    filter: WatchFilter = Field(default_factory=WatchFilter)
    notify_threshold: NotifyThreshold = "clear_win"

    model_config = ConfigDict(extra="forbid")


class TelegramConfig(BaseModel):
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"

    model_config = ConfigDict(extra="forbid")


class NotifyConfig(BaseModel):
    """Notification channel configuration. v1 supports one channel; multi-
    channel routing comes later."""

    channel: Literal["telegram", "stdout"] = "stdout"
    telegram: TelegramConfig | None = None

    model_config = ConfigDict(extra="forbid")


class AgentConfig(BaseModel):
    """Optional: spawn an agent on every match (PR 37 scope, defined here so
    the schema is stable).

    When unset, the watcher only notifies — no autonomous evaluator step.
    """

    command: str = "claude"
    timeout_seconds: int = 60

    model_config = ConfigDict(extra="forbid")


class OwnedProduct(BaseModel):
    """One product the user already owns — suppresses notifications even if
    a watch matches.

    Keyed by ``canonical_product_id`` (= server's ``priority_product_id``)
    when known; falls back to ``product_name`` substring matching when not.
    """

    canonical_product_id: str | None = None
    product_name: str | None = None
    since: str | None = None  # ISO date string when acquired; informational

    model_config = ConfigDict(extra="forbid")


class SuppressConfig(BaseModel):
    already_owned: list[OwnedProduct] = Field(default_factory=list)
    brands_blacklist: list[str] = Field(default_factory=list)
    keywords_blacklist: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ProposedUpdate(BaseModel):
    """An agent-suggested edit to the profile, awaiting human approval."""

    at: str
    reason: str
    change: str

    model_config = ConfigDict(extra="forbid")


class ProposedUpdates(BaseModel):
    pending: list[ProposedUpdate] = Field(default_factory=list)
    applied: list[ProposedUpdate] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ProfileMeta(BaseModel):
    schema_version: int = 1
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_edited_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_edited_by: Literal["agent", "human"] = "agent"
    agent_session_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class Profile(BaseModel):
    """Top-level profile. Loadable from YAML; writable back to YAML.

    The agent constructs this from a conversation with the user during
    onboarding (see ``skills/onboarding.md``, PR 37) and refines it over
    time via ``skills/update-profile.md``. Humans can edit by hand but the
    expected interaction model is agent-mediated.
    """

    meta: ProfileMeta = Field(default_factory=ProfileMeta, alias="_meta")
    name: str
    tone: str = "Terse, factual."
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    agent: AgentConfig | None = None
    watches: list[Watch] = Field(default_factory=list)
    suppress: SuppressConfig = Field(default_factory=SuppressConfig)
    agent_proposed_updates: ProposedUpdates = Field(default_factory=ProposedUpdates)

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        """Load a profile from a YAML file. Raises pydantic ValidationError on
        schema mismatch; callers should surface a clear error to the user (or
        the agent that's editing it)."""
        p = Path(path).expanduser()
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)

    def save(self, path: str | Path, *, edited_by: Literal["agent", "human"] = "agent") -> None:
        """Write the profile back to YAML. Updates ``_meta.last_edited_at``
        and ``last_edited_by`` so a future read can tell who touched it last
        (human escape hatch vs. agent-driven evolution)."""
        self.meta.last_edited_at = datetime.now(timezone.utc).isoformat()
        self.meta.last_edited_by = edited_by
        # by_alias=True so we serialize as `_meta:` not `meta:`. exclude_none
        # off so explicit nulls survive a round-trip.
        data: dict[str, Any] = self.model_dump(by_alias=True, mode="json")
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling tmp file then rename — torn-write safety. Profiles
        # are small, so the cost is negligible.
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        tmp.replace(p)

    def matches_owned(self, deal: dict[str, Any]) -> bool:
        """Return True if the deal references a product in already_owned.

        Checks canonical_product_id first (preferred); falls back to a
        case-insensitive substring match on product_name.
        """
        canonical = deal.get("priority_product_id")
        for owned in self.suppress.already_owned:
            if owned.canonical_product_id and canonical and owned.canonical_product_id == canonical:
                return True
        # Substring fallback only when canonical_product_id wasn't set on the
        # event (common for orphan deals — most agent-discovered finds at the
        # long tail).
        if not canonical:
            name = (deal.get("product_name") or "").lower()
            for owned in self.suppress.already_owned:
                if owned.product_name and owned.product_name.lower() in name:
                    return True
        return False

    def matches_blacklist(self, deal: dict[str, Any]) -> bool:
        """Return True if the deal is suppressed by brand or keyword blacklist."""
        brand = (deal.get("brand") or "").lower()
        if any(b.lower() == brand for b in self.suppress.brands_blacklist if b):
            return True
        haystack = " ".join(
            str(deal.get(k, "")) for k in ("product_name", "brand", "deal_quality")
        ).lower()
        for kw in self.suppress.keywords_blacklist:
            if kw and kw.lower() in haystack:
                return True
        return False
