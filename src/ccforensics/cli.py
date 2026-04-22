from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import click

from . import __version__
from .index import (
    collect_stats,
    ensure_schema,
    open_connection,
    reconcile_projects_dir,
)
from .paths import ccforensics_cache_dir, claude_projects_dir
from .pricing import PricingCache


def _index_db_path() -> Path:
    return ccforensics_cache_dir() / "index.sqlite"


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
    """Show index stats."""
    db = _index_db_path()
    conn = open_connection(db)
    ensure_schema(conn)
    stats = collect_stats(conn, db)
    lines = [
        f"files: {stats.files}",
        f"sessions: {stats.sessions}",
        f"messages: {stats.messages}",
        f"subagent_spawns: {stats.subagent_spawns}",
        f"skill_activations: {stats.skill_activations}",
        f"db_size_bytes: {stats.db_size_bytes}",
    ]
    if stats.last_refresh:
        ts = datetime.fromtimestamp(stats.last_refresh, tz=UTC).isoformat()
        lines.append(f"last_refresh: {ts}")
    else:
        lines.append("last_refresh: never")
    click.echo("\n".join(lines))


@index.command("rebuild")
@click.option(
    "--force",
    is_flag=True,
    help="Drop the existing DB before rebuilding (slow; implies --yes).",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt when --force.")
def index_rebuild(force: bool, yes: bool) -> None:
    """Bring the index up to date.

    By default this is incremental: each JSONL is skipped if its
    ``(path, mtime_ns, size)`` hasn't changed since the last run. Use
    ``--force`` to drop the DB and re-parse everything from scratch.
    """
    db = _index_db_path()
    if force:
        if db.exists() and not yes:
            click.confirm(f"Delete and rebuild index at {db}?", abort=True)
        if db.exists():
            db.unlink()
    conn = open_connection(db)
    ensure_schema(conn)
    pricing = PricingCache(cache_file=ccforensics_cache_dir() / "litellm.json").load_or_fetch()
    stats = reconcile_projects_dir(conn, claude_projects_dir(), pricing)
    conn.commit()
    click.echo(
        f"indexed {stats.files_indexed} file(s); "
        f"scanned {stats.files_scanned}, skipped {stats.files_skipped_unchanged}"
    )


if __name__ == "__main__":
    main()
