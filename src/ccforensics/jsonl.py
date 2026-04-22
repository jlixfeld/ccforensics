from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import KNOWN_TYPES, TranscriptEntry, parse_entry


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
