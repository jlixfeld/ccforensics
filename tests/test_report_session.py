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


def test_session_report_excludes_synthetic_model_placeholder(
    tmp_path: Path, pricing_data: dict
) -> None:
    """``<synthetic>`` is Claude Code's literal model string for non-LLM-call
    placeholder assistant entries. It must not appear in ``models_seen``
    on the header OR as a row in the per-model rollup — those surfaces are
    for real models only.
    """
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-synth-mix"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test"),
            _assistant(
                "u2",
                sid,
                "2026-04-22T10:00:01Z",
                msg_id="m-real",
                req_id="r-real",
                model="claude-opus-4-7",
            ),
            _assistant(
                "u3",
                sid,
                "2026-04-22T10:00:02Z",
                msg_id="m-synth",
                req_id="r-synth",
                model="<synthetic>",
            ),
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    report = build_session_report(conn, sid)
    assert "<synthetic>" not in report.header.models_seen
    assert "<synthetic>" not in {m.model for m in report.models}


def test_session_report_includes_per_model_rollup(tmp_path: Path, pricing_data: dict) -> None:
    """The report surfaces per-``messages.model`` cost + tokens, so a user
    looking at ``session show`` can see what each model actually cost in that
    session. Infrastructure rows (model IS NULL) must not leak into the list.
    """
    proj = tmp_path / "projects"
    enc = proj / "-home-mixed"
    sid = "sess-mixed-models"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/mixed"),
            _assistant(
                "u2",
                sid,
                "2026-04-22T10:00:05Z",
                msg_id="m-sonnet",
                req_id="r-sonnet",
                model="claude-sonnet-4-5-20250929",
            ),
            _assistant(
                "u3",
                sid,
                "2026-04-22T10:00:10Z",
                msg_id="m-opus",
                req_id="r-opus",
                model="claude-opus-4-7",
            ),
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    report = build_session_report(conn, sid)
    models = {m.model: m for m in report.models}
    assert "claude-sonnet-4-5-20250929" in models
    assert "claude-opus-4-7" in models
    # Cost per model is populated and > 0 for both.
    assert models["claude-sonnet-4-5-20250929"].cost_usd > 0
    assert models["claude-opus-4-7"].cost_usd > 0
    # Rollup totals across models match the raw messages-level sum.
    total = sum(m.cost_usd for m in report.models)
    header_cost = report.header.total_cost_usd or 0.0
    assert abs(total - header_cost) < 1e-6


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
        compaction_count=0,
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


def test_session_header_reports_compaction_count(tmp_path: Path, pricing_data: dict) -> None:
    """``compaction_count`` on the header is the number of
    ``agent-acompact-*.jsonl`` files indexed under the session."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-cmp"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test"),
            _assistant("u2", sid, "2026-04-22T10:00:01Z", msg_id="m1", req_id="r1"),
        ],
    )
    sub_dir = enc / sid / "subagents"
    sub_dir.mkdir(parents=True)
    # Two compaction files. Each agent-acompact-<hex>.jsonl is one event.
    for hex_id in ("aa11", "bb22"):
        _write_jsonl(
            sub_dir / f"agent-acompact-{hex_id}.jsonl",
            [
                _assistant(
                    f"ac-{hex_id}",
                    sid,
                    "2026-04-22T10:00:05Z",
                    msg_id=f"acm-{hex_id}",
                    req_id=f"acr-{hex_id}",
                    isSidechain=True,
                ),
            ],
        )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    report = build_session_report(conn, sid)
    assert report.header.compaction_count == 2


def test_render_session_report_shows_compactions_in_header(
    tmp_path: Path, pricing_data: dict
) -> None:
    """``compactions: N`` appears on the header line so the user sees the
    figure without needing to drill into the bucket table."""
    from io import StringIO

    from rich.console import Console

    conn, sid = _reconcile(tmp_path, pricing_data)
    report = build_session_report(conn, sid)
    buf = StringIO()
    Console(file=buf, width=200, force_terminal=False).print(render_session_report(report))
    out = buf.getvalue()
    assert "compactions:" in out


def test_render_buckets_appends_totals_row(tmp_path: Path, pricing_data: dict) -> None:
    """The Cost-by-bucket table ends with a bold ``Totals`` row whose dollar
    figure equals the sum of the bucket cells above it."""
    from io import StringIO

    from rich.console import Console

    conn, sid = _reconcile(tmp_path, pricing_data)
    report = build_session_report(conn, sid)
    buf = StringIO()
    Console(file=buf, width=200, force_terminal=False).print(render_session_report(report))
    out = buf.getvalue()
    assert "Totals" in out
    # The totals dollar amount must equal the bucket sum.
    bucket_sum = sum(b.cost_usd for b in report.buckets)
    assert f"${bucket_sum:.2f}" in out


def test_render_session_report_shows_full_summary_in_dedicated_panel(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Summary text renders in its own panel, full-length, not truncated and
    not inlined into the Session header. Earlier the header inlined the
    summary at ``[:120]`` which routinely cut off mid-sentence."""
    from io import StringIO

    from rich.console import Console

    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-long-summary"
    long_text = (
        "Long summary that comfortably exceeds the prior 120-char cap so the "
        "test exercises full-length rendering rather than inline truncation."
    )
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", long_text, cwd="/home/test"),
            _assistant("u2", sid, "2026-04-22T10:00:01Z", msg_id="m1", req_id="r1"),
        ],
    )
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)
    report = build_session_report(conn, sid)

    buf = StringIO()
    Console(file=buf, width=200, force_terminal=False).print(render_session_report(report))
    out = buf.getvalue()
    # The Summary panel exists as a top-level section.
    assert "Summary" in out
    # The full long summary text appears verbatim — not truncated mid-string.
    assert long_text in out
    # The Session header no longer inlines a ``summary:`` field.
    assert "summary:" not in out


def test_render_session_report_suppresses_summary_panel_when_source_is_none(
    tmp_path: Path, pricing_data: dict
) -> None:
    """When summary extraction yields ``source='none'`` the report skips the
    Summary panel entirely rather than showing a panel with the literal
    ``<no summary available>`` placeholder."""
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
        compaction_count=0,
        total_cost_usd=0.0,
        models_seen=[],
        summary_text="<no summary available>",
        summary_source="none",
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
    # No Summary section header — only the Session panel mentions a session
    # title. The placeholder string itself must not leak through either.
    assert "<no summary available>" not in out
    assert "Summary" not in out


def _assistant_with_usage(
    uuid: str,
    sid: str,
    ts: str,
    *,
    msg_id: str,
    req_id: str,
    model: str = "claude-sonnet-4-5-20250929",
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_creation: int = 0,
    cache_read: int = 0,
    service_tier: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Assistant entry with explicit usage / service_tier knobs.

    Adding via a separate helper rather than expanding ``_assistant`` keeps
    the broad existing test surface untouched. Knobs map directly onto the
    ``message.usage`` block written to the JSONL.
    """
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }
    if service_tier is not None:
        usage["service_tier"] = service_tier
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
            "content": [{"type": "text", "text": "ok"}],
            "usage": usage,
        },
        **extra,
    }


def test_session_report_includes_cache_summary(tmp_path: Path, pricing_data: dict) -> None:
    """A session with cache_read + cache_creation activity surfaces token
    totals AND cost-derived efficiency / savings on the report."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-cache"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test"),
            _assistant_with_usage(
                "u2",
                sid,
                "2026-04-22T10:00:05Z",
                msg_id="m1",
                req_id="r1",
                input_tokens=1000,
                output_tokens=50,
                cache_creation=2000,
                cache_read=8000,
                service_tier="standard",
            ),
        ],
    )
    db = tmp_path / "i.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    report = build_session_report(conn, sid, pricing_data=pricing_data)

    assert report.cache is not None
    assert report.cache.cache_read_tokens == 8000
    assert report.cache.cache_creation_tokens == 2000
    assert report.cache.cache_eff_pct > 0
    assert report.cache.cache_savings_usd > 0


def test_session_report_no_cache_activity(tmp_path: Path, pricing_data: dict) -> None:
    """Zero cache_read AND zero cache_creation → cache fields all zero, no
    division-by-zero in efficiency calc."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-nocache"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test"),
            _assistant_with_usage(
                "u2",
                sid,
                "2026-04-22T10:00:05Z",
                msg_id="m1",
                req_id="r1",
                input_tokens=1000,
                output_tokens=50,
                cache_creation=0,
                cache_read=0,
            ),
        ],
    )
    db = tmp_path / "i.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    report = build_session_report(conn, sid, pricing_data=pricing_data)

    assert report.cache is not None
    assert report.cache.cache_read_tokens == 0
    assert report.cache.cache_creation_tokens == 0
    assert report.cache.cache_eff_pct == 0.0
    assert report.cache.cache_savings_usd == 0.0


def test_session_report_service_tier_breakdown_present(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Mixed standard + priority should both appear; user role excluded —
    only assistant messages carry a service_tier and inflating with user
    rows would mislead."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-tiers"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test"),
            _assistant_with_usage(
                "u2",
                sid,
                "2026-04-22T10:00:05Z",
                msg_id="m1",
                req_id="r1",
                service_tier="standard",
            ),
            _assistant_with_usage(
                "u3",
                sid,
                "2026-04-22T10:00:10Z",
                msg_id="m2",
                req_id="r2",
                service_tier="priority",
            ),
        ],
    )
    db = tmp_path / "i.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    report = build_session_report(conn, sid, pricing_data=pricing_data)

    assert report.service_tier_breakdown == {"standard": 1, "priority": 1}


def test_render_session_report_includes_cache_text(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Rich-rendered output contains the ``Cache:`` footer line when there
    is cache activity in the session — the visible payoff of the metric."""
    from io import StringIO

    from rich.console import Console

    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-cache-render"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test"),
            _assistant_with_usage(
                "u2",
                sid,
                "2026-04-22T10:00:05Z",
                msg_id="m1",
                req_id="r1",
                input_tokens=1000,
                output_tokens=50,
                cache_creation=2000,
                cache_read=8000,
            ),
        ],
    )
    db = tmp_path / "i.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    report = build_session_report(conn, sid, pricing_data=pricing_data)

    buf = StringIO()
    Console(file=buf, force_terminal=False, width=200, color_system=None).print(
        render_session_report(report)
    )
    out = buf.getvalue()
    assert "Cache:" in out
    assert "efficiency" in out
    assert "saved $" in out


def test_render_session_report_omits_cache_line_when_no_activity(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Sessions with no cache reads/creations must not render the Cache:
    footer — a 0% efficiency line on a session that didn't use caching is
    misleading noise."""
    from io import StringIO

    from rich.console import Console

    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-no-cache-render"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test"),
            _assistant_with_usage(
                "u2",
                sid,
                "2026-04-22T10:00:05Z",
                msg_id="m1",
                req_id="r1",
                cache_creation=0,
                cache_read=0,
            ),
        ],
    )
    db = tmp_path / "i.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    report = build_session_report(conn, sid, pricing_data=pricing_data)

    buf = StringIO()
    Console(file=buf, force_terminal=False, width=200, color_system=None).print(
        render_session_report(report)
    )
    out = buf.getvalue()
    assert "Cache:" not in out


def test_render_session_report_shows_service_tier_line_for_non_standard(
    tmp_path: Path, pricing_data: dict
) -> None:
    """When a session has a non-standard tier (e.g. priority), the
    ``Service tiers:`` footer must appear so the user sees that the
    standard pricing model doesn't fully apply."""
    from io import StringIO

    from rich.console import Console

    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-priority"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test"),
            _assistant_with_usage(
                "u2",
                sid,
                "2026-04-22T10:00:05Z",
                msg_id="m1",
                req_id="r1",
                service_tier="priority",
            ),
        ],
    )
    db = tmp_path / "i.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    report = build_session_report(conn, sid, pricing_data=pricing_data)

    buf = StringIO()
    Console(file=buf, force_terminal=False, width=200, color_system=None).print(
        render_session_report(report)
    )
    out = buf.getvalue()
    assert "Service tiers:" in out
    assert "priority" in out


def test_render_session_report_omits_service_tier_line_for_standard_only(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Sessions with only standard/unknown tiers must not render the
    Service tiers: footer — that's the boring case and showing it on every
    session would be visual noise."""
    from io import StringIO

    from rich.console import Console

    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-standard"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test"),
            _assistant_with_usage(
                "u2",
                sid,
                "2026-04-22T10:00:05Z",
                msg_id="m1",
                req_id="r1",
                service_tier="standard",
            ),
        ],
    )
    db = tmp_path / "i.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    report = build_session_report(conn, sid, pricing_data=pricing_data)

    buf = StringIO()
    Console(file=buf, force_terminal=False, width=200, color_system=None).print(
        render_session_report(report)
    )
    out = buf.getvalue()
    assert "Service tiers:" not in out


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
