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

import hashlib
import json
import math
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


# ---------- T2 + T3 extractors ----------


def _tool_result_text(block: Any) -> str:
    """Best-effort string extraction from a tool_result content block.

    Tool results land as either a bare string, a list of dicts with
    ``text`` keys, or a pydantic-parsed block whose ``content`` mirrors
    the same shapes. Centralized so the extractors share one normalizer.
    """
    raw = block.content
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                txt = item.get("text") or ""
                if isinstance(txt, str):
                    parts.append(txt)
        return " ".join(parts)
    return getattr(block, "text", "") or ""


def _canonical_input_hash(tool_input: Any) -> str:
    """Stable hash of a tool's ``input`` dict — used for tool_arg_churn
    duplicate detection."""
    try:
        serialized = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = repr(tool_input)
    return hashlib.sha256(serialized.encode()).hexdigest()


def _result_excerpt_hash(text: str) -> str:
    """Hash first 200 chars of normalized text. Disambiguates flake-retry
    (varying results) from true churn (identical results)."""
    return hashlib.sha256(_normalize_error(text)[:200].encode()).hexdigest()


def detect_tool_arg_churn(
    entries: list[TranscriptEntry],
    threshold: int = 3,
) -> list[Signal]:
    """Same ``(tool_name, args)`` AND same result content repeated
    >= ``threshold`` times in distinct turns (spec §3 — tool_arg_churn).

    Result-variation suppression: a Bash command that flakes returns
    different results across invocations → not churn. Industry ReAct
    loop detection uses ``(function, args)`` action keys; we extend
    with the result hash to distinguish flake-retry from true churn.
    """
    uses: dict[str, tuple[int, str, str]] = {}
    for turn, _entry, block in _iter_assistant_tool_uses(entries):
        if not block.id or not block.name:
            continue
        uses[block.id] = (turn, block.name, _canonical_input_hash(block.input))

    grouped: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    for _turn, _entry, result_block in _iter_tool_results(entries):
        tool_use_id = getattr(result_block, "tool_use_id", None)
        if not tool_use_id or tool_use_id not in uses:
            continue
        use_turn, tool_name, arg_hash = uses[tool_use_id]
        result_hash = _result_excerpt_hash(_tool_result_text(result_block))
        grouped[(tool_name, arg_hash)].append((use_turn, result_hash))

    signals: list[Signal] = []
    for (tool_name, arg_hash), occurrences in grouped.items():
        if len(occurrences) < threshold:
            continue
        result_hashes = {r for _, r in occurrences}
        if len(result_hashes) > 1:
            # Result varies → not churn (likely flake retry).
            continue
        signals.append(
            Signal(
                signal_type="tool_arg_churn",
                count=len(occurrences),
                evidence={
                    "tool_name": tool_name,
                    "arg_hash": arg_hash[:16],
                    "result_variation": False,
                    "repeats": len(occurrences),
                    "turn_indices": [t for t, _ in occurrences],
                },
            )
        )

    if not signals:
        return []
    return [max(signals, key=lambda s: s.count)]


_USER_CORRECTION_RE = re.compile(
    r"\b(no|nope|wrong|still|broken|try again|that'?s not|not (?:right|it)|"
    r"doesn'?t work|didn'?t work|fix it|same (?:error|issue))\b",
    re.IGNORECASE,
)
_USER_CORRECTION_MAX_TOKENS = 20


def _user_text_blocks(message_content: Any) -> str:
    """Concatenate text from a user message's content list (or a bare
    string). Returns empty string if nothing text-shaped is present."""
    if isinstance(message_content, str):
        return message_content
    if not isinstance(message_content, list):
        return ""
    parts: list[str] = []
    for block in message_content:
        if isinstance(block, dict):
            txt = block.get("text") or ""
            if isinstance(txt, str):
                parts.append(txt)
            continue
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return " ".join(parts).strip()


def detect_user_correction(
    entries: list[TranscriptEntry],
    threshold: int = 2,
) -> list[Signal]:
    """Short user message (<20 tokens) matching correction-shape regex,
    fired >= ``threshold`` times across the session (spec §3 —
    user_correction). The first user message is excluded — it's the
    initial prompt, not a correction.
    """
    matches: list[tuple[int, str]] = []
    user_index = -1
    for e in entries:
        if not e.message or e.message.role != "user":
            continue
        user_index += 1
        if user_index == 0:
            continue
        text = _user_text_blocks(e.message.content)
        if not text:
            continue
        if len(text.split()) >= _USER_CORRECTION_MAX_TOKENS:
            continue
        m = _USER_CORRECTION_RE.search(text)
        if m:
            matches.append((user_index, m.group(0).lower()))

    if len(matches) < threshold:
        return []

    return [
        Signal(
            signal_type="user_correction",
            count=len(matches),
            evidence={
                "matches": [m[1] for m in matches],
                "turn_indices": [m[0] for m in matches],
            },
        )
    ]


_RESOLUTION_RE = re.compile(
    r"\b(thanks|done|perfect|great|works|fixed|ok|got it|merged|shipped|nice)\b",
    re.IGNORECASE,
)


def _last_message(entries: list[TranscriptEntry]) -> TranscriptEntry | None:
    for e in reversed(entries):
        if e.message:
            return e
    return None


def _last_tool_result_is_error(entries: list[TranscriptEntry]) -> bool:
    for e in reversed(entries):
        if not e.message or e.message.role != "user":
            continue
        for block in e.message.content:
            if block.type == "tool_result":
                return _block_is_tool_error(block)
    return False


def _last_user_text_is_resolution(entries: list[TranscriptEntry]) -> bool | None:
    """``True`` if the last user message includes a resolution marker;
    ``False`` if it does not; ``None`` if there is no final user
    message (i.e., session ends mid-assistant turn — explicit signal).
    """
    last_user_text: str | None = None
    for e in reversed(entries):
        if not e.message or e.message.role != "user":
            continue
        text = _user_text_blocks(e.message.content)
        if text:
            last_user_text = text
            break
    if last_user_text is None:
        return None
    return bool(_RESOLUTION_RE.search(last_user_text))


def _wall_clock_seconds(entries: list[TranscriptEntry]) -> int:
    """Total wall-clock duration (rounded seconds) from first to last
    timestamp. Returns 0 for empty / single-entry sessions."""
    if len(entries) < 2:
        return 0
    first = entries[0].timestamp
    last = entries[-1].timestamp
    return int((last - first).total_seconds())


def detect_session_abandoned(
    entries: list[TranscriptEntry],
    min_turns: int = 20,
) -> list[Signal]:
    """Long session ended without a resolution marker (spec §3 —
    session_abandoned). Fires only when assistant turns >= ``min_turns``
    AND any of: last role is assistant, last tool result is error,
    last user message lacks resolution-shape text.
    """
    total = assistant_turn_count(entries)
    if total < min_turns:
        return []

    last = _last_message(entries)
    if last is None:
        return []

    last_role = last.message.role if last.message else "unknown"
    last_tool_error = _last_tool_result_is_error(entries)
    last_user_resolved = _last_user_text_is_resolution(entries)

    if last_role == "assistant" or last_tool_error or last_user_resolved is False:
        return [
            Signal(
                signal_type="session_abandoned",
                count=total,
                evidence={
                    "total_turns": total,
                    "last_role": last_role,
                    "last_tool_error": last_tool_error,
                    "wall_clock_total_seconds": _wall_clock_seconds(entries),
                },
            )
        ]
    return []


# ---------- T3 extractors ----------


def _record_errors_into_set(entry: TranscriptEntry, seen_error_hashes: set[str]) -> bool:
    """Update ``seen_error_hashes`` from any tool_result errors in this
    user-turn entry. Returns True if a NEW error hash was added."""
    if not entry.message or entry.message.role != "user":
        return False
    new_error = False
    for block in entry.message.content:
        if block.type != "tool_result":
            continue
        text = _tool_result_text(block)
        m = _ERROR_LINE_RE.search(text)
        if not m:
            continue
        normalized = _normalize_error(m.group(0))[:120]
        if normalized and normalized not in seen_error_hashes:
            seen_error_hashes.add(normalized)
            new_error = True
    return new_error


def _flat_run_signature(
    entry: TranscriptEntry,
    seen_files: set[str],
    seen_tool_names: set[str],
) -> tuple[bool, str]:
    """Return ``(is_flat, assistant_text)`` for one assistant turn after
    updating the running sets. ``is_flat`` means the turn produced no
    new file/tool. Caller updates the shared error-hash set separately
    via ``_record_errors_into_set`` on user turns."""
    if not entry.message or entry.message.role != "assistant":
        return False, ""
    new_file = False
    new_tool = False
    text_parts: list[str] = []
    for block in entry.message.content:
        if block.type == "tool_use" and block.name:
            if block.name not in seen_tool_names:
                seen_tool_names.add(block.name)
                new_tool = True
            if block.name in ("Edit", "Write") and block.input:
                path = block.input.get("file_path") or block.input.get("path")
                if isinstance(path, str) and path and path not in seen_files:
                    seen_files.add(path)
                    new_file = True
        elif block.type == "text" and block.text:
            text_parts.append(block.text)
    return (not (new_file or new_tool)), " ".join(text_parts)


def _char_set_jaccard(a: str, b: str) -> float:
    """Character-set Jaccard similarity. Used for "deep debugging"
    suppression — varied reasoning text across consecutive flat turns
    should NOT count as a thrash window even when files/errors/tools
    haven't grown."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / max(1, len(sa | sb))


def _close_flat_run(run_len: int, run_start: int | None, run_texts: list[str]) -> dict[str, Any]:
    pairwise: list[float] = []
    for i in range(1, len(run_texts)):
        pairwise.append(_char_set_jaccard(run_texts[i - 1], run_texts[i]))
    return {
        "max_flat_run": run_len,
        "from_turn": run_start if run_start is not None else 0,
        "to_turn": (run_start or 0) + run_len - 1,
        "new_files_in_window": 0,
        "new_errors_in_window": 0,
        "text_jaccard_max": max(pairwise) if pairwise else 1.0,
    }


def detect_novelty_window(
    entries: list[TranscriptEntry],
    window: int = 6,
    threshold: int = 2,
    jaccard_min: float = 0.85,
) -> list[Signal]:
    """N consecutive turns w/ no new unique file/tool/error AND high
    text-similarity across consecutive assistant turns (spec §3 —
    novelty_window). Formalizes the industry ``no_progress_steps``
    metric; jaccard suppression prevents false-positive on legitimate
    deep debugging.
    """
    seen_files: set[str] = set()
    seen_error_hashes: set[str] = set()
    seen_tool_names: set[str] = set()

    flat_runs: list[dict[str, Any]] = []
    cur_run_len = 0
    cur_run_start: int | None = None
    cur_run_texts: list[str] = []
    assistant_turn = -1

    for e in entries:
        if e.message and e.message.role == "user":
            new_err = _record_errors_into_set(e, seen_error_hashes)
            if new_err and cur_run_len > 0:
                if cur_run_len >= window:
                    flat_runs.append(_close_flat_run(cur_run_len, cur_run_start, cur_run_texts))
                cur_run_len = 0
                cur_run_texts = []
                cur_run_start = None
            continue
        if not e.message or e.message.role != "assistant":
            continue
        assistant_turn += 1
        is_flat, text = _flat_run_signature(e, seen_files, seen_tool_names)
        if is_flat:
            if cur_run_len == 0:
                cur_run_start = assistant_turn
            cur_run_len += 1
            cur_run_texts.append(text)
        else:
            if cur_run_len >= window:
                flat_runs.append(_close_flat_run(cur_run_len, cur_run_start, cur_run_texts))
            cur_run_len = 0
            cur_run_texts = []
            cur_run_start = None

    if cur_run_len >= window:
        flat_runs.append(_close_flat_run(cur_run_len, cur_run_start, cur_run_texts))

    qualifying = [r for r in flat_runs if r["text_jaccard_max"] >= jaccard_min]

    if len(qualifying) < threshold:
        return []

    worst = max(qualifying, key=lambda r: r["max_flat_run"])
    return [
        Signal(
            signal_type="novelty_window",
            count=worst["max_flat_run"],
            evidence=worst,
        )
    ]


def _least_squares_slope(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Return ``(slope, r_squared)`` for the linear fit. Returns
    ``(0.0, 0.0)`` when input is degenerate (constant x or fewer than
    2 points)."""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    if ss_xx == 0:
        return 0.0, 0.0
    slope = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot == 0:
        return slope, 0.0
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True))
    r_squared = 1.0 - (ss_res / ss_tot)
    return slope, max(0.0, r_squared)


def detect_turn_cost_acceleration(
    entries: list[TranscriptEntry],
    min_turns: int = 15,
    r2_threshold: float = 0.55,
    window: int = 30,
) -> list[Signal]:
    """Output-tokens-per-turn has positive slope w/ r² above threshold
    over the last ``window`` assistant turns (spec §3 —
    turn_cost_acceleration).

    Output tokens (not total cost) — output is what the model produced;
    input is dominated by accumulated tool output that grows
    mechanically and confounds the signal.
    """
    series: list[tuple[int, int]] = []
    turn = -1
    for e in entries:
        if not e.message or e.message.role != "assistant":
            continue
        turn += 1
        usage = e.message.usage
        if usage is None or usage.output_tokens is None:
            continue
        series.append((turn, usage.output_tokens))

    if len(series) < min_turns:
        return []

    sliced = series[-window:]
    xs = [float(t) for t, _ in sliced]
    ys = [float(o) for _, o in sliced]
    slope, r_squared = _least_squares_slope(xs, ys)

    if slope <= 0 or r_squared < r2_threshold:
        return []

    return [
        Signal(
            signal_type="turn_cost_acceleration",
            count=int(slope),
            evidence={
                "slope_output_tokens_per_turn": round(slope, 2),
                "window_start_turn": int(sliced[0][0]),
                "window_end_turn": int(sliced[-1][0]),
                "r_squared": round(r_squared, 3),
            },
        )
    ]


_TEST_RUNNER_RE = re.compile(
    r"\b(pytest|jest|npm\s+test|cargo\s+test|go\s+test|mvn\s+test|make\s+test)\b",
    re.IGNORECASE,
)
_PYTEST_FAIL_RE = re.compile(r"(\d+)\s+failed", re.IGNORECASE)
_JEST_FAIL_RE = re.compile(r"Tests:\s+(\d+)\s+failed", re.IGNORECASE)
_GO_FAIL_RE = re.compile(r"(?im)^FAIL\s+\S+")
_GENERIC_FAIL_RE = re.compile(r"(?im)^(?:FAIL|E\s)")


def _parse_fail_count(output: str) -> int | None:
    """Return the failed-test count from a test runner output, or None
    if the output doesn't look like a recognized runner result."""
    m = _PYTEST_FAIL_RE.search(output)
    if m:
        return int(m.group(1))
    m = _JEST_FAIL_RE.search(output)
    if m:
        return int(m.group(1))
    go_matches = _GO_FAIL_RE.findall(output)
    if go_matches:
        return len(go_matches)
    generic = _GENERIC_FAIL_RE.findall(output)
    if generic:
        return len(generic)
    return None


def detect_test_regression(
    entries: list[TranscriptEntry],
    threshold: int = 1,
) -> list[Signal]:
    """Test fail count rises across consecutive runs with at least one
    Edit between them (spec §3 — test_regression). Validated by
    SWE-bench's fail2pass / pass2pass framework as a recognized
    "made it worse" signal.
    """
    edit_turns: list[int] = []
    use_id_to_command: dict[str, tuple[int, str]] = {}

    for turn, _entry, block in _iter_assistant_tool_uses(entries):
        if block.name == "Bash" and block.input:
            cmd = block.input.get("command") or ""
            if isinstance(cmd, str) and _TEST_RUNNER_RE.search(cmd) and block.id:
                use_id_to_command[block.id] = (turn, cmd[:200])
        elif block.name in ("Edit", "Write"):
            edit_turns.append(turn)

    test_runs: list[tuple[int, int, str]] = []
    for _turn, _entry, result_block in _iter_tool_results(entries):
        tool_use_id = getattr(result_block, "tool_use_id", None)
        if not tool_use_id or tool_use_id not in use_id_to_command:
            continue
        run_turn, cmd = use_id_to_command[tool_use_id]
        text = _tool_result_text(result_block)
        fail_count = _parse_fail_count(text)
        if fail_count is None:
            continue
        test_runs.append((run_turn, fail_count, cmd))

    test_runs.sort(key=lambda r: r[0])

    regressions: list[dict[str, Any]] = []
    for i in range(1, len(test_runs)):
        prev_turn, prev_fails, _prev_cmd = test_runs[i - 1]
        cur_turn, cur_fails, cur_cmd = test_runs[i]
        if cur_fails <= prev_fails:
            continue
        edits_between = [t for t in edit_turns if prev_turn < t < cur_turn]
        if not edits_between:
            continue
        regressions.append(
            {
                "tool_name": "Bash",
                "command_excerpt": cur_cmd,
                "fail_count_before": prev_fails,
                "fail_count_after": cur_fails,
                "edit_between_turns": edits_between,
            }
        )

    if len(regressions) < threshold:
        return []

    worst = max(regressions, key=lambda r: r["fail_count_after"] - r["fail_count_before"])
    return [
        Signal(
            signal_type="test_regression",
            count=len(regressions),
            evidence=worst,
        )
    ]


# ---------- trajectory_length_zscore ----------


@dataclass(frozen=True)
class BaselineStats:
    """Per-user, per-primary-model baseline of historical session lengths.
    Built by the orchestrator (T4) over the last 90 days, excluding the
    sessions in the current evaluation window to avoid self-reference.
    """

    primary_model: str
    mean_turns: float
    stddev_turns: float
    n_sessions: int


def detect_trajectory_length_zscore(
    entries: list[TranscriptEntry],
    baseline: BaselineStats | None,
    z_threshold: float = 2.0,
    min_baseline_n: int = 20,
) -> list[Signal]:
    """Session length is anomalously long compared to user's baseline
    for this primary model (spec §3 — trajectory_length_zscore).

    Validated by SWE-EVAL trajectory analysis: trajectory length and
    variance correlate w/ failure modes. Personalizing to the user
    baseline avoids penalizing users w/ long routine sessions.
    """
    if baseline is None or baseline.n_sessions < min_baseline_n:
        return []
    if baseline.stddev_turns <= 0:
        return []
    session_turns = assistant_turn_count(entries)
    z = (session_turns - baseline.mean_turns) / baseline.stddev_turns
    if not math.isfinite(z) or z < z_threshold:
        return []
    return [
        Signal(
            signal_type="trajectory_length_zscore",
            count=session_turns,
            evidence={
                "session_turns": session_turns,
                "user_baseline_mean_turns": round(baseline.mean_turns, 2),
                "user_baseline_stddev": round(baseline.stddev_turns, 2),
                "z_score": round(z, 2),
                "primary_model": baseline.primary_model,
            },
        )
    ]


# ---------- T4: composite scorer + orchestrator ----------


WEIGHTS: dict[str, float] = {
    "novelty_window": 0.22,
    "test_regression": 0.18,
    "repeated_edit": 0.15,
    "repeated_error": 0.12,
    "placeholder_emit": 0.10,
    "user_correction": 0.08,
    "trajectory_length_zscore": 0.06,
    "tool_arg_churn": 0.05,
    "turn_cost_acceleration": 0.02,
    "session_abandoned": 0.02,
}

THRESHOLDS: dict[str, int] = {
    "novelty_window": 6,
    "test_regression": 1,
    "repeated_edit": 4,
    "repeated_error": 3,
    "placeholder_emit": 2,
    "user_correction": 2,
    "trajectory_length_zscore": 1,
    "tool_arg_churn": 3,
    "turn_cost_acceleration": 1,
    "session_abandoned": 20,
}

FLAG_THRESHOLD = 0.40
MIN_FIRED_SIGNAL_TYPES = 2


def compute_thrash_score(signals: list[Signal]) -> float:
    """Composite in [0.0, 1.0]. Each fired signal contributes its full
    weight; counts beyond the per-signal threshold add a log-scaled
    bonus capped at 1.5x. Sessions with fewer than
    ``MIN_FIRED_SIGNAL_TYPES`` distinct signals score below
    ``FLAG_THRESHOLD`` by construction (single-signal cap = 0.22 x 1.5
    = 0.33; well below 0.40 flag gate).
    """
    fired = {s.signal_type: s.count for s in signals}
    score = 0.0
    for sig_type, weight in WEIGHTS.items():
        if sig_type not in fired:
            continue
        count = max(1, fired[sig_type])
        threshold = THRESHOLDS.get(sig_type, 1)
        bonus = min(0.5, 0.1 * math.log2(max(1.0, count / threshold)))
        score += weight * (1.0 + bonus)
    return min(1.0, max(0.0, score))


def is_flagged(signals: list[Signal]) -> bool:
    """Session must fire >= MIN_FIRED_SIGNAL_TYPES AND score >=
    FLAG_THRESHOLD. Both gates are required — composite score alone
    can be reached by stacking bonus on a single signal."""
    if len({s.signal_type for s in signals}) < MIN_FIRED_SIGNAL_TYPES:
        return False
    return compute_thrash_score(signals) >= FLAG_THRESHOLD


def extract_all_signals(
    entries: list[TranscriptEntry],
    baseline: BaselineStats | None = None,
) -> list[Signal]:
    """Run every extractor and concatenate the results. Pure / no I/O.
    Caller is responsible for filtering via ``session_eligible`` before
    calling — this function does NOT short-circuit on filter."""
    signals: list[Signal] = []
    signals.extend(detect_placeholder_emit(entries))
    signals.extend(detect_repeated_edit(entries))
    signals.extend(detect_repeated_error(entries))
    signals.extend(detect_tool_arg_churn(entries))
    signals.extend(detect_user_correction(entries))
    signals.extend(detect_session_abandoned(entries))
    signals.extend(detect_novelty_window(entries))
    signals.extend(detect_turn_cost_acceleration(entries))
    signals.extend(detect_test_regression(entries))
    signals.extend(detect_trajectory_length_zscore(entries, baseline))
    return signals


def populate_session_signals(
    conn: Any,
    session_id: str,
    entries: list[TranscriptEntry],
    baseline: BaselineStats | None = None,
) -> float | None:
    """Write per-session thrash signals + composite score to the index.

    Returns the computed thrash_score, or None when the session was
    filtered out (Opus primary, fewer than MIN_SESSION_TURNS, etc.).

    Writes:
    - ``session_signals`` — one row per fired signal_type (deletes
      stale rows for this session first).
    - ``session_rollups`` — updates every bucket row for this session
      with the same ``thrash_score`` + ``thrash_score_version`` so
      downstream queries can read the score from any rollup row.

    Filtered sessions get ``thrash_score = NULL`` written to all
    rollup rows (explicitly cleared so prior scores don't linger after
    a filter-criterion change).
    """
    conn.execute("DELETE FROM session_signals WHERE session_id = ?", (session_id,))

    if not session_eligible(entries):
        conn.execute(
            """UPDATE session_rollups
               SET thrash_score = NULL,
                   thrash_score_version = NULL
               WHERE session_id = ?""",
            (session_id,),
        )
        return None

    signals = extract_all_signals(entries, baseline)
    score = compute_thrash_score(signals)

    for sig in signals:
        conn.execute(
            """INSERT INTO session_signals
               (session_id, signal_type, count, evidence, signal_version)
               VALUES (?, ?, ?, ?, ?)""",
            (
                session_id,
                sig.signal_type,
                sig.count,
                json.dumps(sig.evidence, sort_keys=True),
                SIGNAL_VERSION,
            ),
        )

    conn.execute(
        """UPDATE session_rollups
           SET thrash_score = ?,
               thrash_score_version = ?
           WHERE session_id = ?""",
        (score, SIGNAL_VERSION, session_id),
    )
    return score
