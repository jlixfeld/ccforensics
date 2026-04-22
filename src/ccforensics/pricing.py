from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("ccforensics.pricing")

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)


@dataclass(frozen=True)
class ModelPrice:
    input_cost: float
    output_cost: float
    cache_creation_cost: float
    cache_read_cost: float

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> ModelPrice:
        inp = float(entry.get("input_cost_per_token", 0.0) or 0.0)
        out = float(entry.get("output_cost_per_token", 0.0) or 0.0)
        cc = entry.get("cache_creation_input_token_cost")
        cr = entry.get("cache_read_input_token_cost")
        cache_create = float(cc) if cc is not None else inp * 0.25
        cache_read = float(cr) if cr is not None else inp * 0.10
        return cls(
            input_cost=inp,
            output_cost=out,
            cache_creation_cost=cache_create,
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


def resolve_pricing(model: str, data: dict[str, dict[str, Any]]) -> ModelPrice | None:
    """Resolve a model name to a ModelPrice via fuzzy lookup."""
    for c in _candidates(model):
        if c in data:
            return ModelPrice.from_entry(data[c])
    lowered = model.lower()
    for k, v in data.items():
        kl = k.lower()
        if lowered in kl or kl in lowered:
            return ModelPrice.from_entry(v)
    return None


def compute_message_cost(
    price: ModelPrice,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_creation: int | None,
    cache_read: int | None,
) -> float:
    return (
        (input_tokens or 0) * price.input_cost
        + (output_tokens or 0) * price.output_cost
        + (cache_creation or 0) * price.cache_creation_cost
        + (cache_read or 0) * price.cache_read_cost
    )


def fallback_hardcoded() -> dict[str, dict[str, float]]:
    """Minimal offline fallback covering current Claude models."""
    return {
        "claude-sonnet-4-5-20250929": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_creation_input_token_cost": 0.00000375,
            "cache_read_input_token_cost": 0.0000003,
        },
        "claude-haiku-4-5-20251001": {
            "input_cost_per_token": 0.000001,
            "output_cost_per_token": 0.000005,
            "cache_creation_input_token_cost": 0.00000125,
            "cache_read_input_token_cost": 0.0000001,
        },
        "claude-opus-4-1-20250805": {
            "input_cost_per_token": 0.000015,
            "output_cost_per_token": 0.000075,
            "cache_creation_input_token_cost": 0.00001875,
            "cache_read_input_token_cost": 0.0000015,
        },
        "claude-opus-4-5-20251101": {
            "input_cost_per_token": 0.000015,
            "output_cost_per_token": 0.000075,
            "cache_creation_input_token_cost": 0.00001875,
            "cache_read_input_token_cost": 0.0000015,
        },
        "claude-sonnet-4-20250514": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_creation_input_token_cost": 0.00000375,
            "cache_read_input_token_cost": 0.0000003,
        },
        "claude-3-5-haiku-20241022": {
            "input_cost_per_token": 0.0000008,
            "output_cost_per_token": 0.000004,
            "cache_creation_input_token_cost": 0.000001,
            "cache_read_input_token_cost": 0.00000008,
        },
    }


@dataclass
class PricingCache:
    cache_file: Path
    ttl_seconds: int = 24 * 60 * 60

    def load_or_fetch(
        self, http_client: httpx.Client | None = None
    ) -> dict[str, dict[str, Any]]:
        now = int(time.time())
        if self.cache_file.exists():
            try:
                wrapper = json.loads(self.cache_file.read_text())
                fetched_at = int(wrapper.get("fetched_at", 0))
                data = wrapper.get("data") or {}
                if data and now - fetched_at < self.ttl_seconds:
                    return data
                try:
                    return self._fetch_and_store(http_client, now)
                except Exception as e:
                    logger.warning("pricing refresh failed (%s); using stale cache", e)
                    return data
            except Exception as e:
                logger.warning("pricing cache unreadable (%s); refetching", e)
        try:
            return self._fetch_and_store(http_client, now)
        except Exception as e:
            logger.warning("pricing fetch failed (%s); using hardcoded fallback", e)
            return fallback_hardcoded()

    def _fetch_and_store(
        self, client: httpx.Client | None, now: int
    ) -> dict[str, dict[str, Any]]:
        client = client or httpx.Client(timeout=15.0)
        resp = client.get(LITELLM_URL)
        resp.raise_for_status()
        data: dict[str, dict[str, Any]] = resp.json()
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps({"fetched_at": now, "data": data}))
        return data
