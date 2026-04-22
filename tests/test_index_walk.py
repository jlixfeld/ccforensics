from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccforensics.index import (
    ensure_schema,
    open_connection,
    reconcile_projects_dir,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def test_walk_indexes_all_jsonl(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    sess_a = proj / "-home-test"
    sess_a.mkdir(parents=True)
    (sess_a / "sess-a.jsonl").write_bytes((FIXTURES / "basic" / "s1.jsonl").read_bytes())

    sess_b = proj / "-home-test-other"
    sess_b.mkdir(parents=True)
    (sess_b / "sess-b.jsonl").write_bytes((FIXTURES / "drift" / "session.jsonl").read_bytes())
    sub = sess_b / "sess-b" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-deadbeef.jsonl").write_bytes((FIXTURES / "basic" / "s1.jsonl").read_bytes())

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    stats = reconcile_projects_dir(conn, proj, pricing_data)
    conn.commit()

    assert stats.files_indexed == 3
    assert stats.files_changed == 3
    file_rows = conn.execute("SELECT kind FROM files").fetchall()
    kinds = {row[0] for row in file_rows}
    assert kinds == {"main", "subagent"}


def test_walk_commits_per_file(tmp_path: Path, pricing_data: dict) -> None:
    """If the walker is interrupted mid-loop, already-processed files
    must be persisted (per-file commit, not commit-at-end)."""
    proj = tmp_path / "projects"
    sess = proj / "-home-test"
    sess.mkdir(parents=True)
    for i in range(3):
        (sess / f"sess-{i}.jsonl").write_bytes((FIXTURES / "basic" / "s1.jsonl").read_bytes())

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)

    # Wrap conn.execute on the third reconcile_file insert to raise, simulating
    # an interrupt. Then verify the first two files made it to disk by opening
    # a fresh connection and counting.
    real_reconcile = __import__("ccforensics.index", fromlist=["reconcile_file"]).reconcile_file
    call_count = {"n": 0}

    def boom(c, p, pd):
        call_count["n"] += 1
        if call_count["n"] >= 3:
            raise KeyboardInterrupt("simulated interrupt")
        return real_reconcile(c, p, pd)

    import ccforensics.index as idx_mod

    monkey_target = idx_mod.reconcile_file
    idx_mod.reconcile_file = boom  # type: ignore[assignment]
    try:
        with pytest.raises(KeyboardInterrupt):
            reconcile_projects_dir(conn, proj, pricing_data)
    finally:
        idx_mod.reconcile_file = monkey_target  # type: ignore[assignment]

    # Open a fresh connection — if commits are per-file, the first two
    # are durable even though the third interrupted before commit.
    conn.close()
    fresh = open_connection(db)
    durable = fresh.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert durable == 2, f"expected 2 files persisted before interrupt, got {durable}"
