from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from ccforensics.cli import main


def test_help_prints_command_tree() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "ccforensics" in result.output.lower()
    assert "session" in result.output
    assert "aggregate" in result.output
    assert "plugins" in result.output
    assert "tools" in result.output
    assert "thrash" in result.output
    assert "index" in result.output


def test_thrash_help_documents_caveats() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["thrash", "--help"])
    assert result.exit_code == 0
    assert "thrash" in result.output.lower()
    assert "Caveats" in result.output
    assert "--evidence" in result.output
    assert "--session" in result.output
    assert "--min-signals" in result.output


def test_thrash_no_refresh_runs_against_empty_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh index w/ no sessions should print "No flagged sessions"
    rather than crash."""
    from ccforensics import paths as paths_mod

    monkeypatch.setattr(paths_mod, "ccforensics_cache_dir", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["thrash", "--no-refresh"])
    assert result.exit_code == 0, result.output
    assert "No flagged sessions in scope" in result.output


def test_thrash_json_emits_valid_json_against_empty_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ccforensics import paths as paths_mod

    monkeypatch.setattr(paths_mod, "ccforensics_cache_dir", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["thrash", "--no-refresh", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["headline"]["n_flagged"] == 0
    assert payload["rows"] == []


def test_thrash_csv_emits_header_against_empty_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ccforensics import paths as paths_mod

    monkeypatch.setattr(paths_mod, "ccforensics_cache_dir", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["thrash", "--no-refresh", "--csv"])
    assert result.exit_code == 0, result.output
    reader = csv.reader(io.StringIO(result.output))
    header = next(reader)
    assert "session_id" in header
    assert "thrash_score" in header


def test_thrash_json_csv_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ccforensics import paths as paths_mod

    monkeypatch.setattr(paths_mod, "ccforensics_cache_dir", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["thrash", "--no-refresh", "--json", "--csv"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_verbose_flag_is_accepted() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["-v", "aggregate", "--no-refresh"])
    assert result.exit_code == 0


def test_pricing_fallback_prints_banner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When PricingCache lands in the hardcoded-fallback path, the CLI
    surfaces a banner on stderr — otherwise users would see fabricated
    costs with no indication the network lookup failed."""
    from ccforensics import pricing as pricing_mod

    def _raise(*_a: object, **_k: object) -> None:
        raise RuntimeError("simulated offline")

    monkeypatch.setattr(pricing_mod.PricingCache, "_fetch_and_store", _raise)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(main, ["aggregate"])
    assert result.exit_code == 0
    assert "built-in fallback" in result.stderr


def test_pricing_stale_prints_banner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale cache (refresh failure with cache present) also surfaces a banner."""
    import json as _json

    from ccforensics import pricing as pricing_mod
    from ccforensics.paths import ccforensics_cache_dir

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    # Seed a stale cache at whatever path platformdirs resolves to under the
    # redirected envs — no guessing between Linux/macOS layouts.
    data = _json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())
    cache_dir = ccforensics_cache_dir()
    (cache_dir / "litellm.json").write_text(_json.dumps({"fetched_at": 0, "data": data}))

    def _raise(*_a: object, **_k: object) -> None:
        raise RuntimeError("simulated refresh failure")

    monkeypatch.setattr(pricing_mod.PricingCache, "_fetch_and_store", _raise)
    runner = CliRunner()
    result = runner.invoke(main, ["aggregate"])
    assert result.exit_code == 0
    assert "last cached pricing" in result.stderr


def test_main_installs_stderr_log_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() must call logging.basicConfig so module warnings reach stderr.

    Without this, every ``logger.warning(...)`` in pricing/registry/index/skills
    is silently discarded. Covers the Tier 1 audit finding that the CLI never
    configured Python logging.
    """
    import logging as lg

    root = lg.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    for h in original_handlers:
        root.removeHandler(h)
    try:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(main, ["index", "stats"])
        assert result.exit_code == 0
        assert any(isinstance(h, lg.StreamHandler) for h in root.handlers), (
            "main() should install a StreamHandler on the root logger"
        )
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in original_handlers:
            root.addHandler(h)
        root.setLevel(original_level)


def test_index_stats_on_missing_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main, ["index", "stats"])
    assert result.exit_code == 0
    assert "files: 0" in result.output


def test_index_rebuild_on_empty_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(main, ["index", "rebuild"])
    assert result.exit_code == 0
    assert "indexed 0 file(s)" in result.output


def test_index_rebuild_force_drops_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    runner = CliRunner()
    # --force requires --yes for non-interactive
    result = runner.invoke(main, ["index", "rebuild", "--force", "--yes"])
    assert result.exit_code == 0


# ---------- session list: integration ----------

FIXTURES = Path(__file__).parent / "fixtures"


def _seed_pricing_cache(tmp_path: Path) -> None:
    """Pre-populate the ccforensics cache so PricingCache.load_or_fetch never hits the network.

    Writes the litellm fixture wrapped with a fresh ``fetched_at`` to the
    platformdirs-resolved cache path. Both XDG_CACHE_HOME (Linux) and HOME
    (macOS) are already redirected to ``tmp_path`` by the caller, so this
    lands under the isolated fixture tree.
    """
    data = json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())
    for candidate in (
        tmp_path / ".cache" / "ccforensics",
        tmp_path / "Library" / "Caches" / "ccforensics",
    ):
        candidate.mkdir(parents=True, exist_ok=True)
        (candidate / "litellm.json").write_text(
            json.dumps({"fetched_at": int(time.time()), "data": data})
        )


def _write_synthetic_session(
    projects_dir: Path,
    *,
    session_id: str,
    encoded_dir: str,
    first_prompt: str,
    started_ts: str = "2026-04-20T10:00:00Z",
    second_ts: str = "2026-04-20T10:00:30Z",
) -> Path:
    """Write a minimal real-shape JSONL file and return its path."""
    enc = projects_dir / encoded_dir
    enc.mkdir(parents=True, exist_ok=True)
    path = enc / f"{session_id}.jsonl"
    entries = [
        {
            "type": "user",
            "uuid": f"{session_id}-u1",
            "sessionId": session_id,
            "timestamp": started_ts,
            "isSidechain": False,
            "isMeta": False,
            "cwd": "/home/test/proj",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": first_prompt}],
            },
        },
        {
            "type": "assistant",
            "uuid": f"{session_id}-a1",
            "sessionId": session_id,
            "timestamp": second_ts,
            "isSidechain": False,
            "isMeta": False,
            "requestId": f"{session_id}-r1",
            "message": {
                "role": "assistant",
                "id": f"{session_id}-m1",
                "model": "claude-sonnet-4-5-20250929",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e))
            f.write("\n")
    return path


def test_session_show_prefix_not_found_exits_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown prefix must exit code 2 with a helpful stderr message —
    not a crash, and not a silent zero-rows response."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_pricing_cache(tmp_path)
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "show", "--no-refresh", "nonexistent123"])
    assert result.exit_code == 2
    assert "no session matches" in result.stderr
    assert "nonexistent123" in result.stderr


def test_session_show_ambiguous_prefix_exits_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prefix matching multiple sessions must exit code 2 with both ids
    listed on stderr so the user can disambiguate."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    # Both sessions share the "ambig1" prefix.
    _write_synthetic_session(
        projects,
        session_id="ambig1-aaaa",
        encoded_dir="-home-test-proj",
        first_prompt="first",
    )
    _write_synthetic_session(
        projects,
        session_id="ambig1-bbbb",
        encoded_dir="-home-test-proj2",
        first_prompt="second",
    )
    runner = CliRunner()
    rebuild = runner.invoke(main, ["index", "rebuild", "--force", "--yes"])
    assert rebuild.exit_code == 0, rebuild.output

    result = runner.invoke(main, ["session", "show", "--no-refresh", "ambig1"])
    assert result.exit_code == 2
    assert "matches 2 sessions" in result.stderr
    assert "ambig1-aaaa" in result.stderr
    assert "ambig1-bbbb" in result.stderr


def test_session_list_no_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "list", "--no-refresh"])
    assert result.exit_code == 0


def test_session_list_json_mutually_exclusive_with_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "list", "--no-refresh", "--json", "--csv"])
    assert result.exit_code == 2  # click UsageError
    assert "mutually exclusive" in result.output.lower()


def test_session_list_json_export_real_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: real JSONL on disk, real reconcile, then --json export."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    _write_synthetic_session(
        projects,
        session_id="abc123xyz",
        encoded_dir="-home-test-proj",
        first_prompt="investigate parser bug",
    )

    runner = CliRunner()
    rebuild = runner.invoke(main, ["index", "rebuild", "--force", "--yes"])
    assert rebuild.exit_code == 0, rebuild.output

    result = runner.invoke(main, ["session", "list", "--no-refresh", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    assert row["session_id"] == "abc123xyz"
    assert row["summary_text"] == "investigate parser bug"
    assert row["summary_source"] == "first-prompt"
    assert row["project_path"] == "/home/test/proj"
    assert row["project_display"] == "proj"
    assert row["turn_count"] == 1
    assert row["total_cost_usd"] is not None and row["total_cost_usd"] > 0.0


def test_session_list_csv_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    _write_synthetic_session(
        projects,
        session_id="abc123xyz",
        encoded_dir="-home-test-proj",
        first_prompt="refactor helper module",
    )

    runner = CliRunner()
    rebuild = runner.invoke(main, ["index", "rebuild", "--force", "--yes"])
    assert rebuild.exit_code == 0, rebuild.output

    result = runner.invoke(main, ["session", "list", "--no-refresh", "--csv"])
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(result.output)))
    assert len(rows) == 1
    assert rows[0]["session_id"] == "abc123xyz"
    assert rows[0]["summary_text"] == "refactor helper module"
    assert rows[0]["project_display"] == "proj"
    # Expected headers present in declared order.
    header_line = result.output.splitlines()[0]
    assert header_line.startswith("session_id,project_path,project_display")


def test_session_list_grep_filters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    _write_synthetic_session(
        projects,
        session_id="aaaaaaaaaa",
        encoded_dir="-home-test-a",
        first_prompt="fix parser edge case",
    )
    _write_synthetic_session(
        projects,
        session_id="bbbbbbbbbb",
        encoded_dir="-home-test-b",
        first_prompt="unrelated refactor",
        started_ts="2026-04-20T11:00:00Z",
        second_ts="2026-04-20T11:00:30Z",
    )

    runner = CliRunner()
    rebuild = runner.invoke(main, ["index", "rebuild", "--force", "--yes"])
    assert rebuild.exit_code == 0, rebuild.output

    result = runner.invoke(main, ["session", "list", "--no-refresh", "--grep", "parser", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [r["session_id"] for r in payload] == ["aaaaaaaaaa"]


def test_session_list_project_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    # Both sessions use cwd=/home/test/proj (hard-coded in helper), so we
    # differentiate via project_path override by writing a second session
    # whose cwd points elsewhere.
    _write_synthetic_session(
        projects,
        session_id="aaaaaaaaaa",
        encoded_dir="-home-test-proj",
        first_prompt="session A",
    )
    # Second session: different encoded dir + different cwd.
    enc_b = projects / "-var-other-place"
    enc_b.mkdir(parents=True)
    (enc_b / "bbbbbbbbbb.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "bbbbbbbbbb-u1",
                "sessionId": "bbbbbbbbbb",
                "timestamp": "2026-04-20T11:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "cwd": "/var/other/place",
                "message": {"role": "user", "content": [{"type": "text", "text": "session B"}]},
            }
        )
        + "\n"
    )

    runner = CliRunner()
    rebuild = runner.invoke(main, ["index", "rebuild", "--force", "--yes"])
    assert rebuild.exit_code == 0, rebuild.output

    result = runner.invoke(
        main, ["session", "list", "--no-refresh", "--project", "other", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [r["session_id"] for r in payload] == ["bbbbbbbbbb"]


def test_session_list_since_until_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    _write_synthetic_session(
        projects,
        session_id="oldoldoldo",
        encoded_dir="-home-test-old",
        first_prompt="older work",
        started_ts="2026-04-10T10:00:00Z",
        second_ts="2026-04-10T10:01:00Z",
    )
    _write_synthetic_session(
        projects,
        session_id="newnewnewn",
        encoded_dir="-home-test-new",
        first_prompt="recent work",
        started_ts="2026-04-21T10:00:00Z",
        second_ts="2026-04-21T10:01:00Z",
    )

    runner = CliRunner()
    rebuild = runner.invoke(main, ["index", "rebuild", "--force", "--yes"])
    assert rebuild.exit_code == 0, rebuild.output

    # --since catches only the recent one.
    result = runner.invoke(
        main, ["session", "list", "--no-refresh", "--since", "2026-04-15", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [r["session_id"] for r in payload] == ["newnewnewn"]

    # --until catches only the older one.
    result2 = runner.invoke(
        main, ["session", "list", "--no-refresh", "--until", "2026-04-15", "--json"]
    )
    assert result2.exit_code == 0, result2.output
    payload2 = json.loads(result2.output)
    assert [r["session_id"] for r in payload2] == ["oldoldoldo"]


def test_session_list_sort_reverse_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    for i, ts in enumerate(
        ["2026-04-10T10:00:00Z", "2026-04-20T10:00:00Z", "2026-04-15T10:00:00Z"]
    ):
        _write_synthetic_session(
            projects,
            session_id=f"sess{i}xxxxx",
            encoded_dir=f"-home-test-{i}",
            first_prompt=f"prompt {i}",
            started_ts=ts,
            # second message one minute later.
            second_ts=ts.replace("T10:00:00Z", "T10:01:00Z"),
        )

    runner = CliRunner()
    rebuild = runner.invoke(main, ["index", "rebuild", "--force", "--yes"])
    assert rebuild.exit_code == 0, rebuild.output

    # Default: sort by last-active DESC → most recent first.
    result = runner.invoke(main, ["session", "list", "--no-refresh", "--sort", "started", "--json"])
    payload = json.loads(result.output)
    assert [r["session_id"] for r in payload] == ["sess1xxxxx", "sess2xxxxx", "sess0xxxxx"]

    # --reverse flips order.
    result = runner.invoke(
        main,
        ["session", "list", "--no-refresh", "--sort", "started", "--reverse", "--json"],
    )
    payload = json.loads(result.output)
    assert [r["session_id"] for r in payload] == ["sess0xxxxx", "sess2xxxxx", "sess1xxxxx"]

    # --limit caps results.
    result = runner.invoke(
        main,
        ["session", "list", "--no-refresh", "--sort", "started", "--limit", "2", "--json"],
    )
    payload = json.loads(result.output)
    assert len(payload) == 2
    assert [r["session_id"] for r in payload] == ["sess1xxxxx", "sess2xxxxx"]


def test_session_list_table_renders_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (no --json/--csv) path renders a rich table containing summary text."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # Rich reads COLUMNS when stdout isn't a real terminal; widen it so the
    # Summary column doesn't fold long text across multiple rows and break
    # substring matching below.
    monkeypatch.setenv("COLUMNS", "400")
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    _write_synthetic_session(
        projects,
        session_id="abc123xyz9",
        encoded_dir="-home-test-proj",
        first_prompt="distinct-marker-xyzzy",
    )

    runner = CliRunner()
    rebuild = runner.invoke(main, ["index", "rebuild", "--force", "--yes"])
    assert rebuild.exit_code == 0, rebuild.output

    result = runner.invoke(main, ["session", "list", "--no-refresh"])
    assert result.exit_code == 0, result.output
    assert "distinct-marker-xyzzy" in result.output
    # Short UUID prefix appears.
    assert "abc123" in result.output


def test_session_list_narrow_terminal_drops_project_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At COLUMNS=80, render_session_list switches to narrow mode and drops
    the Project column. Covers the _detect_console_width env-variable path.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "80")
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    _write_synthetic_session(
        projects,
        session_id="narrow1xyz",
        encoded_dir="-home-test-proj",
        first_prompt="first prompt",
    )

    runner = CliRunner()
    rebuild = runner.invoke(main, ["index", "rebuild", "--force", "--yes"])
    assert rebuild.exit_code == 0, rebuild.output

    result = runner.invoke(main, ["session", "list", "--no-refresh"])
    assert result.exit_code == 0, result.output
    # The Project column is suppressed in narrow mode.
    assert "Project" not in result.output
    # Other core columns remain.
    assert "UUID" in result.output
    assert "Summary" in result.output


def test_session_list_default_refreshes_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --no-refresh, `session list` runs reconcile (and pricing fetch) itself.

    The test drops a JSONL into the projects dir with NO prior index rebuild,
    then runs a plain `session list --json`. The row must appear because the
    command itself triggered reconcile.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "400")
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    _write_synthetic_session(
        projects,
        session_id="freshreconc",
        encoded_dir="-home-test-proj",
        first_prompt="self-refreshed prompt",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["session", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [r["session_id"] for r in payload] == ["freshreconc"]
    assert payload[0]["summary_text"] == "self-refreshed prompt"


def test_session_list_no_refresh_skips_pricing_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--no-refresh must not call PricingCache.load_or_fetch (which may hit the network)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True)

    from ccforensics import cli as cli_mod

    calls: list[bool] = []
    original = cli_mod.PricingCache.load_or_fetch

    def tracker(self: cli_mod.PricingCache) -> dict[str, dict[str, object]]:
        calls.append(True)
        return original(self)

    monkeypatch.setattr(cli_mod.PricingCache, "load_or_fetch", tracker)

    runner = CliRunner()
    result = runner.invoke(main, ["session", "list", "--no-refresh"])
    assert result.exit_code == 0
    assert calls == []


# ---------- tools command: integration ----------


def _write_tools_session(
    projects_dir: Path,
    *,
    session_id: str,
    encoded_dir: str,
    tool_specs: list[tuple[str, list[tuple[str, str]]]],
    started_ts: str = "2026-04-22T10:00:00Z",
) -> Path:
    """Write a JSONL with one user turn + N assistant turns, each turn
    emitting the given list of (tool_use_id, tool_name) pairs.

    ``tool_specs`` is a list of (request_id, tools) tuples — one assistant
    message per entry.
    """
    enc = projects_dir / encoded_dir
    enc.mkdir(parents=True, exist_ok=True)
    path = enc / f"{session_id}.jsonl"
    entries: list[dict[str, Any]] = [
        {
            "type": "user",
            "uuid": f"{session_id}-u1",
            "sessionId": session_id,
            "timestamp": started_ts,
            "isSidechain": False,
            "isMeta": False,
            "cwd": "/home/test/proj",
            "message": {"role": "user", "content": "go"},
        }
    ]
    for i, (req_id, tools) in enumerate(tool_specs, start=1):
        content: list[dict[str, Any]] = [{"type": "text", "text": "ok"}]
        for tu_id, tu_name in tools:
            content.append({"type": "tool_use", "id": tu_id, "name": tu_name, "input": {"x": 1}})
        entries.append(
            {
                "type": "assistant",
                "uuid": f"{session_id}-a{i}",
                "sessionId": session_id,
                "timestamp": f"2026-04-22T10:00:0{i}Z",
                "isSidechain": False,
                "isMeta": False,
                "requestId": req_id,
                "message": {
                    "id": f"{session_id}-m{i}",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5-20250929",
                    "content": content,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "service_tier": "standard",
                    },
                },
            }
        )
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e))
            f.write("\n")
    return path


def test_tools_command_default_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default tools render: header + footer present, exit 0."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    _write_tools_session(
        projects,
        session_id="tools-cli1",
        encoded_dir="-home-test-proj",
        tool_specs=[("r1", [("tu1", "Edit")])],
    )

    runner = CliRunner()
    result = runner.invoke(main, ["tools"])
    assert result.exit_code == 0, result.output
    assert "TOOL / MCP SERVER" in result.output
    assert "Isolated $ is exact" in result.output
    assert "Edit" in result.output


def test_tools_command_json_csv_mutually_exclusive() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["tools", "--json", "--csv"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_tools_command_detail_expands_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--detail expands mcp_server rows into per-tool rows."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    _write_tools_session(
        projects,
        session_id="tools-cli2",
        encoded_dir="-home-test-proj",
        tool_specs=[
            ("r1", [("tu1", "mcp__stratplaybook__query")]),
            ("r2", [("tu2", "mcp__stratplaybook__build")]),
        ],
    )

    runner = CliRunner()
    no_detail = runner.invoke(main, ["tools"])
    assert no_detail.exit_code == 0, no_detail.output
    assert "(server)" in no_detail.output
    assert "stratplaybook" in no_detail.output

    with_detail = runner.invoke(main, ["tools", "--detail"])
    assert with_detail.exit_code == 0, with_detail.output
    assert "mcp__stratplaybook__query" in with_detail.output
    assert "mcp__stratplaybook__build" in with_detail.output
    assert "(server)" not in with_detail.output


def test_tools_command_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--json emits envelope with rows + _meta.footer."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_pricing_cache(tmp_path)
    projects = tmp_path / ".claude" / "projects"
    _write_tools_session(
        projects,
        session_id="tools-cli3",
        encoded_dir="-home-test-proj",
        tool_specs=[("r1", [("tu1", "Edit")])],
    )

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "rows" in payload
    assert "_meta" in payload
    assert payload["_meta"]["footer"]
    assert any(r["group_key"] == "Edit" for r in payload["rows"])


def test_tools_command_no_refresh_skips_pricing_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--no-refresh must not call PricingCache.load_or_fetch — the tools
    report consumes pre-annotated costs from the index, not pricing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True)

    from ccforensics import cli as cli_mod

    calls: list[bool] = []
    original = cli_mod.PricingCache.load_or_fetch

    def tracker(self: cli_mod.PricingCache) -> dict[str, dict[str, object]]:
        calls.append(True)
        return original(self)

    monkeypatch.setattr(cli_mod.PricingCache, "load_or_fetch", tracker)

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "--no-refresh"])
    assert result.exit_code == 0
    assert calls == []
