"""Thrash detection — signal extractors + session filter + composite scorer.

Signals operate on parsed ``TranscriptEntry`` lists (one session's worth) and
return ``list[Signal]``. Each extractor is pure, deterministic, no I/O. The
composite scorer + filter live alongside; the orchestrator hookpoint
(``populate_session_signals``) is added in a later task.

``SIGNAL_VERSION`` MUST be incremented on any threshold/weight/extractor
change. Mismatch with stored ``session_signals.signal_version`` triggers
recompute on the next index pass — see spec §1 (Architecture) and §3
(Composite scorer).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ccforensics.models import TranscriptEntry

SIGNAL_VERSION = 1

MIN_SESSION_TURNS = 20
LOW_TIER_MODEL_RE = re.compile(r"claude-(?:sonnet|haiku)-", re.IGNORECASE)


@dataclass(frozen=True)
class Signal:
    signal_type: str
    count: int
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------- session filter ----------


def primary_model(entries: list[TranscriptEntry]) -> str | None:
    """Most-frequent model across assistant turns. None if no assistant turns."""
    counts: dict[str, int] = defaultdict(int)
    for e in entries:
        if e.message and e.message.role == "assistant" and e.message.model:
            counts[e.message.model] += 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def assistant_turn_count(entries: list[TranscriptEntry]) -> int:
    return sum(1 for e in entries if e.message and e.message.role == "assistant")


def session_eligible(entries: list[TranscriptEntry]) -> bool:
    """Eligible for thrash scoring iff primary model is Sonnet/Haiku AND
    assistant turn count >= ``MIN_SESSION_TURNS``.

    Asymmetric scope: detect under-modeling, not over-modeling. Opus
    sessions and trivially short sessions are not candidates — see spec
    §0 (Out of scope) and §3 (Session filter).
    """
    model = primary_model(entries)
    if model is None or not LOW_TIER_MODEL_RE.search(model):
        return False
    return assistant_turn_count(entries) >= MIN_SESSION_TURNS


# ---------- signal extractors ----------


_PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*(?:#|//|--|/\*)\s*(?:TODO|FIXME|XXX|HACK|STUB|placeholder)\b",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(r"\bpass\s*#\s*(?:TODO|placeholder|stub)\b", re.IGNORECASE),
    re.compile(r"\braise\s+NotImplementedError\b"),
    re.compile(r"""throw\s+new\s+Error\(['"]not\s+implemented""", re.IGNORECASE),
)


def _iter_assistant_tool_uses(
    entries: list[TranscriptEntry],
) -> list[tuple[int, TranscriptEntry, Any]]:
    """Yield ``(turn_index, entry, content_block)`` for every assistant tool_use
    block. ``turn_index`` is the assistant-turn ordinal (0-based)."""
    out: list[tuple[int, TranscriptEntry, Any]] = []
    turn = -1
    for e in entries:
        if not (e.message and e.message.role == "assistant"):
            continue
        turn += 1
        for block in e.message.content:
            if block.type == "tool_use":
                out.append((turn, e, block))
    return out


def detect_placeholder_emit(
    entries: list[TranscriptEntry],
    threshold: int = 2,
) -> list[Signal]:
    """Assistant emits TODO / FIXME / NotImplementedError / stub markers via
    Edit or Write tool inputs — proxy for low-confidence completion
    (spec §3 — placeholder_emit).

    Validated by code-completion research ("uncertainty-aware code
    completion" — placeholder generation correlates with low model
    confidence) and industry "theater detection" tools.
    """
    matches: list[dict[str, Any]] = []
    for turn, _entry, block in _iter_assistant_tool_uses(entries):
        if block.name not in ("Edit", "Write"):
            continue
        if not block.input:
            continue
        content = block.input.get("new_string") or block.input.get("content") or ""
        if not isinstance(content, str):
            continue
        for pat in _PLACEHOLDER_PATTERNS:
            if pat.search(content):
                file_path = block.input.get("file_path") or block.input.get("path") or ""
                matches.append(
                    {
                        "turn": turn,
                        "file_path": file_path,
                        "marker": pat.pattern,
                    }
                )
                break

    if len(matches) < threshold:
        return []

    return [
        Signal(
            signal_type="placeholder_emit",
            count=len(matches),
            evidence={
                "matches": [m["marker"] for m in matches],
                "files": sorted({m["file_path"] for m in matches if m["file_path"]}),
                "turn_indices": [m["turn"] for m in matches],
            },
        )
    ]


_BASH_NONZERO_MARKERS: tuple[str, ...] = (
    "Error",
    "error:",
    "Traceback",
    "FAIL",
    "FAILED",
    "Exception",
    "exit code",
    "command not found",
)


def _block_is_tool_error(block: Any) -> bool:
    """Best-effort: tool_result block whose content/text contains an error
    marker. False for empty / non-error results."""
    if block.type != "tool_result":
        return False
    raw = block.content
    if isinstance(raw, list):
        text = " ".join((b.get("text") or "") for b in raw if isinstance(b, dict))
    elif isinstance(raw, str):
        text = raw
    else:
        text = block.text or ""
    return any(marker in text for marker in _BASH_NONZERO_MARKERS)


def _iter_tool_results(
    entries: list[TranscriptEntry],
) -> list[tuple[int, TranscriptEntry, Any]]:
    """Yield ``(turn_index, entry, tool_result_block)`` per result block.

    Tool results live on USER turns in Claude Code's transcript shape —
    the user message that follows the assistant tool_use carries the
    matching tool_result block. Turn index is the assistant-turn ordinal
    that PRECEDES this result (i.e., the turn that issued the tool_use).
    """
    out: list[tuple[int, TranscriptEntry, Any]] = []
    last_assistant_turn = -1
    for e in entries:
        if not e.message:
            continue
        if e.message.role == "assistant":
            last_assistant_turn += 1
            continue
        if e.message.role != "user":
            continue
        for block in e.message.content:
            if block.type == "tool_result":
                out.append((last_assistant_turn, e, block))
    return out


def detect_repeated_edit(
    entries: list[TranscriptEntry],
    threshold: int = 4,
    min_distinct_errors: int = 2,
) -> list[Signal]:
    """Same file edited >= ``threshold`` times with >= ``min_distinct_errors``
    distinct errors observed during the edit window (spec §3 —
    repeated_edit). The error guard avoids flagging a normal multi-step
    refactor as thrash.
    """
    edits_by_path: dict[str, list[int]] = defaultdict(list)
    for turn, _entry, block in _iter_assistant_tool_uses(entries):
        if block.name not in ("Edit", "Write"):
            continue
        if not block.input:
            continue
        path = block.input.get("file_path") or block.input.get("path")
        if not isinstance(path, str) or not path:
            continue
        edits_by_path[path].append(turn)

    error_turns_to_hashes: dict[int, str] = {}
    for turn, _entry, block in _iter_tool_results(entries):
        if not _block_is_tool_error(block):
            continue
        raw = block.content if isinstance(block.content, str) else (block.text or "")
        if isinstance(block.content, list):
            raw = " ".join((b.get("text") or "") for b in block.content if isinstance(b, dict))
        excerpt = _normalize_error(raw)[:120]
        error_turns_to_hashes[turn] = excerpt

    signals: list[Signal] = []
    for path, turn_list in edits_by_path.items():
        if len(turn_list) < threshold:
            continue
        first, last = turn_list[0], turn_list[-1]
        distinct = {excerpt for t, excerpt in error_turns_to_hashes.items() if first <= t <= last}
        if len(distinct) < min_distinct_errors:
            continue
        signals.append(
            Signal(
                signal_type="repeated_edit",
                count=len(turn_list),
                evidence={
                    "file_path": path,
                    "edit_count": len(turn_list),
                    "first_turn": first,
                    "last_turn": last,
                    "distinct_errors_during_window": len(distinct),
                },
            )
        )

    if not signals:
        return []

    # Multiple paths can fire; collapse to a single composite Signal whose
    # count is the worst (max) edit_count and whose evidence carries the
    # offending file. The session-level scorer cares about presence +
    # magnitude, not per-file decomposition (which is preserved in
    # ``evidence`` anyway).
    worst = max(signals, key=lambda s: s.count)
    return [worst]


_ERROR_LINE_RE = re.compile(r"(?im)^.{0,400}?(?:Error|Traceback|FAIL|.*Error:|.*Exception:).*$")
_NORMALIZE_DIGITS = re.compile(r"\d+")
_NORMALIZE_HEX = re.compile(r"\b[0-9a-f]{4,}\b", re.IGNORECASE)
_NORMALIZE_PATH = re.compile(r"/[^\s'\"]+")
_NORMALIZE_TIME = re.compile(r"\d{2}:\d{2}:\d{2}")
_NORMALIZE_WS = re.compile(r"\s+")


def _normalize_error(text: str) -> str:
    """Strip volatile elements (digits, hex, paths, timestamps) and lowercase
    so the same logical error dedups across runs that report different
    line numbers / paths / addresses (spec §3 — repeated_error)."""
    text = _NORMALIZE_TIME.sub("", text)
    text = _NORMALIZE_PATH.sub("", text)
    text = _NORMALIZE_HEX.sub("", text)
    text = _NORMALIZE_DIGITS.sub("", text)
    return _NORMALIZE_WS.sub(" ", text).strip().lower()


def detect_repeated_error(
    entries: list[TranscriptEntry],
    threshold: int = 3,
) -> list[Signal]:
    """Same normalized error excerpt surfaces in tool_result content
    >= ``threshold`` times across distinct turns (spec §3 —
    repeated_error). Normalization by prefix-hash is robust to novel
    error types and partial stack traces.
    """
    by_excerpt: dict[str, list[int]] = defaultdict(list)
    raw_excerpts: dict[str, str] = {}
    for turn, _entry, block in _iter_tool_results(entries):
        raw = block.content if isinstance(block.content, str) else (block.text or "")
        if isinstance(block.content, list):
            raw = " ".join((b.get("text") or "") for b in block.content if isinstance(b, dict))
        if not raw:
            continue
        match = _ERROR_LINE_RE.search(raw)
        if not match:
            continue
        normalized = _normalize_error(match.group(0))[:120]
        if not normalized:
            continue
        by_excerpt[normalized].append(turn)
        raw_excerpts.setdefault(normalized, match.group(0).strip())

    signals: list[Signal] = []
    for excerpt, turns in by_excerpt.items():
        unique_turns = sorted(set(turns))
        if len(unique_turns) < threshold:
            continue
        signals.append(
            Signal(
                signal_type="repeated_error",
                count=len(unique_turns),
                evidence={
                    "error_excerpt": raw_excerpts[excerpt][:200],
                    "occurrences": len(unique_turns),
                    "first_turn": unique_turns[0],
                    "last_turn": unique_turns[-1],
                },
            )
        )

    if not signals:
        return []

    worst = max(signals, key=lambda s: s.count)
    return [worst]
