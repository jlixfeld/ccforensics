from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ccforensics.index import ensure_schema, open_connection, reconcile_projects_dir
from ccforensics.report.aggregate import query_aggregate
from ccforensics.report.plugins import PluginRollup, query_plugins, render_plugins

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
    model: str = "claude-sonnet-4-5-20250929",
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
                    "model": model,
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


# ---------- aggregate: --group-by model & --model filter ----------


def test_aggregate_group_by_model_returns_row_per_model(tmp_path: Path, pricing_data: dict) -> None:
    """One row per distinct ``messages.model`` (NULL models are
    non-billable infrastructure rows and must not show up)."""
    proj = tmp_path / "projects"
    _make_session(proj, "s-sonnet", model="claude-sonnet-4-5-20250929")
    _make_session(proj, "s-opus", model="claude-opus-4-7")

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    rows = query_aggregate(conn, group_by="model")
    keys = {r.group_key for r in rows}
    assert "claude-sonnet-4-5-20250929" in keys
    assert "claude-opus-4-7" in keys
    # No NULL / '<none>' row leaks into the output.
    assert "<none>" not in keys
    assert None not in keys


def test_aggregate_model_filter_restricts_cost_to_matching_messages(
    tmp_path: Path, pricing_data: dict
) -> None:
    """``--model opus`` over a session that used BOTH opus and sonnet returns
    only the opus cost, not whole-session cost — this is the whole point of
    adding a message-level model filter."""
    proj = tmp_path / "projects"
    enc = proj / "-home-mixed"
    sid = "mixed"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": sid,
                "timestamp": "2026-04-22T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "cwd": "/home/mixed",
                "message": {"role": "user", "content": "hi"},
            },
            {
                "type": "assistant",
                "uuid": "u2",
                "sessionId": sid,
                "timestamp": "2026-04-22T10:00:01Z",
                "isSidechain": False,
                "isMeta": False,
                "requestId": "r-sonnet",
                "message": {
                    "id": "m-sonnet",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5-20250929",
                    "content": [{"type": "text", "text": "a"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
            {
                "type": "assistant",
                "uuid": "u3",
                "sessionId": sid,
                "timestamp": "2026-04-22T10:00:02Z",
                "isSidechain": False,
                "isMeta": False,
                "requestId": "r-opus",
                "message": {
                    "id": "m-opus",
                    "role": "assistant",
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "b"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ],
    )

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    unfiltered = query_aggregate(conn, group_by="none")
    opus_only = query_aggregate(conn, model="opus", group_by="none")

    # Both are a single '(all)' row; both > 0. Opus-only must be STRICTLY
    # less than the total (since there's at least one sonnet message).
    assert unfiltered[0].total_cost_usd > 0
    assert opus_only[0].total_cost_usd > 0
    assert opus_only[0].total_cost_usd < unfiltered[0].total_cost_usd


def test_aggregate_model_filter_combines_with_project_group(
    tmp_path: Path, pricing_data: dict
) -> None:
    """``--model X --group-by project`` gives per-project cost for model X only."""
    proj = tmp_path / "projects"
    _make_session(proj, "s-a-opus", cwd="/home/proj-a", model="claude-opus-4-7")
    _make_session(proj, "s-a-sonnet", cwd="/home/proj-a", model="claude-sonnet-4-5-20250929")
    _make_session(proj, "s-b-opus", cwd="/home/proj-b", model="claude-opus-4-7")

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    rows = query_aggregate(conn, model="opus", group_by="project")
    by_proj = {r.group_key: r for r in rows}
    # Both projects had opus activity → both appear.
    assert "/home/proj-a" in by_proj
    assert "/home/proj-b" in by_proj
    # Sonnet-only projects must not appear; the session_count for proj-a
    # counts ONLY the opus session, not the sonnet one.
    assert by_proj["/home/proj-a"].session_count == 1
    assert by_proj["/home/proj-b"].session_count == 1


def test_aggregate_model_filter_is_case_insensitive_substring(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Mirrors ``--project`` semantics — ``opus`` matches ``claude-opus-4-7``."""
    proj = tmp_path / "projects"
    _make_session(proj, "s1", model="claude-opus-4-7")

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    lower = query_aggregate(conn, model="opus", group_by="none")
    upper = query_aggregate(conn, model="OPUS", group_by="none")
    assert lower[0].total_cost_usd > 0
    assert upper[0].total_cost_usd == lower[0].total_cost_usd


def test_aggregate_model_filter_rejects_plugin_group(tmp_path: Path, pricing_data: dict) -> None:
    """Combining ``--model`` with ``--group-by plugin`` is out of scope for v1:
    plugin bucketing routes through ``session_rollups``, which has no model
    dimension. We reject the combo loudly rather than silently returning
    whole-session cost that ignores the model filter.
    """
    proj = tmp_path / "projects"
    _make_session(proj, "s1", model="claude-opus-4-7")

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    with pytest.raises(ValueError, match=r"model.*plugin"):
        query_aggregate(conn, model="opus", group_by="plugin")


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


def test_render_plugins_preserves_epoch_zero_seen() -> None:
    """first_seen=0 / last_seen=0 must render as 1970-01-01, not '-'.

    Guards against a falsy-check regression (``if r.first_seen``) that would
    otherwise misreport a session legitimately anchored at epoch 0.
    """
    from io import StringIO

    from rich.console import Console

    rollup = PluginRollup(
        plugin="main",
        total_cost_usd=0.0,
        session_count=1,
        most_used_agent_type=None,
        agent_type_count=0,
        most_used_skill=None,
        skill_count=0,
        first_seen=0,
        last_seen=0,
    )
    buf = StringIO()
    Console(file=buf, width=120, force_terminal=False).print(render_plugins([rollup]))
    out = buf.getvalue()
    # Exactly two date cells: first_seen + last_seen both render as 1970-01-01.
    assert out.count("1970-01-01") == 2
