from __future__ import annotations

import click

from . import __version__


@click.group()
@click.version_option(__version__, prog_name="ccforensics")
@click.option("-v", "--verbose", is_flag=True, help="Print per-session warnings.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """ccforensics — Claude Code session cost attribution."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose


@main.group()
def session() -> None:
    """Per-session listing and deep reports."""


@session.command("list")
def session_list() -> None:
    """List all discoverable sessions (not yet implemented)."""
    click.echo("session --list: not yet implemented (milestone M4)")


@main.command()
def aggregate() -> None:
    """Aggregate cost across a date range (not yet implemented)."""
    click.echo("aggregate: not yet implemented (milestone M9)")


@main.command()
def plugins() -> None:
    """Per-plugin all-time rollup (not yet implemented)."""
    click.echo("plugins: not yet implemented (milestone M9)")


@main.group()
def index() -> None:
    """SQLite index management."""


@index.command("stats")
def index_stats() -> None:
    """Show index stats (not yet implemented)."""
    click.echo("index --stats: not yet implemented (milestone M3)")


@index.command("rebuild")
def index_rebuild() -> None:
    """Drop and rebuild the index (not yet implemented)."""
    click.echo("index --rebuild: not yet implemented (milestone M3)")


if __name__ == "__main__":
    main()
