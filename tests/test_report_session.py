from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ccforensics.index import ensure_schema, open_connection, reconcile_projects_dir
from ccforensics.report.session import (
    SessionReportNotFound,
    build_session_report,
    render_session_report,
)

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


def _user(uuid: str, sid: str, ts: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": sid,
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
        "message": {"role": "user", "content": text},
        **extra,
    }


def _assistant(
    uuid: str,
    sid: str,
    ts: str,
    *,
    msg_id: str,
    req_id: str,
    model: str = "claude-sonnet-4-5-20250929",
    content: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": sid,
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
        "requestId": req_id,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "content": content or [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
        **extra,
    }


def _reconcile(tmp_path: Path, pricing: dict) -> tuple[Any, str]:
    """Build a realistic session with main + subagent + meta.json."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-report-1"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test/myproj"),
            _assistant(
                "u2",
                sid,
                "2026-04-22T10:00:10Z",
                msg_id="m1",
                req_id="r1",
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "Agent",
                        "input": {"subagent_type": "pr-review-toolkit:code-reviewer"},
                    }
                ],
            ),
        ],
    )
    sub_dir = enc / sid / "subagents"
    sub_dir.mkdir(parents=True)
    _write_jsonl(
        sub_dir / "agent-abc.jsonl",
        [
            {
                "type": "user",
                "uuid": "c-u1",
                "sessionId": sid,
                "agentId": "abc",
                "timestamp": "2026-04-22T10:00:15Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "review"},
            },
            _assistant(
                "c-u2",
                sid,
                "2026-04-22T10:00:20Z",
                msg_id="m2",
                req_id="r2",
                agentId="abc",
                isSidechain=True,
            ),
        ],
    )
    (sub_dir / "agent-abc.meta.json").write_text(
        '{"agentType":"pr-review-toolkit:code-reviewer","description":"review"}'
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing)
    return conn, sid


def test_build_session_report_has_header_and_buckets(tmp_path: Path, pricing_data: dict) -> None:
    conn, sid = _reconcile(tmp_path, pricing_data)
    report = build_session_report(conn, sid)

    assert report.header.session_id == sid
    assert report.header.project_path == "/home/test/myproj"
    assert report.header.turn_count >= 1
    assert "claude-sonnet-4-5-20250929" in report.header.models_seen

    bucket_kinds = {(b.bucket_kind, b.bucket_name) for b in report.buckets}
    assert ("main", "main") in bucket_kinds
    assert ("subagent", "pr-review-toolkit:code-reviewer") in bucket_kinds


def test_plugin_rollup_resolves_known_plugin(tmp_path: Path, pricing_data: dict) -> None:
    conn, sid = _reconcile(tmp_path, pricing_data)
    # Seed the plugins table so the pr-review-toolkit namespace resolves.
    conn.execute(
        "INSERT OR REPLACE INTO plugins (name, version, install_path, scope) VALUES (?,?,?,?)",
        ("pr-review-toolkit", "unknown", "/fake/plugins/pr-review-toolkit", "user"),
    )
    conn.commit()

    report = build_session_report(conn, sid)
    sources = {p.source for p in report.plugins}
    assert "pr-review-toolkit" in sources
    assert "main" in sources


def test_plugin_rollup_unknown_prefix_is_marked_unknown(tmp_path: Path, pricing_data: dict) -> None:
    """Subagent_type with an unfamiliar prefix → 'unknown'.

    Reconcile auto-populates the plugins table from ``~/.claude/plugins``,
    so we clear that table and rebuild the report to test the
    unknown-classification path deterministically, independent of the
    developer's local plugin install.
    """
    conn, sid = _reconcile(tmp_path, pricing_data)
    conn.execute("DELETE FROM plugins")
    conn.commit()

    report = build_session_report(conn, sid)
    sources = {p.source for p in report.plugins}
    assert "unknown" in sources


def test_include_unattributed_populates_items(tmp_path: Path, pricing_data: dict) -> None:
    """Unresolvable subagent → listed when --include-unattributed."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-ua"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [_user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test")],
    )
    sub_dir = enc / sid / "subagents"
    sub_dir.mkdir(parents=True)
    _write_jsonl(
        sub_dir / "agent-abc.jsonl",
        [
            {
                "type": "user",
                "uuid": "c-u1",
                "sessionId": sid,
                "agentId": "abc",
                "timestamp": "2026-04-22T10:00:05Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "x"},
            },
            _assistant(
                "c-u2",
                sid,
                "2026-04-22T10:00:10Z",
                msg_id="m2",
                req_id="r2",
                agentId="abc",
                isSidechain=True,
            ),
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    without = build_session_report(conn, sid, include_unattributed=False)
    assert without.unattributed_items == []

    with_ = build_session_report(conn, sid, include_unattributed=True)
    assert len(with_.unattributed_items) == 1
    assert with_.unattributed_items[0].child_file_path.endswith("agent-abc.jsonl")


def test_parse_notes_populated(tmp_path: Path, pricing_data: dict) -> None:
    conn, sid = _reconcile(tmp_path, pricing_data)
    report = build_session_report(conn, sid)
    assert report.parse_notes is not None
    assert report.parse_notes.files_count >= 2  # main + subagent


def test_build_report_missing_session_raises(tmp_path: Path, pricing_data: dict) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    with pytest.raises(SessionReportNotFound):
        build_session_report(conn, "does-not-exist")


def test_render_session_report_produces_output(tmp_path: Path, pricing_data: dict) -> None:
    from io import StringIO

    from rich.console import Console

    conn, sid = _reconcile(tmp_path, pricing_data)
    report = build_session_report(conn, sid, include_unattributed=True)

    buf = StringIO()
    Console(file=buf, width=120, force_terminal=False).print(render_session_report(report))
    out = buf.getvalue()
    # Header hits
    assert sid in out
    assert "project:" in out
    assert "models:" in out
    # Tables present
    assert "Cost by bucket" in out
    assert "Cost by plugin" in out


def test_report_cost_totals_match_session_total(tmp_path: Path, pricing_data: dict) -> None:
    """Sum over rendered buckets == session total (the invariant, surfaced
    through the report)."""
    conn, sid = _reconcile(tmp_path, pricing_data)
    report = build_session_report(conn, sid)
    bucket_total = sum(b.cost_usd for b in report.buckets)
    assert report.header.total_cost_usd is not None
    assert abs(bucket_total - report.header.total_cost_usd) < 1e-6


def test_render_handles_zero_cost_and_empty_tables() -> None:
    """A session with total_cost_usd=0.0 and no buckets/plugins must still
    render — no divide-by-zero, no broken tables, no crash. Covers the
    degenerate case of an empty/user-only session before any billable
    activity lands."""
    from io import StringIO

    from rich.console import Console

    from ccforensics.report.session import (
        ParseNotes,
        SessionHeader,
        SessionReport,
        render_session_report,
    )

    header = SessionHeader(
        session_id="empty-sid",
        project_path="/home/test/proj",
        started_at=1_713_600_000,
        last_active_at=1_713_600_030,
        duration_s=30,
        turn_count=0,
        total_cost_usd=0.0,
        models_seen=[],
        summary_text=None,
        summary_source=None,
    )
    report = SessionReport(
        header=header,
        buckets=[],
        plugins=[],
        unattributed_items=[],
        parse_notes=ParseNotes(schema_versions=[], parse_warnings_total=0, files_count=0),
    )
    buf = StringIO()
    Console(file=buf, width=120, force_terminal=False).print(render_session_report(report))
    out = buf.getvalue()
    assert "empty-sid" in out
    assert "$0.00" in out
    assert "Cost by bucket" in out
    assert "Cost by plugin" in out


def test_skill_ledger_zero_cost_renders_dollar_zero() -> None:
    """estimated_cost_usd=0.0 must render as $0.00, not '-'.

    Guards against a falsy-check regression (``if entry.estimated_cost_usd``)
    that would otherwise suppress legitimate zero-cost activations.
    """
    from io import StringIO

    from rich.console import Console

    from ccforensics.report.session import SkillLedgerEntry, _render_skill_ledger

    entry = SkillLedgerEntry(
        activated_at=1_713_600_000,
        skill_name="my-skill",
        plugin_name=None,
        source="skill-tool",
        content_size=100,
        skill_path="/tmp/skills/my-skill/SKILL.md",
        estimated_cost_usd=0.0,
        estimated_cost_band_usd=None,
    )
    buf = StringIO()
    Console(file=buf, width=120, force_terminal=False).print(_render_skill_ledger([entry]))
    out = buf.getvalue()
    assert "$0.00" in out
