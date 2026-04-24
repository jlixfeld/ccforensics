from __future__ import annotations

import dataclasses
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from . import __version__
from .export import write_csv, write_json
from .index import (
    collect_stats,
    ensure_schema,
    open_connection,
    reconcile_projects_dir,
)
from .paths import ccforensics_cache_dir, claude_projects_dir
from .pricing import PricingCache
from .report._dates import parse_since, parse_until
from .report.aggregate import query_aggregate, render_aggregate
from .report.plugins import query_plugins, render_plugins
from .report.resolver import AmbiguousPrefix, SessionNotFound, resolve_session_id
from .report.session import (
    SessionReportNotFound,
    build_session_report,
    render_session_report,
)
from .report.session_list import query_session_list, render_session_list


def _index_db_path() -> Path:
    return ccforensics_cache_dir() / "index.sqlite"


def _open_index() -> sqlite3.Connection:
    conn = open_connection(_index_db_path())
    ensure_schema(conn)
    return conn


def _load_pricing() -> dict[str, dict[str, Any]]:
    cache = PricingCache(cache_file=ccforensics_cache_dir() / "litellm.json")
    data = cache.load_or_fetch()
    if cache.last_source == "fallback":
        click.echo(
            "WARNING: pricing fetch failed and no cache available; "
            "using built-in fallback table — costs may be stale.",
            err=True,
        )
    elif cache.last_source == "stale":
        click.echo(
            "WARNING: pricing refresh failed; using last cached pricing.",
            err=True,
        )
    return data


@click.group()
@click.version_option(__version__, prog_name="ccforensics")
@click.option("-v", "--verbose", is_flag=True, help="Print per-session warnings.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """ccforensics — Claude Code session cost attribution."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose


@main.group()
def session() -> None:
    """Per-session listing and deep reports."""


@session.command("list")
@click.option("--project", help="Filter by project path substring (case-insensitive).")
@click.option("--since", help="Date filter: YYYY-MM-DD | Nd | today | yesterday")
@click.option("--until", help="Date filter: YYYY-MM-DD | Nd | today | yesterday")
@click.option(
    "--grep",
    help=(
        "Case-insensitive substring on summary text. "
        "Does NOT match project paths or session IDs — use --project for project filtering."
    ),
)
@click.option(
    "--sort",
    "sort_key",
    type=click.Choice(["cost", "started", "last-active", "turns"]),
    default="last-active",
    show_default=True,
    help="Column to sort on.",
)
@click.option(
    "--reverse",
    is_flag=True,
    help=(
        "Reverse the sort order. Default is descending for every column "
        "(highest cost, most recent, most turns first), so --reverse gives "
        "ascending. Within ties, rows are ordered most-recently-active first."
    ),
)
@click.option("--limit", type=int, help="Cap the number of rows.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON to stdout.")
@click.option("--csv", "as_csv", is_flag=True, help="Emit CSV to stdout.")
@click.option(
    "--no-refresh",
    is_flag=True,
    help="Skip reconciliation and pricing fetch (schema still initializes on first run).",
)
@click.pass_context
def session_list(
    ctx: click.Context,
    project: str | None,
    since: str | None,
    until: str | None,
    grep: str | None,
    sort_key: str,
    reverse: bool,
    limit: int | None,
    as_json: bool,
    as_csv: bool,
    no_refresh: bool,
) -> None:
    """List all discoverable sessions."""
    if as_json and as_csv:
        raise click.UsageError("--json and --csv are mutually exclusive")

    conn = _open_index()

    if not no_refresh:
        pricing = _load_pricing()
        reconcile_projects_dir(conn, claude_projects_dir(), pricing)
        conn.commit()

    since_dt = parse_since(since) if since else None
    until_dt = parse_until(until) if until else None

    # click.Choice narrows `sort_key` at parse time; the cast is safe.
    rows = query_session_list(
        conn,
        project=project,
        since=since_dt,
        until=until_dt,
        grep=grep,
        sort_key=sort_key,  # type: ignore[arg-type]
        reverse=reverse,
        limit=limit,
    )

    if as_json:
        payload = [dataclasses.asdict(r) for r in rows]
        write_json(payload, sys.stdout)
        return
    if as_csv:
        headers = [
            "session_id",
            "project_path",
            "project_display",
            "started_at",
            "last_active_at",
            "duration_s",
            "turn_count",
            "total_cost_usd",
            "summary_text",
            "summary_source",
        ]
        write_csv((dataclasses.asdict(r) for r in rows), headers, sys.stdout)
        return

    Console().print(render_session_list(rows, verbose=bool(ctx.obj.get("verbose", False))))


@session.command("show")
@click.argument("spec")
@click.option(
    "--include-unattributed",
    is_flag=True,
    help="List the subagent files whose cost landed in the unattributed bucket.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON to stdout.")
@click.option("--csv", "as_csv", is_flag=True, help="Emit CSV (one row per bucket).")
@click.option(
    "--no-refresh",
    is_flag=True,
    help="Skip reconciliation and pricing fetch.",
)
def session_show(
    spec: str,
    include_unattributed: bool,
    as_json: bool,
    as_csv: bool,
    no_refresh: bool,
) -> None:
    """Show the deep report for one session.

    ``SPEC`` is a full session id, a prefix of ≥6 characters, or an
    absolute path to a session JSONL file.
    """
    if as_json and as_csv:
        raise click.UsageError("--json and --csv are mutually exclusive")

    conn = _open_index()

    if not no_refresh:
        pricing = _load_pricing()
        reconcile_projects_dir(conn, claude_projects_dir(), pricing)
        conn.commit()

    try:
        session_id = resolve_session_id(spec, conn)
    except AmbiguousPrefix as e:
        raise click.UsageError(str(e)) from e
    except SessionNotFound as e:
        raise click.UsageError(str(e)) from e

    try:
        report = build_session_report(conn, session_id, include_unattributed=include_unattributed)
    except SessionReportNotFound as e:
        raise click.UsageError(str(e)) from e

    if as_json:
        payload = dataclasses.asdict(report)
        write_json(payload, sys.stdout)
        return
    if as_csv:
        rows = [
            {
                "session_id": session_id,
                "bucket_kind": b.bucket_kind,
                "bucket_name": b.bucket_name,
                "cost_usd": b.cost_usd,
                "input_tokens": b.input_tokens,
                "output_tokens": b.output_tokens,
                "cache_create": b.cache_create,
                "cache_read": b.cache_read,
            }
            for b in report.buckets
        ]
        headers = [
            "session_id",
            "bucket_kind",
            "bucket_name",
            "cost_usd",
            "input_tokens",
            "output_tokens",
            "cache_create",
            "cache_read",
        ]
        write_csv(iter(rows), headers, sys.stdout)
        return

    Console().print(render_session_report(report))


@main.command()
@click.option("--since", help="Date filter: YYYY-MM-DD | Nd | today | yesterday")
@click.option("--until", help="Date filter: YYYY-MM-DD | Nd | today | yesterday")
@click.option("--project", help="Filter by project path substring (case-insensitive).")
@click.option(
    "--group-by",
    "group_by",
    type=click.Choice(["none", "project", "day", "week", "month", "plugin"]),
    default="none",
    show_default=True,
    help="Group-by key for aggregation.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON to stdout.")
@click.option("--csv", "as_csv", is_flag=True, help="Emit CSV to stdout.")
@click.option(
    "--no-refresh",
    is_flag=True,
    help="Skip reconciliation and pricing fetch.",
)
def aggregate(
    since: str | None,
    until: str | None,
    project: str | None,
    group_by: str,
    as_json: bool,
    as_csv: bool,
    no_refresh: bool,
) -> None:
    """Aggregate cost across a date range."""
    if as_json and as_csv:
        raise click.UsageError("--json and --csv are mutually exclusive")

    conn = _open_index()

    if not no_refresh:
        pricing = _load_pricing()
        reconcile_projects_dir(conn, claude_projects_dir(), pricing)
        conn.commit()

    since_dt = parse_since(since) if since else None
    until_dt = parse_until(until) if until else None

    rows = query_aggregate(
        conn,
        since=since_dt,
        until=until_dt,
        project=project,
        group_by=group_by,  # type: ignore[arg-type]
    )

    if as_json:
        write_json([dataclasses.asdict(r) for r in rows], sys.stdout)
        return
    if as_csv:
        headers = [
            "group_key",
            "total_cost_usd",
            "session_count",
            "input_tokens",
            "output_tokens",
            "cache_create",
            "cache_read",
        ]
        write_csv((dataclasses.asdict(r) for r in rows), headers, sys.stdout)
        return

    Console().print(render_aggregate(rows, group_by))


@main.command()
@click.option("--since", help="Date filter: YYYY-MM-DD | Nd | today | yesterday")
@click.option("--until", help="Date filter: YYYY-MM-DD | Nd | today | yesterday")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON to stdout.")
@click.option("--csv", "as_csv", is_flag=True, help="Emit CSV to stdout.")
@click.option(
    "--no-refresh",
    is_flag=True,
    help="Skip reconciliation and pricing fetch.",
)
def plugins(
    since: str | None,
    until: str | None,
    as_json: bool,
    as_csv: bool,
    no_refresh: bool,
) -> None:
    """Per-plugin all-time rollup."""
    if as_json and as_csv:
        raise click.UsageError("--json and --csv are mutually exclusive")

    conn = _open_index()

    if not no_refresh:
        pricing = _load_pricing()
        reconcile_projects_dir(conn, claude_projects_dir(), pricing)
        conn.commit()

    since_dt = parse_since(since) if since else None
    until_dt = parse_until(until) if until else None

    rows = query_plugins(conn, since=since_dt, until=until_dt)

    if as_json:
        write_json([dataclasses.asdict(r) for r in rows], sys.stdout)
        return
    if as_csv:
        headers = [
            "plugin",
            "total_cost_usd",
            "session_count",
            "most_used_agent_type",
            "agent_type_count",
            "most_used_skill",
            "skill_count",
            "first_seen",
            "last_seen",
        ]
        write_csv((dataclasses.asdict(r) for r in rows), headers, sys.stdout)
        return

    Console().print(render_plugins(rows))


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
    pricing = _load_pricing()
    stats = reconcile_projects_dir(conn, claude_projects_dir(), pricing)
    conn.commit()
    click.echo(
        f"indexed {stats.files_indexed} file(s); "
        f"scanned {stats.files_scanned}, skipped {stats.files_skipped_unchanged}"
    )


if __name__ == "__main__":
    main()
