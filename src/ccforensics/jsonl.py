from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import KNOWN_TYPES, TranscriptEntry, parse_entry
from .pricing import compute_message_cost, resolve_pricing


@dataclass
class ParseResult:
    entries: list[TranscriptEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parse_errors: int = 0
    truncated_tail: bool = False
    unknown_types: set[str] = field(default_factory=set)
    seen_versions: set[str] = field(default_factory=set)


def parse_file(path: Path) -> ParseResult:
    """Stream-parse a JSONL file with defensive error handling.

    - Non-final line JSON-parse failure: counted + warned, continue.
    - Final line JSON-parse failure at EOF: silently marked as truncated_tail.
    - Unknown ``type`` values: recorded in unknown_types, entry kept.
    - File not found: raises FileNotFoundError.
    """
    if not path.exists():
        raise FileNotFoundError(path)

    result = ParseResult()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=False)
    last_idx = len(lines) - 1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            if i == last_idx:
                result.truncated_tail = True
            else:
                result.parse_errors += 1
                result.warnings.append(f"{path}:{i + 1}: malformed JSON, skipped")
            continue

        try:
            entry = parse_entry(raw)
        except Exception as e:
            result.parse_errors += 1
            result.warnings.append(
                f"{path}:{i + 1}: pydantic rejected line ({e.__class__.__name__}), skipped"
            )
            continue

        if entry.type not in KNOWN_TYPES:
            if entry.type not in result.unknown_types:
                result.warnings.append(f"{path}: unknown type {entry.type!r} (kept, non-billable)")
            result.unknown_types.add(entry.type)

        if entry.version:
            result.seen_versions.add(entry.version)

        result.entries.append(entry)

    return result


def dedup_key(entry: TranscriptEntry) -> str | None:
    """Compute a tiered dedup key. Prefix prevents cross-tier collisions.

    Priority:
    1. ``req:<message.id>:<requestId>``   (billing-accurate)
    2. ``session:<message.id>:<sessionId>``  (fallback)

    Returns ``None`` when the entry has no ``message.id`` — caller passes
    the entry through un-deduped.
    """
    mid = entry.message.id if entry.message else None
    if mid and entry.request_id:
        return f"req:{mid}:{entry.request_id}"
    if mid and entry.session_id:
        return f"session:{mid}:{entry.session_id}"
    return None


def dedup_entries(entries: list[TranscriptEntry]) -> list[TranscriptEntry]:
    """Dedup by key; on collision the earliest-timestamped entry wins.

    Entries with no dedup_key (e.g., system events) pass through unchanged
    and their order relative to each other is preserved.
    """
    first_seen: dict[str, TranscriptEntry] = {}
    keyless: list[TranscriptEntry] = []

    for entry in entries:
        k = dedup_key(entry)
        if k is None:
            keyless.append(entry)
            continue
        prev = first_seen.get(k)
        if prev is None or entry.timestamp < prev.timestamp:
            first_seen[k] = entry

    deduped = list(first_seen.values())
    deduped.sort(key=lambda e: (e.timestamp, dedup_key(e) or ""))
    keyless.sort(key=lambda e: (e.timestamp, e.uuid or ""))
    return deduped + keyless


@dataclass
class AnnotatedEntry:
    entry: TranscriptEntry
    cost_usd: float | None
    pricing_unresolved_model: str | None = None


def annotate_cost(
    entries: list[TranscriptEntry],
    pricing_data: dict[str, dict[str, Any]],
) -> list[AnnotatedEntry]:
    """Annotate entries with cost.

    - Non-billable entries (anything that isn't an assistant turn with a
      ``message.usage``): ``cost_usd = 0.0``.
    - Assistant turns with a model whose pricing can't be resolved:
      ``cost_usd = None`` plus ``pricing_unresolved_model`` set.

    The 0.0/None split lets callers distinguish "intentionally zero"
    (non-billable system events, user prompts) from "we tried but couldn't
    compute" (LiteLLM didn't have the model).
    """
    out: list[AnnotatedEntry] = []
    unresolved: set[str] = set()
    for e in entries:
        if e.type != "assistant" or e.message is None or e.message.usage is None:
            out.append(AnnotatedEntry(entry=e, cost_usd=0.0))
            continue
        model = e.message.model
        if model is None:
            out.append(AnnotatedEntry(entry=e, cost_usd=None))
            continue
        price = resolve_pricing(model, pricing_data)
        if price is None:
            unresolved.add(model)
            out.append(AnnotatedEntry(entry=e, cost_usd=None, pricing_unresolved_model=model))
            continue
        usage = e.message.usage
        cost = compute_message_cost(
            price=price,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation=usage.cache_creation_input_tokens,
            cache_read=usage.cache_read_input_tokens,
        )
        out.append(AnnotatedEntry(entry=e, cost_usd=cost))
    return out
