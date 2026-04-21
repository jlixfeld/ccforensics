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
