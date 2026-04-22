"""Opt-in smoke test: ingest every real JSONL under ~/.claude/projects/.

Run with: ``CCFORENSICS_REAL=1 uv run pytest tests/test_real_ingest.py -v``
Skipped by default in CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ccforensics.jsonl import dedup_entries, parse_file
from ccforensics.models import TranscriptEntry
from ccforensics.paths import claude_projects_dir

pytestmark = pytest.mark.skipif(
    os.environ.get("CCFORENSICS_REAL") != "1",
    reason="real-corpus smoke test; set CCFORENSICS_REAL=1 to run",
)


def _all_jsonl_files() -> list[Path]:
    root = claude_projects_dir()
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"))


def test_parses_all_real_jsonl_without_crashing() -> None:
    files = _all_jsonl_files()
    assert len(files) > 0, "no JSONL files to test against"
    total_entries = 0
    total_warnings = 0
    for f in files:
        result = parse_file(f)
        total_entries += len(result.entries)
        total_warnings += len(result.warnings)
    assert total_entries > 0


def test_dedup_is_idempotent_across_real_corpus() -> None:
    all_entries: list[TranscriptEntry] = []
    for f in _all_jsonl_files():
        all_entries.extend(parse_file(f).entries)
    first = dedup_entries(all_entries)
    second = dedup_entries(first)
    assert len(first) == len(second)
