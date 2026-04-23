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

    Iterates the file line-by-line rather than loading the whole text into
    memory. Truncation detection: a JSONL file that ends without a trailing
    newline has potentially-truncated last bytes; if that final partial line
    fails to parse it's marked ``truncated_tail`` instead of counted as a
    parse error.
    """
    if not path.exists():
        raise FileNotFoundError(path)

    file_size = path.stat().st_size
    final_line_is_complete = True
    if file_size > 0:
        with path.open("rb") as fb:
            fb.seek(-1, 2)
            final_line_is_complete = fb.read(1) == b"\n"

    result = ParseResult()
    last_line_num = 0
    last_was_blank = True

    with path.open("r", encoding="utf-8") as f:
        for line_num, raw_line in enumerate(f, start=1):
            last_line_num = line_num
            stripped = raw_line.strip()
            if not stripped:
                last_was_blank = True
                continue
            last_was_blank = False
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError:
                # We can't tell here whether this is "the last line" — only
                # the post-loop check (using the trailing-newline probe and
                # the final line number) can. Record as a parse error for
                # now; the retraction block at the bottom will downgrade to
                # truncated_tail if this turns out to have been the unfinished
                # last line.
                result.parse_errors += 1
                result.warnings.append(f"{path}:{line_num}: malformed JSON, skipped")
                continue

            try:
                entry = parse_entry(raw)
            except Exception as e:
                result.parse_errors += 1
                result.warnings.append(
                    f"{path}:{line_num}: pydantic rejected line ({e.__class__.__name__}), skipped"
                )
                continue

            if entry.type not in KNOWN_TYPES:
                if entry.type not in result.unknown_types:
                    result.warnings.append(
                        f"{path}: unknown type {entry.type!r} (kept, non-billable)"
                    )
                result.unknown_types.add(entry.type)

            if entry.version:
                result.seen_versions.add(entry.version)

            result.entries.append(entry)

    # Truncation: the file's last byte isn't a newline AND the very last
    # processed line was the one that failed to parse. Retract that error
    # and mark the tail truncated.
    if (
        not final_line_is_complete
        and not last_was_blank
        and result.warnings
        and result.warnings[-1].startswith(f"{path}:{last_line_num}: malformed JSON")
    ):
        result.warnings.pop()
        result.parse_errors -= 1
        result.truncated_tail = True

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


def _dedup_preference(entry: TranscriptEntry) -> tuple[int, int, float]:
    """Ranking key for dedup collision resolution (higher wins).

    Claude Code sometimes writes the same LLM response as two JSONL
    entries sharing a ``dedup_key`` but with different content (e.g., a
    streamed text block written first, then a tool_use block written
    when the tool call resolves). Usage is identical on both so cost is
    unaffected; but which entry we keep determines whether downstream
    spawn-linkage can see the tool_use.

    Priority (all higher = preferred):
    1. Entry has at least one ``tool_use`` block (load-bearing for
       cross-file spawn discovery in ``tree.py``).
    2. More non-empty content blocks (richer representation).
    3. Later timestamp (most recent write of the response).
    """
    has_tool_use = 0
    nonempty_blocks = 0
    if entry.message and entry.message.content:
        for b in entry.message.content:
            if b.type == "tool_use":
                has_tool_use = 1
            if b.type == "text":
                if b.text:
                    nonempty_blocks += 1
            elif b.type or b.id or b.name or b.content or b.input:
                nonempty_blocks += 1
    return (has_tool_use, nonempty_blocks, entry.timestamp.timestamp())


def dedup_entries(entries: list[TranscriptEntry]) -> list[TranscriptEntry]:
    """Dedup by key; on collision the content-richest entry wins.

    Entries with no dedup_key (e.g., system events) pass through unchanged
    and their order relative to each other is preserved.

    See ``_dedup_preference`` for the collision-resolution rule.
    """
    first_seen: dict[str, TranscriptEntry] = {}
    keyless: list[TranscriptEntry] = []

    for entry in entries:
        k = dedup_key(entry)
        if k is None:
            keyless.append(entry)
            continue
        prev = first_seen.get(k)
        if prev is None or _dedup_preference(entry) > _dedup_preference(prev):
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
