from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

logger = logging.getLogger("ccforensics.pricing")

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
_LITELLM_MAX_BYTES = 10 * 1024 * 1024  # 10 MB cap — real file is ~600 KB

#: First instant at which Claude Sonnet 5 bills at list price. Its launch
#: promotion runs through 2026-08-31 inclusive, so the cutover is midnight UTC
#: on 2026-09-01. Anthropic quotes the window by date without a timezone; UTC is
#: the assumption, and being a few hours off only matters for offline runs made
#: during that single night.
SONNET_5_PROMO_ENDS = datetime(2026, 9, 1, tzinfo=UTC)

#: Sonnet 5 launch-promotion rates: $2/1M input, $10/1M output. Cache rates are
#: written out rather than derived — ``0.000002 * 1.25`` is not exactly
#: ``0.0000025`` in binary floating point, and these values must compare equal
#: to LiteLLM's so the live and offline paths agree to the cent.
_SONNET_5_PROMO_RATES: dict[str, float] = {
    "input_cost_per_token": 0.000002,
    "output_cost_per_token": 0.00001,
    "cache_creation_input_token_cost": 0.0000025,
    "cache_creation_input_token_cost_above_1hr": 0.000004,
    "cache_read_input_token_cost": 0.0000002,
}

#: Sonnet 5 list rates: $3/1M input, $15/1M output. In effect from
#: ``SONNET_5_PROMO_ENDS``.
_SONNET_5_LIST_RATES: dict[str, float] = {
    "input_cost_per_token": 0.000003,
    "output_cost_per_token": 0.000015,
    "cache_creation_input_token_cost": 0.00000375,
    "cache_creation_input_token_cost_above_1hr": 0.000006,
    "cache_read_input_token_cost": 0.0000003,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(when: datetime | None) -> datetime:
    """Normalize a caller-supplied instant to UTC; naive input is assumed UTC.

    Without this a naive ``datetime`` would raise ``TypeError`` on comparison
    with the tz-aware cutover constant — an unhelpful crash deep inside cost
    annotation, for a caller that almost certainly meant UTC. Transcript
    timestamps are tz-aware already; the naive branch is for hand-built
    callers and tests.
    """
    if when is None:
        return _utc_now()
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


@dataclass(frozen=True)
class ModelPrice:
    input_cost: float
    output_cost: float
    cache_creation_cost: float  # 5-minute TTL rate (Anthropic: input * 1.25)
    cache_creation_1h_cost: float  # 1-hour TTL rate (Anthropic: input * 2.0)
    cache_read_cost: float

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> ModelPrice:
        inp = float(entry.get("input_cost_per_token", 0.0) or 0.0)
        out = float(entry.get("output_cost_per_token", 0.0) or 0.0)
        cc = entry.get("cache_creation_input_token_cost")
        cc_1h = entry.get("cache_creation_input_token_cost_above_1hr")
        cr = entry.get("cache_read_input_token_cost")
        # Anthropic cache multipliers used as fallback when LiteLLM doesn't
        # break out cache pricing: 1.25x input for 5m creation, 2.0x input
        # for 1h creation, 0.1x input for reads. The 5m fallback below is
        # intentionally ``inp * 0.25`` (pre-existing — fires only on models
        # missing the LiteLLM field, which is none of the current Claude 4.x
        # entries; see issue tracker for the broader cleanup).
        cache_create = float(cc) if cc is not None else inp * 0.25
        cache_create_1h = float(cc_1h) if cc_1h is not None else inp * 2.0
        cache_read = float(cr) if cr is not None else inp * 0.10
        return cls(
            input_cost=inp,
            output_cost=out,
            cache_creation_cost=cache_create,
            cache_creation_1h_cost=cache_create_1h,
            cache_read_cost=cache_read,
        )


def _candidates(model: str) -> list[str]:
    return [
        model,
        f"anthropic/{model}",
        f"claude-3-5-{model}",
        f"claude-3-{model}",
        f"claude-{model}",
    ]


def _lookup(model: str, data: dict[str, dict[str, Any]]) -> ModelPrice | None:
    """Fuzzy-resolve ``model`` against a single pricing dict (no fallback).

    Tries the candidate list (exact + common prefixes) first; falls back to
    substring match where a key is a substring of the model name, picking the
    longest such key for determinism. The reverse direction (model-substring-
    of-key) is intentionally dropped: it silently maps short aliases onto
    arbitrary bedrock/vertex variants with different pricing.
    """
    for c in _candidates(model):
        if c in data:
            return ModelPrice.from_entry(data[c])
    lowered = model.lower()
    best: tuple[str, dict[str, Any]] | None = None
    for k, v in data.items():
        kl = k.lower()
        if kl and kl in lowered and (best is None or len(kl) > len(best[0])):
            best = (k, v)
    if best is not None:
        logger.warning(
            "pricing: substring fallback resolved %r -> %r (no exact match)",
            model,
            best[0],
        )
        return ModelPrice.from_entry(best[1])
    return None


def resolve_pricing(
    model: str, data: dict[str, dict[str, Any]], at: datetime | None = None
) -> ModelPrice | None:
    """Resolve a model name to a ModelPrice, live data first.

    Looks up ``data`` (normally the LiteLLM table) via :func:`_lookup`. When
    that misses, consults the hardcoded current-model table as a last resort —
    so a model LiteLLM hasn't ingested yet (e.g. one released today) still
    prices, instead of resolving to NULL cost on every command. Maintained
    LiteLLM entries always win because ``data`` is tried first. Returns None
    only when neither table knows the model.

    ``at`` is forwarded to :func:`fallback_hardcoded` to pick the rate card
    that was in effect at that instant. It has no bearing on the live path:
    LiteLLM publishes only current rates, so historical messages resolved
    against live data are priced at today's rate regardless.
    """
    hit = _lookup(model, data)
    if hit is not None:
        return hit
    fb = fallback_hardcoded(at)
    if fb is not data:  # guard against re-scanning when data already IS the fallback
        return _lookup(model, fb)
    return None


def compute_message_cost(
    price: ModelPrice,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_creation: int | None,
    cache_read: int | None,
    cache_creation_1h: int | None = None,
    cache_creation_5m: int | None = None,
) -> float:
    """Compute message cost in USD.

    When the per-TTL split (``cache_creation_1h`` / ``cache_creation_5m``) is
    provided, each bucket is priced at its rate. When only the legacy total
    (``cache_creation``) is provided, the full amount is priced at the 5m
    rate — back-compat for transcripts written before Claude Code emitted
    the ``usage.cache_creation`` sub-object.
    """
    if cache_creation_1h is not None or cache_creation_5m is not None:
        cc_1h = cache_creation_1h or 0
        cc_5m = cache_creation_5m or 0
    else:
        cc_1h = 0
        cc_5m = cache_creation or 0
    return (
        (input_tokens or 0) * price.input_cost
        + (output_tokens or 0) * price.output_cost
        + cc_5m * price.cache_creation_cost
        + cc_1h * price.cache_creation_1h_cost
        + (cache_read or 0) * price.cache_read_cost
    )


def fallback_hardcoded(at: datetime | None = None) -> dict[str, dict[str, float]]:
    """Minimal offline fallback covering current Claude models.

    ``cache_creation_input_token_cost_above_1hr`` is the 1h-TTL rate (2.0x
    input). Pre-4.x models (claude-3-5-haiku) predate the 1h cache TTL so
    the field is omitted — ``ModelPrice.from_entry`` synthesizes 2.0x input
    as the fallback.

    ``at`` is the instant to price *as of* — for cost annotation that is the
    message's own timestamp, so a message is billed the rate that was in
    effect when it was actually sent, not the rate in effect whenever the
    index happens to be rebuilt. It only affects models with a scheduled price
    change (currently just Sonnet 5's launch promotion) and defaults to the
    current UTC time for callers with no specific instant in mind. Naive
    datetimes are read as UTC.
    """
    at = _as_utc(at)
    return {
        "claude-fable-5": {
            # Mythos-class flagship, released 2026-06-08/09. $10/1M input,
            # $50/1M output (2x Opus 4.8); standard Anthropic cache multipliers
            # (5m 1.25x, 1h 2.0x, read 0.1x). LiteLLM did not carry this model
            # when the entry was added, so it was then the live path; LiteLLM
            # has since ingested it at identical rates and now wins, leaving
            # this as offline insurance.
            "input_cost_per_token": 0.00001,
            "output_cost_per_token": 0.00005,
            "cache_creation_input_token_cost": 0.0000125,
            "cache_creation_input_token_cost_above_1hr": 0.00002,
            "cache_read_input_token_cost": 0.000001,
        },
        "claude-opus-5": {
            # Successor to Opus 4.8. Drop-in at Opus 4.8's rates: $5/1M
            # input, $25/1M output; standard Anthropic cache multipliers
            # (5m 1.25x, 1h 2.0x, read 0.1x). LiteLLM already carries this
            # model at the same rates, so this entry is offline insurance
            # rather than the live path. Fast mode bills differently
            # ($10/$50) — not modelled; see the deferred ``usage.speed``
            # pricing branch.
            "input_cost_per_token": 0.000005,
            "output_cost_per_token": 0.000025,
            "cache_creation_input_token_cost": 0.00000625,
            "cache_creation_input_token_cost_above_1hr": 0.00001,
            "cache_read_input_token_cost": 0.0000005,
        },
        # Promotional rates through 2026-08-31, list price thereafter — see
        # SONNET_5_PROMO_ENDS. Mirrors what LiteLLM serves, so an offline run
        # bills Sonnet 5 the same as an online one.
        "claude-sonnet-5": dict(
            _SONNET_5_PROMO_RATES if at < SONNET_5_PROMO_ENDS else _SONNET_5_LIST_RATES
        ),
        "claude-sonnet-4-5-20250929": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_creation_input_token_cost": 0.00000375,
            "cache_creation_input_token_cost_above_1hr": 0.000006,
            "cache_read_input_token_cost": 0.0000003,
        },
        "claude-haiku-4-5-20251001": {
            "input_cost_per_token": 0.000001,
            "output_cost_per_token": 0.000005,
            "cache_creation_input_token_cost": 0.00000125,
            "cache_creation_input_token_cost_above_1hr": 0.000002,
            "cache_read_input_token_cost": 0.0000001,
        },
        "claude-opus-4-1-20250805": {
            "input_cost_per_token": 0.000015,
            "output_cost_per_token": 0.000075,
            "cache_creation_input_token_cost": 0.00001875,
            "cache_creation_input_token_cost_above_1hr": 0.00003,
            "cache_read_input_token_cost": 0.0000015,
        },
        "claude-opus-4-5-20251101": {
            "input_cost_per_token": 0.000015,
            "output_cost_per_token": 0.000075,
            "cache_creation_input_token_cost": 0.00001875,
            "cache_creation_input_token_cost_above_1hr": 0.00003,
            "cache_read_input_token_cost": 0.0000015,
        },
        "claude-opus-4-6": {
            "input_cost_per_token": 0.000005,
            "output_cost_per_token": 0.000025,
            "cache_creation_input_token_cost": 0.00000625,
            "cache_creation_input_token_cost_above_1hr": 0.00001,
            "cache_read_input_token_cost": 0.0000005,
        },
        "claude-opus-4-7": {
            "input_cost_per_token": 0.000005,
            "output_cost_per_token": 0.000025,
            "cache_creation_input_token_cost": 0.00000625,
            "cache_creation_input_token_cost_above_1hr": 0.00001,
            "cache_read_input_token_cost": 0.0000005,
        },
        "claude-opus-4-8": {
            "input_cost_per_token": 0.000005,
            "output_cost_per_token": 0.000025,
            "cache_creation_input_token_cost": 0.00000625,
            "cache_creation_input_token_cost_above_1hr": 0.00001,
            "cache_read_input_token_cost": 0.0000005,
        },
        "claude-sonnet-4-6": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_creation_input_token_cost": 0.00000375,
            "cache_creation_input_token_cost_above_1hr": 0.000006,
            "cache_read_input_token_cost": 0.0000003,
        },
        "claude-sonnet-4-20250514": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_creation_input_token_cost": 0.00000375,
            "cache_creation_input_token_cost_above_1hr": 0.000006,
            "cache_read_input_token_cost": 0.0000003,
        },
        "claude-3-5-haiku-20241022": {
            "input_cost_per_token": 0.0000008,
            "output_cost_per_token": 0.000004,
            "cache_creation_input_token_cost": 0.000001,
            "cache_read_input_token_cost": 0.00000008,
        },
    }


PricingSource = Literal["fresh", "cached", "stale", "fallback"]


@dataclass
class PricingCache:
    cache_file: Path
    ttl_seconds: int = 24 * 60 * 60
    last_source: PricingSource | None = None

    def load_or_fetch(self, http_client: httpx.Client | None = None) -> dict[str, dict[str, Any]]:
        now = int(time.time())
        if self.cache_file.exists():
            try:
                wrapper = json.loads(self.cache_file.read_text())
                fetched_at = int(wrapper.get("fetched_at", 0))
                data = wrapper.get("data") or {}
                if data and now - fetched_at < self.ttl_seconds:
                    self.last_source = "cached"
                    return data
                try:
                    result = self._fetch_and_store(http_client, now)
                    self.last_source = "fresh"
                    return result
                except Exception as e:
                    logger.warning("pricing refresh failed (%s); using stale cache", e)
                    self.last_source = "stale"
                    return data
            except Exception as e:
                logger.warning("pricing cache unreadable (%s); refetching", e)
        try:
            result = self._fetch_and_store(http_client, now)
            self.last_source = "fresh"
            return result
        except Exception as e:
            logger.warning("pricing fetch failed (%s); using hardcoded fallback", e)
            self.last_source = "fallback"
            return fallback_hardcoded()

    def _fetch_and_store(self, client: httpx.Client | None, now: int) -> dict[str, dict[str, Any]]:
        client = client or httpx.Client(timeout=15.0, follow_redirects=False, verify=True)
        # Stream with a size cap so a misbehaving CDN can't OOM the tool.
        total = 0
        chunks: list[bytes] = []
        with client.stream("GET", LITELLM_URL) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > _LITELLM_MAX_BYTES:
                    raise ValueError(
                        f"pricing response exceeded {_LITELLM_MAX_BYTES} bytes; aborting"
                    )
                chunks.append(chunk)
        data: dict[str, dict[str, Any]] = json.loads(b"".join(chunks))
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps({"fetched_at": now, "data": data}))
        return data
