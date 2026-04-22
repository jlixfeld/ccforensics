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
        (["index", "stats"], "M3"),
        (["index", "rebuild"], "M3"),
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
