from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path

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
    assert "index" in result.output


def test_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


@pytest.mark.parametrize(
    ("argv", "milestone"),
    [
        (["aggregate"], "M9"),
        (["plugins"], "M9"),
    ],
)
def test_stub_commands_echo_not_yet_implemented(argv: list[str], milestone: str) -> None:
    runner = CliRunner()
    result = runner.invoke(main, argv)
    assert result.exit_code == 0
    assert "not yet implemented" in result.output
    assert milestone in result.output


def test_verbose_flag_is_accepted() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["-v", "aggregate"])
    assert result.exit_code == 0


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
