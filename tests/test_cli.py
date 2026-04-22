from __future__ import annotations

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
        (["session", "list"], "M4"),
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
