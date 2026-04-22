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
