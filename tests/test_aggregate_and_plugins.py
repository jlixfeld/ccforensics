from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ccforensics.index import ensure_schema, open_connection, reconcile_projects_dir
from ccforensics.report.aggregate import query_aggregate
from ccforensics.report.plugins import query_plugins

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict[str, Any]:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e))
            f.write("\n")


def _make_session(
    proj: Path,
    sid: str,
    *,
    cwd: str = "/home/test",
    ts: str = "2026-04-22T10:00:00Z",
    subagent_type: str | None = None,
) -> None:
    """Build a minimal main-only or main+subagent session."""
    enc = proj / "-home-test"
    main_content: list[dict[str, Any]] = [{"type": "text", "text": "ok"}]
    if subagent_type:
        main_content = [
            {
                "type": "tool_use",
                "id": f"tu-{sid}",
                "name": "Agent",
                "input": {"subagent_type": subagent_type},
            }
        ]
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            {
                "type": "user",
                "uuid": f"u-{sid}-1",
                "sessionId": sid,
                "timestamp": ts,
                "isSidechain": False,
                "isMeta": False,
                "cwd": cwd,
                "message": {"role": "user", "content": "hi"},
            },
            {
                "type": "assistant",
                "uuid": f"u-{sid}-2",
                "sessionId": sid,
                "timestamp": ts,
                "isSidechain": False,
                "isMeta": False,
                "requestId": f"r-{sid}",
                "message": {
                    "id": f"m-{sid}",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5-20250929",
                    "content": main_content,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ],
    )
    if subagent_type:
        sub_dir = enc / sid / "subagents"
        sub_dir.mkdir(parents=True)
        # agent-<hex>.jsonl — the regex requires pure hex in the id slot.
        child_id = "".join(f"{ord(c):02x}" for c in sid)
        _write_jsonl(
            sub_dir / f"agent-{child_id}.jsonl",
            [
                {
                    "type": "assistant",
                    "uuid": f"c-{sid}-1",
                    "sessionId": sid,
                    "agentId": child_id,
                    "timestamp": ts,
                    "isSidechain": True,
                    "isMeta": False,
                    "requestId": f"cr-{sid}",
                    "message": {
                        "id": f"cm-{sid}",
                        "role": "assistant",
                        "model": "claude-sonnet-4-5-20250929",
                        "content": [{"type": "text", "text": "done"}],
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                },
            ],
        )
        (sub_dir / f"agent-{child_id}.meta.json").write_text(
            json.dumps({"agentType": subagent_type, "description": "x"})
        )


# ---------- aggregate ----------


def test_aggregate_group_none_sums_all(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    _make_session(proj, "s1")
    _make_session(proj, "s2")

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    rows = query_aggregate(conn, group_by="none")
    assert len(rows) == 1
    assert rows[0].group_key == "(all)"
    assert rows[0].session_count == 2
    assert rows[0].total_cost_usd > 0


def test_aggregate_group_by_project(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    _make_session(proj, "s1", cwd="/home/proj-a")
    _make_session(proj, "s2", cwd="/home/proj-a")
    _make_session(proj, "s3", cwd="/home/proj-b")

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    rows = query_aggregate(conn, group_by="project")
    keys = {r.group_key: r.session_count for r in rows}
    assert keys.get("/home/proj-a") == 2
    assert keys.get("/home/proj-b") == 1


def test_aggregate_since_until_window(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    _make_session(proj, "s-old", ts="2026-03-01T10:00:00Z")
    _make_session(proj, "s-new", ts="2026-04-22T10:00:00Z")

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    since = datetime(2026, 4, 1, tzinfo=UTC)
    rows = query_aggregate(conn, since=since, group_by="none")
    assert rows[0].session_count == 1


def test_aggregate_group_by_plugin_classifies(tmp_path: Path, pricing_data: dict) -> None:
    """Subagent types resolve to their plugin (or 'unknown' when not
    in the plugins table)."""
    proj = tmp_path / "projects"
    _make_session(proj, "s1", subagent_type="pr-review-toolkit:code-reviewer")

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)
    # Clear the real plugins registry so the classification is
    # deterministic (matches the similar pattern in test_report_session).
    conn.execute("DELETE FROM plugins")
    conn.execute(
        "INSERT INTO plugins (name, install_path, scope) VALUES (?,?,?)",
        ("pr-review-toolkit", "/fake", "user"),
    )
    conn.commit()

    rows = query_aggregate(conn, group_by="plugin")
    keys = {r.group_key for r in rows}
    assert "pr-review-toolkit" in keys
    assert "main" in keys


def test_aggregate_group_by_day_uses_last_active(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    _make_session(proj, "a", ts="2026-04-22T10:00:00Z")
    _make_session(proj, "b", ts="2026-04-22T18:00:00Z")
    _make_session(proj, "c", ts="2026-04-23T09:00:00Z")

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    rows = query_aggregate(conn, group_by="day")
    keys = {r.group_key: r.session_count for r in rows}
    assert keys.get("2026-04-22") == 2
    assert keys.get("2026-04-23") == 1


# ---------- plugins ----------


def test_plugins_reports_main_and_subagent(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    _make_session(proj, "s1", subagent_type="Explore")

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    rows = query_plugins(conn)
    by_plugin = {r.plugin: r for r in rows}
    assert "main" in by_plugin
    assert "builtin" in by_plugin  # 'Explore' is a builtin
    assert by_plugin["builtin"].most_used_agent_type == "Explore"


def test_plugins_since_until_filters(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    _make_session(proj, "old", ts="2026-03-01T10:00:00Z")
    _make_session(proj, "new", ts="2026-04-22T10:00:00Z")

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    since = datetime(2026, 4, 1, tzinfo=UTC)
    rows = query_plugins(conn, since=since)
    # 'main' should appear but with session_count == 1 (the newer session).
    main_rollup = next((r for r in rows if r.plugin == "main"), None)
    assert main_rollup is not None
    assert main_rollup.session_count == 1
