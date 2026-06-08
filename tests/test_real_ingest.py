"""Opt-in smoke test: ingest every real JSONL under ~/.claude/projects/.

Run with: ``CCFORENSICS_REAL=1 uv run pytest tests/test_real_ingest.py -v``
Skipped by default in CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ccforensics.attribution import find_invariant_violators
from ccforensics.cli import _load_pricing
from ccforensics.index import ensure_schema, open_connection, reconcile_projects_dir
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


def test_real_corpus_workflow_attribution(tmp_path: Path) -> None:
    """Reconcile the real corpus and verify dynamic-workflow attribution:
    no phantom ``agent-<hex>``/``journal`` sessions, workflow agent files
    classified as ``subagent`` (never ``main``), ``journal.jsonl`` skipped,
    and the per-session cost invariant holds. Conditional on the corpus
    actually containing workflow artifacts."""
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, claude_projects_dir(), _load_pricing())

    # Invariant must hold across every real session.
    assert find_invariant_violators(conn, tolerance=1e-6) == []

    workflow_files = conn.execute(
        "SELECT COUNT(*) FROM files WHERE path LIKE '%/subagents/workflows/%'"
    ).fetchone()[0]
    if workflow_files == 0:
        pytest.skip("no dynamic-workflow artifacts in this corpus")

    # No phantom sessions from the pre-v6 misclassification.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM files WHERE session_id LIKE 'agent-%' OR session_id = 'journal'"
        ).fetchone()[0]
        == 0
    )
    # Workflow agent files classify as subagent, never main.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM files WHERE path LIKE '%/subagents/workflows/%' AND kind = 'main'"
        ).fetchone()[0]
        == 0
    )
    # journal.jsonl is never indexed.
    assert (
        conn.execute("SELECT COUNT(*) FROM files WHERE path LIKE '%journal.jsonl'").fetchone()[0]
        == 0
    )
    # At least one first-class workflow:<name> bucket resolved.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM session_rollups WHERE bucket_kind = 'workflow'"
        ).fetchone()[0]
        > 0
    )
