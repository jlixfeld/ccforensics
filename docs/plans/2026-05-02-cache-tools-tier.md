# Cache efficiency, per-tool/per-MCP, service-tier capture — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three additive features to ccforensics — exact cache-efficiency metrics, honest per-tool/per-MCP spend reporting (precise isolated cost + upper-bound shared exposure), and `service_tier` capture (precursor to fast-mode pricing, deferred until verifiable).

**Architecture:** Schema v2 → v3 adds `messages.service_tier` and a new `message_tool_uses` table (one row per tool_use block, since the current writer stores only the first per assistant message). Cache-eff is post-aggregation computation over already-stored values via a small helper module. Per-tool report is a new CLI subcommand backed by a single SQL query; it does not participate in the bucket-attribution invariant.

**Tech Stack:** Python 3.13, uv, SQLite (stdlib `sqlite3`), pydantic v2, click, pytest, ruff, mypy strict.

**Spec:** `docs/specs/2026-05-02-cache-tools-tier-design.md`

---

## File map

**Modify:**
- `src/ccforensics/index.py` — schema v3 migration, `CURRENT_SCHEMA_VERSION` bump, `_insert_message` (or equivalent in current code) loop change
- `src/ccforensics/models.py` — add `service_tier` field to `UsageBlock`
- `src/ccforensics/report/session.py` — cache-eff columns, service-tier breakdown footer
- `src/ccforensics/report/aggregate.py` — cache-eff columns, service-tier breakdown footer
- `src/ccforensics/report/__init__.py` — re-export new tools module
- `src/ccforensics/cli.py` — add `tools` command
- `tests/test_attribution.py` — invariant guard for v3 migration
- `scripts/redact_jsonl.py` — `service_tier` to allow-list
- `tests/fixtures/real/redacted_session.jsonl` — regenerate
- `README.md` — `tools` example, cache-eff sample, service-tier note
- `CHANGELOG.md` — Unreleased entry

**Create:**
- `src/ccforensics/report/_cache.py` — `cache_metrics()` helper
- `src/ccforensics/report/tools.py` — query + render
- `tests/test_jsonl_multi_tool.py`
- `tests/test_service_tier.py`
- `tests/test_cache_metrics.py`
- `tests/test_tools_report.py`

---

## Task 1: Schema v3 migration

**Files:**
- Modify: `src/ccforensics/index.py:65` (`CURRENT_SCHEMA_VERSION`)
- Modify: `src/ccforensics/index.py:67` (`MIGRATIONS` list — append entry)
- Test: `tests/test_index_schema.py` (existing file — append test)

- [ ] **Step 1: Write failing test for v3 schema**

Append to `tests/test_index_schema.py`:

```python
def test_schema_v3_creates_message_tool_uses_and_service_tier(tmp_path: Path) -> None:
    from ccforensics.index import ensure_schema, open_connection, CURRENT_SCHEMA_VERSION

    db = tmp_path / "v3.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)

    assert CURRENT_SCHEMA_VERSION >= 3

    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "service_tier" in cols

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "message_tool_uses" in tables

    mtu_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(message_tool_uses)").fetchall()
    }
    assert mtu_cols == {
        "message_dedup_key",
        "ordinal",
        "tool_use_id",
        "tool_name",
        "mcp_server",
        "args_size_bytes",
    }


def test_schema_v3_cold_backfill_resets_file_mtime(tmp_path: Path) -> None:
    """v2 → v3 migration MUST reset files.mtime_ns to force re-reconcile so
    message_tool_uses and service_tier populate from existing files."""
    from ccforensics.index import open_connection, ensure_schema

    # Build a v2 db manually then migrate.
    db = tmp_path / "v2.sqlite"
    conn = open_connection(db)
    # Seed v0 → v2 by running existing migrations up to v2 only:
    conn.executescript(
        """
        CREATE TABLE files (
            path TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            agent_id TEXT,
            schema_version TEXT,
            parse_warnings INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO files (path, mtime_ns, size, session_id, kind)
        VALUES ('/x.jsonl', 999999, 100, 's', 'main');
        """
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()

    ensure_schema(conn)

    row = conn.execute("SELECT mtime_ns FROM files WHERE path='/x.jsonl'").fetchone()
    assert row[0] == 0, "v3 migration must reset mtime_ns to force cold backfill"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_index_schema.py::test_schema_v3_creates_message_tool_uses_and_service_tier tests/test_index_schema.py::test_schema_v3_cold_backfill_resets_file_mtime -v`

Expected: both FAIL (`CURRENT_SCHEMA_VERSION` is 2; new table doesn't exist).

- [ ] **Step 3: Bump schema version + add migration entry**

Edit `src/ccforensics/index.py`:

```python
# At line 65 (CURRENT_SCHEMA_VERSION = 2):
CURRENT_SCHEMA_VERSION = 3
```

Append to the `MIGRATIONS: list[list[str]]` list (which currently has two inner lists for v0→v1 and v1→v2):

```python
    # v2 → v3: add messages.service_tier column and new message_tool_uses
    # table (one row per tool_use block on an assistant message — the writer
    # currently stores only the first). Trailing UPDATE forces cold backfill
    # on next reconcile so existing data populates.
    [
        "ALTER TABLE messages ADD COLUMN service_tier TEXT",
        """CREATE TABLE message_tool_uses (
            message_dedup_key  TEXT NOT NULL REFERENCES messages(dedup_key) ON DELETE CASCADE,
            ordinal            INTEGER NOT NULL,
            tool_use_id        TEXT NOT NULL,
            tool_name          TEXT NOT NULL,
            mcp_server         TEXT,
            args_size_bytes    INTEGER NOT NULL,
            PRIMARY KEY (message_dedup_key, ordinal)
        )""",
        "CREATE INDEX idx_mtu_tool_name ON message_tool_uses(tool_name)",
        "CREATE INDEX idx_mtu_mcp_server ON message_tool_uses(mcp_server) WHERE mcp_server IS NOT NULL",
        "UPDATE files SET mtime_ns = 0",
    ],
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_index_schema.py -v`

Expected: PASS — both new tests + all existing schema tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/index.py tests/test_index_schema.py
git commit -m "feat(index): schema v3 — service_tier col + message_tool_uses table

Adds messages.service_tier column (nullable) and a new message_tool_uses
table keyed (message_dedup_key, ordinal). Migration ends with
'UPDATE files SET mtime_ns = 0' so the next reconcile re-parses every
file and cold-backfills the new columns/table."
```

---

## Task 2: `UsageBlock.service_tier` field

**Files:**
- Modify: `src/ccforensics/models.py` (`UsageBlock` definition)
- Test: `tests/test_models.py` (existing file — append test)

- [ ] **Step 1: Write failing test**

Append to `tests/test_models.py`:

```python
def test_usage_block_captures_service_tier() -> None:
    from ccforensics.models import UsageBlock

    block = UsageBlock.model_validate(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "service_tier": "priority",
        }
    )
    assert block.service_tier == "priority"


def test_usage_block_service_tier_optional() -> None:
    from ccforensics.models import UsageBlock

    block = UsageBlock.model_validate({"input_tokens": 100, "output_tokens": 50})
    assert block.service_tier is None
```

- [ ] **Step 2: Run test to verify failure mode**

Run: `uv run pytest tests/test_models.py::test_usage_block_captures_service_tier -v`

Expected: PASS for the optional case (extra="allow"), but `block.service_tier` access raises `AttributeError` on the typed access — first test FAILS.

- [ ] **Step 3: Add field**

Edit `src/ccforensics/models.py`. Find the `UsageBlock` class (should look approximately like):

```python
class UsageBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
```

Add the new field at the end of the field list:

```python
class UsageBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    service_tier: str | None = None
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_models.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/models.py tests/test_models.py
git commit -m "feat(models): UsageBlock.service_tier field

Surfaces the response-side service tier (standard | priority | batch)
emitted by Anthropic's API in usage blocks. Stored verbatim; pricing
branch is deferred until a fast-mode session lands and the signal is
verifiable end-to-end."
```

---

## Task 3: `write_message` captures `service_tier` and ALL tool_use blocks

**Files:**
- Modify: `src/ccforensics/index.py` — `_insert_message` function near line 460 (the function that writes a message row); also bump the `INSERT OR REPLACE INTO messages` column list and parameter tuple
- Create: `tests/test_jsonl_multi_tool.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_jsonl_multi_tool.py`:

```python
"""Multi-tool turn capture — every tool_use block lands in message_tool_uses.

Today the writer ``break``s after the first tool_use, so siblings are dropped
from indexed columns. Schema v3 adds message_tool_uses to capture all of them
without breaking ``messages.tool_name`` (load-bearing for tree.discover_spawn).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ccforensics.index import ensure_schema, open_connection, reconcile_projects_dir

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


def _assistant_multi_tool(
    uuid: str, sid: str, ts: str, *, msg_id: str, req_id: str
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
            "model": "claude-sonnet-4-5-20250929",
            "content": [
                {"type": "text", "text": "ok"},
                {
                    "type": "tool_use",
                    "id": "tu_native",
                    "name": "Edit",
                    "input": {"file_path": "/x.py", "old": "a", "new": "b"},
                },
                {
                    "type": "tool_use",
                    "id": "tu_mcp1",
                    "name": "mcp__stratplaybook__query",
                    "input": {"query": "test"},
                },
                {
                    "type": "tool_use",
                    "id": "tu_mcp2",
                    "name": "mcp__strattrader-collector__get_bars",
                    "input": {"symbol": "AAPL"},
                },
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "service_tier": "standard",
            },
        },
    }


def test_multi_tool_turn_writes_all_tool_use_rows(
    tmp_path: Path, pricing_data: dict
) -> None:
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-multi"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "go", cwd="/home/test"),
            _assistant_multi_tool(
                "u2", sid, "2026-04-22T10:00:05Z", msg_id="m1", req_id="r1"
            ),
        ],
    )
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    rows = conn.execute(
        """SELECT ordinal, tool_use_id, tool_name, mcp_server, args_size_bytes
           FROM message_tool_uses ORDER BY ordinal"""
    ).fetchall()

    # 3 tool_use blocks (text block does not produce a row); ordinals match
    # positions WITHIN the message.content array (text is at 0, tools at 1,2,3).
    assert len(rows) == 3
    assert rows[0] == (1, "tu_native", "Edit", None, rows[0][4])
    assert rows[1] == (
        2,
        "tu_mcp1",
        "mcp__stratplaybook__query",
        "stratplaybook",
        rows[1][4],
    )
    assert rows[2] == (
        3,
        "tu_mcp2",
        "mcp__strattrader-collector__get_bars",
        "strattrader-collector",
        rows[2][4],
    )
    # args_size_bytes is precise byte length of canonical JSON; non-zero for
    # all three, monotonically derivable but we only assert > 0 here.
    for r in rows:
        assert r[4] > 0


def test_multi_tool_messages_tool_name_unchanged(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Regression guard: messages.tool_name must still equal the FIRST tool_use's
    name. tree.discover_spawn relies on this."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-first-tool"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "go", cwd="/home/test"),
            _assistant_multi_tool(
                "u2", sid, "2026-04-22T10:00:05Z", msg_id="m1", req_id="r1"
            ),
        ],
    )
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT tool_name, tool_use_id FROM messages WHERE uuid='u2'"
    ).fetchone()
    assert row[0] == "Edit"
    assert row[1] == "tu_native"


def test_service_tier_persisted_on_message(
    tmp_path: Path, pricing_data: dict
) -> None:
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-tier"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "go", cwd="/home/test"),
            _assistant_multi_tool(
                "u2", sid, "2026-04-22T10:00:05Z", msg_id="m1", req_id="r1"
            ),
        ],
    )
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT service_tier FROM messages WHERE uuid='u2'"
    ).fetchone()
    assert row[0] == "standard"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_jsonl_multi_tool.py -v`

Expected: FAIL — current writer breaks after first tool_use; `message_tool_uses` is empty; `service_tier` column exists (Task 1) but writer doesn't populate it.

- [ ] **Step 3: Modify the message writer**

Edit `src/ccforensics/index.py`. Locate the `_insert_message` function (currently around line 460; the body shows `tool_use_id = None / tool_name = None / for block in msg.content: if block.type == "tool_use": tool_use_id = block.id; tool_name = block.name; break`).

Add `import json` at the top of the file if not already present.

Replace the tool_use extraction block with:

```python
    tool_use_id = None
    tool_name = None
    tool_uses_for_aux: list[tuple[int, str, str, object]] = []
    if msg and msg.content:
        for ordinal, block in enumerate(msg.content):
            if block.type == "tool_use":
                if tool_use_id is None:
                    tool_use_id = block.id
                    tool_name = block.name
                tool_uses_for_aux.append((ordinal, block.id, block.name, block.input))
```

Locate the `INSERT OR REPLACE INTO messages` statement. Add `service_tier` to the column list and the parameter tuple. The column list block should become:

```python
        """INSERT OR REPLACE INTO messages (
            dedup_key, file_path, session_id, uuid, parent_uuid,
            source_tool_use_id, source_tool_assistant_uuid,
            tool_use_id, tool_name, agent_id, role, type, model, ts,
            is_sidechain, is_meta,
            input_tokens, output_tokens, cache_creation, cache_read, cost_usd,
            service_tier
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
```

And the parameter tuple's tail should become:

```python
            usage.cache_read_input_tokens if usage else None,
            cost_usd,
            usage.service_tier if usage else None,
        ),
```

Immediately after the `conn.execute(...)` call that inserts the message row, add the per-tool_use loop:

```python
    for ordinal, tu_id, tu_name, tu_input in tool_uses_for_aux:
        mcp_server = (
            tu_name.split("__", 2)[1]
            if tu_name.startswith("mcp__") and tu_name.count("__") >= 2
            else None
        )
        try:
            args_size = len(
                json.dumps(tu_input, sort_keys=True, separators=(",", ":")).encode()
            )
        except (TypeError, ValueError):
            args_size = 0
        conn.execute(
            """INSERT OR REPLACE INTO message_tool_uses
               (message_dedup_key, ordinal, tool_use_id, tool_name, mcp_server, args_size_bytes)
               VALUES (?,?,?,?,?,?)""",
            (key, ordinal, tu_id, tu_name, mcp_server, args_size),
        )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_jsonl_multi_tool.py tests/test_attribution.py tests/test_index_reconcile.py -v`

Expected: PASS — new tests pass and existing reconcile/attribution tests still pass (regression check on `messages.tool_name` semantics + invariant).

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/index.py tests/test_jsonl_multi_tool.py
git commit -m "feat(index): capture every tool_use block + service_tier per message

Replaces break-after-first-tool_use with a loop populating the new
message_tool_uses table. messages.tool_name still stores the FIRST
tool_use's name (load-bearing for tree.discover_spawn), so spawn
discovery is unchanged. Adds service_tier capture from the usage block."
```

---

## Task 4: `cache_metrics()` helper

**Files:**
- Create: `src/ccforensics/report/_cache.py`
- Create: `tests/test_cache_metrics.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_cache_metrics.py`:

```python
"""Cache efficiency math — exact arithmetic over stored values."""

from __future__ import annotations

from dataclasses import dataclass

from ccforensics.report._cache import CacheMetrics, CacheRow, cache_metrics


@dataclass(frozen=True)
class FakePricing:
    input_cost: float
    cache_creation_cost: float
    cache_read_cost: float


def _resolver(table: dict[str, FakePricing]):
    def lookup(model: str) -> FakePricing | None:
        return table.get(model)

    return lookup


def test_cache_metrics_exact_savings_and_efficiency() -> None:
    rows = [
        CacheRow(
            model="claude-sonnet-4-5-20250929",
            input_tokens=1000,
            cache_creation=2000,
            cache_read=8000,
        )
    ]
    pricing = {
        "claude-sonnet-4-5-20250929": FakePricing(
            input_cost=3e-6,
            cache_creation_cost=3.75e-6,
            cache_read_cost=0.3e-6,
        )
    }
    m = cache_metrics(rows, _resolver(pricing))

    # savings = cache_read * (input - read) = 8000 * (3e-6 - 0.3e-6) = 0.0216
    assert abs(m.savings_usd - 0.0216) < 1e-9

    # eff_pct = (cache_read*read) / (cache_read*read + create*create + input*input) * 100
    # numerator = 8000 * 0.3e-6 = 0.0024
    # denom     = 0.0024 + 2000*3.75e-6 + 1000*3e-6 = 0.0024 + 0.0075 + 0.003 = 0.0129
    # eff_pct   = 0.0024 / 0.0129 * 100 = 18.6046511627907
    assert abs(m.eff_pct - (0.0024 / 0.0129 * 100)) < 1e-9

    assert m.rows_excluded_for_unknown_model == 0


def test_cache_metrics_zero_cache_read_returns_zero() -> None:
    rows = [
        CacheRow(
            model="claude-sonnet-4-5-20250929",
            input_tokens=1000,
            cache_creation=0,
            cache_read=0,
        )
    ]
    pricing = {
        "claude-sonnet-4-5-20250929": FakePricing(
            input_cost=3e-6, cache_creation_cost=3.75e-6, cache_read_cost=0.3e-6
        )
    }
    m = cache_metrics(rows, _resolver(pricing))
    assert m.savings_usd == 0.0
    assert m.eff_pct == 0.0


def test_cache_metrics_unknown_model_excluded_and_counted() -> None:
    rows = [
        CacheRow(model="known", input_tokens=1000, cache_creation=0, cache_read=2000),
        CacheRow(model="unknown-x", input_tokens=500, cache_creation=0, cache_read=500),
    ]
    pricing = {
        "known": FakePricing(
            input_cost=3e-6, cache_creation_cost=3.75e-6, cache_read_cost=0.3e-6
        )
    }
    m = cache_metrics(rows, _resolver(pricing))

    # Only the "known" row contributes:
    # savings = 2000 * (3e-6 - 0.3e-6) = 0.0054
    assert abs(m.savings_usd - 0.0054) < 1e-9
    assert m.rows_excluded_for_unknown_model == 1


def test_cache_metrics_empty_input() -> None:
    m = cache_metrics([], _resolver({}))
    assert m == CacheMetrics(savings_usd=0.0, eff_pct=0.0, rows_excluded_for_unknown_model=0)
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_cache_metrics.py -v`

Expected: FAIL — module `ccforensics.report._cache` does not exist.

- [ ] **Step 3: Implement helper module**

Create `src/ccforensics/report/_cache.py`:

```python
"""Cache efficiency + savings — exact arithmetic over stored values.

Both metrics are derivations from already-stored token counts and per-model
pricing. No estimation, no banding.

- savings_usd = sum_over_rows( cache_read * (input_price - read_price) )
- eff_pct     = sum( cache_read * read_price )
              / sum( cache_read*read_price + cache_create*create_price + input*input_price )
              * 100

Models without resolvable pricing are excluded from the calculation and
counted separately so callers can surface a "N rows excluded — unknown
model pricing" footer warning.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol


class _PricingProto(Protocol):
    @property
    def input_cost(self) -> float: ...

    @property
    def cache_creation_cost(self) -> float: ...

    @property
    def cache_read_cost(self) -> float: ...


@dataclass(frozen=True)
class CacheRow:
    model: str
    input_tokens: int
    cache_creation: int
    cache_read: int


@dataclass(frozen=True)
class CacheMetrics:
    savings_usd: float
    eff_pct: float
    rows_excluded_for_unknown_model: int


def cache_metrics(
    rows: Iterable[CacheRow],
    resolve_pricing: Callable[[str], _PricingProto | None],
) -> CacheMetrics:
    savings = 0.0
    num = 0.0
    den = 0.0
    excluded = 0

    for row in rows:
        pricing = resolve_pricing(row.model)
        if pricing is None:
            excluded += 1
            continue
        savings += row.cache_read * (pricing.input_cost - pricing.cache_read_cost)
        num += row.cache_read * pricing.cache_read_cost
        den += (
            row.cache_read * pricing.cache_read_cost
            + row.cache_creation * pricing.cache_creation_cost
            + row.input_tokens * pricing.input_cost
        )

    eff = (num / den * 100.0) if den > 0 else 0.0
    return CacheMetrics(
        savings_usd=savings,
        eff_pct=eff,
        rows_excluded_for_unknown_model=excluded,
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_cache_metrics.py -v`

Expected: PASS — all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/report/_cache.py tests/test_cache_metrics.py
git commit -m "feat(report): cache_metrics helper — exact savings + cost-weighted efficiency

savings_usd = cache_read * (input_price - read_price), summed per-model.
eff_pct = cost-weighted ratio of cache_read cost vs total token cost.
Unknown-model rows are excluded and counted (driver for footer warning)."
```

---

## Task 5: `session show` — cache columns + service-tier breakdown

**Files:**
- Modify: `src/ccforensics/report/session.py` (totals block)
- Test: `tests/test_report_session.py` (existing — append tests)

- [ ] **Step 1: Read existing session.py to find the totals-render call site**

Run: `grep -n "def render\|def show\|total\|TOTAL" src/ccforensics/report/session.py`

Note the function that renders the totals block; the cache-eff line goes there. Also identify the dict that builds JSON output.

- [ ] **Step 2: Write failing test**

Append to `tests/test_report_session.py`:

```python
def test_session_show_includes_cache_efficiency(
    tmp_path: Path, pricing_data: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    from ccforensics.cli import main as cli_main
    from click.testing import CliRunner

    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-cache"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test"),
            _assistant(
                "u2",
                sid,
                "2026-04-22T10:00:05Z",
                msg_id="m1",
                req_id="r1",
                input_tokens=1000,
                cache_create=2000,
                cache_read=8000,
            ),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "session",
            "show",
            sid,
            "--no-refresh",
        ],
        env={"CCFORENSICS_PROJECTS_DIR": str(proj), "CCFORENSICS_DB": str(tmp_path / "i.sqlite")},
    )
    # Pre-populate the index since --no-refresh:
    # NOTE: instead, drop --no-refresh and let the command refresh from
    # the env-overridden projects dir. Adjust based on existing CLI test pattern.
    assert result.exit_code == 0, result.output
    assert "Cache:" in result.output
    assert "efficiency" in result.output
    assert "saved $" in result.output


def test_session_show_json_includes_cache_keys(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Ensure JSON output carries cache_eff_pct + cache_savings_usd."""
    # Mirror the structure of the test above but pass --json. Parse the JSON
    # and assert the keys exist at the top level.
    ...  # IMPLEMENTER: copy structure of test above; replace assertions:
    # data = json.loads(result.output)
    # assert "cache_eff_pct" in data
    # assert "cache_savings_usd" in data
    # assert "cache_read_tokens" in data
    # assert "cache_creation_tokens" in data
```

> **NOTE for implementer:** check existing `tests/test_report_session.py` for the established CLI-invocation pattern (env vars vs. fixtures). Match that pattern exactly. The test above is illustrative; copy structure of any existing `session show` test verbatim and add the assertions for `Cache:` / `efficiency` / `saved $` (text mode) or the four JSON keys.

- [ ] **Step 3: Run test to verify failure**

Run: `uv run pytest tests/test_report_session.py -v -k cache`

Expected: FAIL — output does not contain `Cache:` line yet; JSON missing keys.

- [ ] **Step 4: Add cache-metrics rendering to session.py**

In `src/ccforensics/report/session.py`, in the totals-render function:

1. Import the helper:

```python
from ccforensics.report._cache import CacheRow, cache_metrics
```

2. After the existing per-bucket totals query, add a query for cache-row data per model:

```python
cache_rows_data = conn.execute(
    """SELECT model, COALESCE(SUM(input_tokens), 0),
              COALESCE(SUM(cache_creation), 0), COALESCE(SUM(cache_read), 0)
       FROM messages WHERE session_id = ? AND model IS NOT NULL
       GROUP BY model""",
    (session_id,),
).fetchall()

cache_rows = [
    CacheRow(model=m, input_tokens=i, cache_creation=cc, cache_read=cr)
    for (m, i, cc, cr) in cache_rows_data
]
metrics = cache_metrics(cache_rows, resolve_pricing)
total_cache_read = sum(r.cache_read for r in cache_rows)
total_cache_create = sum(r.cache_creation for r in cache_rows)
```

3. In the text rendering, after the existing totals lines:

```python
def _human_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)

if total_cache_read or total_cache_create:
    eff_str = f"{metrics.eff_pct:.1f}%" if metrics.eff_pct else "—"
    out_lines.append(
        f"Cache: {_human_count(total_cache_read)} read · "
        f"{_human_count(total_cache_create)} created · "
        f"{eff_str} efficiency · saved ${metrics.savings_usd:.2f}"
    )
    if metrics.rows_excluded_for_unknown_model:
        out_lines.append(
            f"  (excluded {metrics.rows_excluded_for_unknown_model} model(s) "
            "with no resolvable pricing)"
        )
```

4. In the JSON-build dict, add:

```python
output_dict["cache_read_tokens"] = total_cache_read
output_dict["cache_creation_tokens"] = total_cache_create
output_dict["cache_eff_pct"] = metrics.eff_pct
output_dict["cache_savings_usd"] = metrics.savings_usd
```

> **NOTE:** field names in your existing `output_dict` may differ. The four keys above are spec-mandated; place them at the top level of the JSON output alongside the existing per-session totals.

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run pytest tests/test_report_session.py -v`

Expected: PASS — new cache tests pass, all existing session tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/ccforensics/report/session.py tests/test_report_session.py
git commit -m "feat(report): cache efficiency + savings on 'session show'

Adds 'Cache: NM read · NK created · NN.N% efficiency · saved \$X.XX' line
to the totals block (text + JSON). Cost-weighted efficiency (per spec)
because cache_read is ~10x cheaper than input — token ratio overstates
dollar savings."
```

---

## Task 6: `aggregate` — cache columns + service-tier breakdown

**Files:**
- Modify: `src/ccforensics/report/aggregate.py`
- Test: `tests/test_aggregate_and_plugins.py` (existing — append)

- [ ] **Step 1: Read existing aggregate.py for the totals code path**

Run: `grep -n "def \|total\|TOTAL\|json" src/ccforensics/report/aggregate.py | head -30`

- [ ] **Step 2: Write failing test**

Append to `tests/test_aggregate_and_plugins.py` (mirror the pattern of any existing aggregate test in that file):

```python
def test_aggregate_includes_cache_efficiency(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Aggregate report carries the same cache totals line as session show."""
    from ccforensics.cli import main as cli_main
    from click.testing import CliRunner

    # Build two sessions with cache activity. Mirror existing test pattern in
    # this file for fixture setup. After invoking 'aggregate':
    runner = CliRunner()
    # ... build fixtures, invoke ...
    # assert "Cache:" in result.output
    # assert "efficiency" in result.output


def test_aggregate_json_includes_cache_keys(
    tmp_path: Path, pricing_data: dict
) -> None:
    """JSON output includes cache_eff_pct, cache_savings_usd."""
    # ... same shape, assert JSON keys ...
```

> **NOTE for implementer:** this file already contains tests that invoke the `aggregate` command. Copy that exact invocation pattern and add the cache assertions. Don't write new boilerplate — reuse the existing fixture builders.

- [ ] **Step 3: Run tests to verify failure**

Run: `uv run pytest tests/test_aggregate_and_plugins.py -v -k cache`

Expected: FAIL.

- [ ] **Step 4: Add cache rendering to aggregate.py**

In `src/ccforensics/report/aggregate.py`, in the totals-render function:

```python
from ccforensics.report._cache import CacheRow, cache_metrics
```

Aggregate cache_rows across the scoped session set:

```python
cache_rows_data = conn.execute(
    """SELECT model, COALESCE(SUM(input_tokens), 0),
              COALESCE(SUM(cache_creation), 0), COALESCE(SUM(cache_read), 0)
       FROM messages
       WHERE session_id IN (""" + ",".join("?" * len(session_ids)) + """)
         AND model IS NOT NULL
       GROUP BY model""",
    tuple(session_ids),
).fetchall()

cache_rows = [
    CacheRow(model=m, input_tokens=i, cache_creation=cc, cache_read=cr)
    for (m, i, cc, cr) in cache_rows_data
]
metrics = cache_metrics(cache_rows, resolve_pricing)
total_cache_read = sum(r.cache_read for r in cache_rows)
total_cache_create = sum(r.cache_creation for r in cache_rows)
```

Render the same `Cache:` line as Task 5 (extract `_human_count` to `report/_format.py` if it isn't already there; otherwise duplicate inline — the codebase prefers small duplication over new modules).

JSON output: same four keys at top level of the aggregate output dict.

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run pytest tests/test_aggregate_and_plugins.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ccforensics/report/aggregate.py tests/test_aggregate_and_plugins.py
git commit -m "feat(report): cache efficiency + savings on 'aggregate'

Same totals line and JSON keys as 'session show', scoped to whatever the
aggregate command is filtering on (days/since/until)."
```

---

## Task 7: `tools` report — query + default server-rolled render

**Files:**
- Create: `src/ccforensics/report/tools.py`
- Create: `tests/test_tools_report.py`

- [ ] **Step 1: Write failing test for the query layer**

Create `tests/test_tools_report.py`:

```python
"""Per-tool / per-MCP report — isolated cost is exact, shared exposure is
an upper bound, and the two columns must not be conflated."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ccforensics.index import ensure_schema, open_connection, reconcile_projects_dir
from ccforensics.report.tools import ToolRow, query_tool_costs

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


def _user(uuid: str, sid: str, ts: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": sid,
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
        "message": {"role": "user", "content": "go"},
        **extra,
    }


def _assistant(
    uuid: str,
    sid: str,
    ts: str,
    *,
    msg_id: str,
    req_id: str,
    tools: list[tuple[str, str]],  # (tool_use_id, tool_name)
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": "ok"}]
    for tu_id, tu_name in tools:
        content.append(
            {"type": "tool_use", "id": tu_id, "name": tu_name, "input": {"x": 1}}
        )
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
            "model": "claude-sonnet-4-5-20250929",
            "content": content,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "service_tier": "standard",
            },
        },
    }


def _build_corpus(tmp_path: Path, pricing_data: dict) -> tuple[Any, list[str]]:
    """3 single-tool turns + 1 multi-tool turn. Returns (conn, [session_ids])."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-tools"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", cwd="/home/test"),
            _assistant(  # single Edit
                "u2", sid, "2026-04-22T10:00:01Z",
                msg_id="m1", req_id="r1",
                tools=[("tu1", "Edit")],
            ),
            _assistant(  # single Read
                "u3", sid, "2026-04-22T10:00:02Z",
                msg_id="m2", req_id="r2",
                tools=[("tu2", "Read")],
            ),
            _assistant(  # single MCP
                "u4", sid, "2026-04-22T10:00:03Z",
                msg_id="m3", req_id="r3",
                tools=[("tu3", "mcp__stratplaybook__query")],
            ),
            _assistant(  # multi-tool: Edit + MCP
                "u5", sid, "2026-04-22T10:00:04Z",
                msg_id="m4", req_id="r4",
                tools=[
                    ("tu4", "Edit"),
                    ("tu5", "mcp__stratplaybook__build"),
                ],
            ),
        ],
    )
    db = tmp_path / "i.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)
    return conn, [sid]


def test_query_tool_costs_isolated_and_shared(
    tmp_path: Path, pricing_data: dict
) -> None:
    conn, session_ids = _build_corpus(tmp_path, pricing_data)
    rows = query_tool_costs(conn, session_ids=session_ids, detail=False, top=50, sort="isolated_cost")
    by_key = {r.group_key: r for r in rows}

    # Edit: 1 isolated turn (m1), 1 shared turn (m4)
    assert by_key["Edit"].isolated_turns == 1
    assert by_key["Edit"].shared_turns == 1
    assert by_key["Edit"].invocations == 2

    # Read: 1 isolated, 0 shared
    assert by_key["Read"].isolated_turns == 1
    assert by_key["Read"].shared_turns == 0

    # mcp__stratplaybook (server roll-up): 1 isolated (m3 query), 1 shared (m4 build)
    assert by_key["stratplaybook"].group_kind == "mcp_server"
    assert by_key["stratplaybook"].isolated_turns == 1
    assert by_key["stratplaybook"].shared_turns == 1
    assert by_key["stratplaybook"].invocations == 2


def test_isolated_cost_sum_equals_single_tool_turn_cost_sum(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Spec invariant: sum(isolated_cost across all tools) == sum of cost
    of single-tool turns in messages. Exact equality."""
    conn, session_ids = _build_corpus(tmp_path, pricing_data)
    rows = query_tool_costs(conn, session_ids=session_ids, detail=False, top=50, sort="isolated_cost")

    isolated_total = sum(r.isolated_cost_usd for r in rows)

    # Reference: cost of messages whose dedup_key has exactly 1 row in
    # message_tool_uses (single-tool turns).
    expected = conn.execute(
        """SELECT COALESCE(SUM(m.cost_usd), 0)
           FROM messages m
           JOIN (
             SELECT message_dedup_key, COUNT(*) AS n
             FROM message_tool_uses GROUP BY message_dedup_key
           ) c ON c.message_dedup_key = m.dedup_key
           WHERE c.n = 1 AND m.session_id IN (?)""",
        (session_ids[0],),
    ).fetchone()[0]

    assert isolated_total == pytest.approx(expected, abs=1e-9)


def test_query_tool_costs_top_clamps(tmp_path: Path, pricing_data: dict) -> None:
    conn, session_ids = _build_corpus(tmp_path, pricing_data)
    rows = query_tool_costs(conn, session_ids=session_ids, detail=False, top=2, sort="isolated_cost")
    assert len(rows) <= 2


def test_query_tool_costs_sort_by_invocations(
    tmp_path: Path, pricing_data: dict
) -> None:
    conn, session_ids = _build_corpus(tmp_path, pricing_data)
    rows = query_tool_costs(conn, session_ids=session_ids, detail=False, top=50, sort="invocations")
    invocations = [r.invocations for r in rows]
    assert invocations == sorted(invocations, reverse=True)
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_tools_report.py -v`

Expected: FAIL — `ccforensics.report.tools` does not exist.

- [ ] **Step 3: Implement `tools.py`**

Create `src/ccforensics/report/tools.py`:

```python
"""Per-tool / per-MCP cost report.

Slicing semantics:
- group_key = mcp_server when mcp_server IS NOT NULL else tool_name (default
  view: server-rolled). With detail=True, group_key = tool_name always.
- isolated_turns = distinct messages where this tool/server is the ONLY tool
  emitted in the assistant turn. isolated_cost_usd = SUM(messages.cost_usd)
  over those turns. EXACT.
- shared_turns / shared_exposure_usd: turns emitting this tool alongside
  siblings. shared_exposure_usd is an UPPER BOUND — same turn cost will
  appear under each sibling's row. Documented in footer; never sum across
  rows.

This report does NOT participate in the bucket-attribution invariant.
It's an orthogonal slicing of the same cost data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

SortKey = Literal["isolated_cost", "invocations", "shared_exposure"]

_SORT_TO_SQL = {
    "isolated_cost": "isolated_cost_usd DESC",
    "invocations": "invocations DESC",
    "shared_exposure": "shared_exposure_usd DESC",
}


@dataclass(frozen=True)
class ToolRow:
    group_key: str
    group_kind: str  # 'native' | 'mcp_server' | 'mcp_tool' (when detail=True)
    invocations: int
    isolated_turns: int
    isolated_cost_usd: float
    shared_turns: int
    shared_exposure_usd: float


def query_tool_costs(
    conn: sqlite3.Connection,
    *,
    session_ids: list[str],
    detail: bool,
    top: int,
    sort: SortKey,
) -> list[ToolRow]:
    if not session_ids:
        return []
    if sort not in _SORT_TO_SQL:
        raise ValueError(f"invalid sort key: {sort!r}")

    group_key_expr = (
        "mtu.tool_name" if detail else "COALESCE(mtu.mcp_server, mtu.tool_name)"
    )
    group_kind_expr = (
        # When detail=True, mcp__* rows are 'mcp_tool'; non-mcp are 'native'.
        # When detail=False, mcp__* rows are 'mcp_server'; non-mcp are 'native'.
        "CASE WHEN mtu.mcp_server IS NOT NULL THEN 'mcp_tool' ELSE 'native' END"
        if detail
        else "CASE WHEN mtu.mcp_server IS NOT NULL THEN 'mcp_server' ELSE 'native' END"
    )

    placeholders = ",".join("?" * len(session_ids))
    sql = f"""
WITH per_message_tool_count AS (
  SELECT message_dedup_key, COUNT(*) AS n_tools
  FROM message_tool_uses
  GROUP BY message_dedup_key
),
tool_keys AS (
  SELECT
    mtu.message_dedup_key,
    {group_key_expr} AS group_key,
    {group_kind_expr} AS group_kind
  FROM message_tool_uses mtu
)
SELECT
  tk.group_key,
  tk.group_kind,
  COUNT(*) AS invocations,
  COUNT(DISTINCT CASE WHEN pmtc.n_tools = 1 THEN tk.message_dedup_key END) AS isolated_turns,
  COALESCE(SUM(CASE WHEN pmtc.n_tools = 1 THEN m.cost_usd ELSE 0 END), 0) AS isolated_cost_usd,
  COUNT(DISTINCT CASE WHEN pmtc.n_tools > 1 THEN tk.message_dedup_key END) AS shared_turns,
  COALESCE(SUM(CASE WHEN pmtc.n_tools > 1 THEN m.cost_usd ELSE 0 END), 0) AS shared_exposure_usd
FROM tool_keys tk
JOIN per_message_tool_count pmtc USING (message_dedup_key)
JOIN messages m ON m.dedup_key = tk.message_dedup_key
WHERE m.session_id IN ({placeholders})
GROUP BY tk.group_key, tk.group_kind
ORDER BY {_SORT_TO_SQL[sort]}
LIMIT ?
"""
    rows = conn.execute(sql, (*session_ids, top)).fetchall()
    return [
        ToolRow(
            group_key=r[0],
            group_kind=r[1],
            invocations=r[2],
            isolated_turns=r[3],
            isolated_cost_usd=r[4],
            shared_turns=r[5],
            shared_exposure_usd=r[6],
        )
        for r in rows
    ]


_FOOTER = (
    "Isolated $ is exact. Shared $ is an upper bound — when a turn emits "
    "multiple tools, the same turn cost appears under each sibling. "
    "Do not sum the Shared $ column across rows."
)


def render_text(rows: list[ToolRow]) -> str:
    if not rows:
        return "No tool usage in scope.\n"

    name_w = max(len("TOOL / MCP SERVER"), max(len(_label(r)) for r in rows))
    header = (
        f"{'TOOL / MCP SERVER':<{name_w}}  "
        f"{'INVOCATIONS':>11}  {'ISOLATED TURNS':>14}  "
        f"{'ISOLATED $':>10}  {'SHARED TURNS':>12}  {'SHARED $≤':>9}"
    )
    sep = "─" * len(header)
    lines = [header, sep]
    iso_total = 0
    inv_total = 0
    iso_cost_total = 0.0
    shr_turn_total = 0
    shr_cost_total = 0.0
    for r in rows:
        lines.append(
            f"{_label(r):<{name_w}}  "
            f"{r.invocations:>11,}  {r.isolated_turns:>14,}  "
            f"{r.isolated_cost_usd:>10.2f}  {r.shared_turns:>12,}  "
            f"{r.shared_exposure_usd:>9.2f}"
        )
        iso_total += r.isolated_turns
        inv_total += r.invocations
        iso_cost_total += r.isolated_cost_usd
        shr_turn_total += r.shared_turns
        shr_cost_total += r.shared_exposure_usd
    lines.append(sep)
    lines.append(
        f"{'TOTALS':<{name_w}}  "
        f"{inv_total:>11,}  {iso_total:>14,}  "
        f"{iso_cost_total:>10.2f}  {shr_turn_total:>12,}  "
        f"{shr_cost_total:>9.2f}"
    )
    lines.append("")
    lines.append(_FOOTER)
    return "\n".join(lines) + "\n"


def _label(r: ToolRow) -> str:
    if r.group_kind == "mcp_server":
        return f"mcp__{r.group_key} (server)"
    return r.group_key


def render_json(rows: list[ToolRow]) -> dict:
    return {
        "rows": [
            {
                "group_key": r.group_key,
                "group_kind": r.group_kind,
                "invocations": r.invocations,
                "isolated_turns": r.isolated_turns,
                "isolated_cost_usd": r.isolated_cost_usd,
                "shared_turns": r.shared_turns,
                "shared_exposure_usd": r.shared_exposure_usd,
            }
            for r in rows
        ],
        "_meta": {"footer": _FOOTER},
    }


def render_csv(rows: list[ToolRow]) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "group_key",
            "group_kind",
            "invocations",
            "isolated_turns",
            "isolated_cost_usd",
            "shared_turns",
            "shared_exposure_usd",
        ]
    )
    for r in rows:
        w.writerow(
            [
                r.group_key,
                r.group_kind,
                r.invocations,
                r.isolated_turns,
                f"{r.isolated_cost_usd:.6f}",
                r.shared_turns,
                f"{r.shared_exposure_usd:.6f}",
            ]
        )
    return buf.getvalue()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_tools_report.py -v`

Expected: PASS — all 4 query-layer tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/report/tools.py tests/test_tools_report.py
git commit -m "feat(report): tools report — query layer + text/JSON/CSV renderers

Default render is server-rolled (mcp__*  tools collapsed to one row per
server, native tools as themselves). isolated_cost_usd is exact;
shared_exposure_usd is an upper bound and labeled as such in the footer.
Does not participate in the bucket-attribution invariant."
```

---

## Task 8: `tools` CLI command wire-up

**Files:**
- Modify: `src/ccforensics/cli.py`
- Test: `tests/test_cli.py` (existing — append)

- [ ] **Step 1: Inspect existing CLI patterns**

Run: `grep -nA 30 '@main\.command' src/ccforensics/cli.py | head -80`

Identify how `aggregate` and `plugins` handle: `--days`, `--since`, `--until`, `--json`/`--csv` mutual exclusion, `--no-refresh`, session resolution. Mirror exactly.

- [ ] **Step 2: Write failing CLI test**

Append to `tests/test_cli.py`:

```python
def test_tools_command_default_render(tmp_path: Path, pricing_data: dict) -> None:
    """`ccforensics tools` produces server-rolled output with footer."""
    from ccforensics.cli import main as cli_main
    from click.testing import CliRunner

    # Build a minimal corpus exactly as in test_tools_report.py::_build_corpus.
    # Reuse the helper if it gets promoted to a conftest fixture; otherwise
    # inline the JSONL setup here.
    # ... build fixture ...

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["tools", "--no-refresh"],
        env={"CCFORENSICS_PROJECTS_DIR": str(tmp_path / "projects"),
             "CCFORENSICS_DB": str(tmp_path / "i.sqlite")},
    )
    assert result.exit_code == 0, result.output
    assert "TOOL / MCP SERVER" in result.output
    assert "Isolated $ is exact" in result.output  # footer


def test_tools_command_json_csv_mutually_exclusive(tmp_path: Path) -> None:
    from ccforensics.cli import main as cli_main
    from click.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(cli_main, ["tools", "--json", "--csv"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_tools_command_detail_flag(tmp_path: Path, pricing_data: dict) -> None:
    """--detail expands mcp_server rows into per-tool rows."""
    # Build the same corpus. Without --detail the output should contain
    # 'mcp__stratplaybook (server)'. With --detail it should contain
    # 'mcp__stratplaybook__query' and 'mcp__stratplaybook__build' but NOT
    # the (server) suffix.
    ...
```

> **NOTE:** mirror the env-var / fixture style of existing CLI tests. The CLI's session-scope env vars / config paths may differ from the placeholder names above — check `cli.py` and `paths.py` for the actual env var names; substitute them.

- [ ] **Step 3: Run tests to verify failure**

Run: `uv run pytest tests/test_cli.py -v -k tools`

Expected: FAIL — `tools` is not a registered command.

- [ ] **Step 4: Add the command**

In `src/ccforensics/cli.py`, after the existing `plugins` command:

```python
@main.command()
@click.option("--session", "session_id", default=None, help="Scope to one session (id prefix or path).")
@click.option("--days", type=int, default=None, help="Last N days.")
@click.option("--since", type=str, default=None, help="Lower bound (Nd | YYYY-MM-DD | today | yesterday).")
@click.option("--until", type=str, default=None, help="Upper bound.")
@click.option("--detail", is_flag=True, help="Expand mcp__server rows to per-tool rows.")
@click.option("--top", type=int, default=50, help="Keep top N rows.")
@click.option(
    "--sort",
    type=click.Choice(["isolated_cost", "invocations", "shared_exposure"]),
    default="isolated_cost",
)
@click.option("--json", "as_json", is_flag=True, help="JSON output.")
@click.option("--csv", "as_csv", is_flag=True, help="CSV output.")
@click.option("--no-refresh", is_flag=True, help="Skip the index refresh.")
@click.pass_context
def tools(
    ctx: click.Context,
    session_id: str | None,
    days: int | None,
    since: str | None,
    until: str | None,
    detail: bool,
    top: int,
    sort: str,
    as_json: bool,
    as_csv: bool,
    no_refresh: bool,
) -> None:
    """Per-tool / per-MCP spend (precise isolated cost + upper-bound shared exposure)."""
    if as_json and as_csv:
        raise click.UsageError("--json and --csv are mutually exclusive")

    conn = _open_index()
    if not no_refresh:
        # Match the refresh pattern used by aggregate/plugins.
        # Typically: pricing_data = _load_pricing(); reconcile_projects_dir(conn, _projects_dir(), pricing_data)
        ...

    # Resolve session scope. Mirror aggregate.py's resolution logic exactly.
    session_ids = _resolve_session_scope(
        conn, session_id=session_id, days=days, since=since, until=until
    )

    from ccforensics.report.tools import (
        query_tool_costs,
        render_csv,
        render_json,
        render_text,
    )

    rows = query_tool_costs(
        conn,
        session_ids=session_ids,
        detail=detail,
        top=top,
        sort=sort,  # type: ignore[arg-type]
    )

    if as_json:
        import json as _json

        click.echo(_json.dumps(render_json(rows), indent=2))
    elif as_csv:
        click.echo(render_csv(rows), nl=False)
    else:
        click.echo(render_text(rows), nl=False)
```

> **NOTE for implementer:** the `_resolve_session_scope` helper above is illustrative — use whatever the existing `aggregate` and `plugins` commands use to resolve session sets from `--days/--since/--until/--session`. Copy that exact helper / inline pattern; do not invent a new one.

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run pytest tests/test_cli.py -v`

Expected: PASS — `tools` tests pass; existing CLI tests untouched.

- [ ] **Step 6: Commit**

```bash
git add src/ccforensics/cli.py tests/test_cli.py
git commit -m "feat(cli): add 'tools' command for per-tool / per-MCP spend

Mirrors the option surface of 'aggregate' and 'plugins' (--days, --since,
--until, --session, --json, --csv, --no-refresh), plus tools-specific
flags (--detail to expand mcp_server rows, --top to clamp, --sort)."
```

---

## Task 9: Service-tier breakdown footer in `session show` and `aggregate`

**Files:**
- Modify: `src/ccforensics/report/session.py`
- Modify: `src/ccforensics/report/aggregate.py`
- Modify: `tests/test_report_session.py` (append)
- Modify: `tests/test_aggregate_and_plugins.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_report_session.py`:

```python
def test_session_show_omits_tier_line_when_only_standard(
    tmp_path: Path, pricing_data: dict
) -> None:
    """If every message in a session is service_tier='standard', no breakdown
    line is rendered (avoid noise on the 99.9% case)."""
    # Build a one-message session with service_tier='standard'. Invoke
    # 'session show'. Assert 'Service tiers' NOT in output.
    ...


def test_session_show_renders_tier_line_when_mixed(
    tmp_path: Path, pricing_data: dict
) -> None:
    """When any non-standard tier is present, render breakdown."""
    # Build a session with one 'standard' message and one 'priority' message.
    # Invoke. Assert 'Service tiers:' IS in output, with both counts.
    # Assert the parenthetical 'pricing not yet applied' is present.
    ...


def test_session_show_json_includes_tier_breakdown(
    tmp_path: Path, pricing_data: dict
) -> None:
    """JSON output ALWAYS includes service_tier_breakdown dict (empty if none)."""
    ...
```

Mirror in `tests/test_aggregate_and_plugins.py`.

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_report_session.py tests/test_aggregate_and_plugins.py -v -k tier`

Expected: FAIL.

- [ ] **Step 3: Add tier-breakdown rendering**

In both `report/session.py` and `report/aggregate.py`, in the same totals block where cache-eff was added:

```python
tier_rows = conn.execute(
    """SELECT COALESCE(service_tier, 'unknown') AS tier, COUNT(*)
       FROM messages WHERE session_id IN ({}) AND role='assistant'
       GROUP BY tier
       ORDER BY tier""".format(",".join("?" * len(session_ids))),
    tuple(session_ids),
).fetchall()
breakdown = {tier: count for tier, count in tier_rows}

# Text rendering: only show line if non-standard tier present.
non_standard_present = any(t not in ("standard", "unknown") for t in breakdown)
if non_standard_present:
    parts = [f"{t} {c:,} msgs" for t, c in sorted(breakdown.items())]
    out_lines.append(
        f"Service tiers: {' · '.join(parts)}  (non-standard pricing not yet applied)"
    )

# JSON: always include the dict (empty if no assistant messages).
output_dict["service_tier_breakdown"] = breakdown
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_report_session.py tests/test_aggregate_and_plugins.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/report/session.py src/ccforensics/report/aggregate.py \
        tests/test_report_session.py tests/test_aggregate_and_plugins.py
git commit -m "feat(report): service_tier breakdown on session show + aggregate

Text mode: line rendered only when a non-standard tier is present (avoids
noise on the 99.9% standard case). JSON: service_tier_breakdown dict
always included. Pricing branch deferred per spec."
```

---

## Task 10: Bucket-invariant regression guard for v3

**Files:**
- Modify: `tests/test_attribution.py` (append one test)

- [ ] **Step 1: Write the test**

Append to `tests/test_attribution.py`:

```python
def test_invariant_holds_after_v3_migration(tmp_path: Path, pricing_data: dict) -> None:
    """The bucket-attribution invariant ``Σ session_rollups == Σ messages``
    must survive the v3 migration (new table, new column, cold backfill)."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-inv"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "go", cwd="/home/test"),
            _assistant(
                "u2", sid, "2026-04-22T10:00:05Z",
                msg_id="m1", req_id="r1",
                content=[
                    {"type": "tool_use", "id": "tu1", "name": "Edit",
                     "input": {"x": 1}},
                    {"type": "tool_use", "id": "tu2", "name": "Read",
                     "input": {"y": 2}},
                ],
                input_tokens=100, output_tokens=50,
            ),
        ],
    )
    db = tmp_path / "i.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    violators = find_invariant_violators(conn, tolerance=1e-6)
    assert violators == [], f"invariant violated for sessions: {violators}"
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/test_attribution.py::test_invariant_holds_after_v3_migration -v`

Expected: PASS (the v3 changes don't touch attribution; this is a regression guard).

- [ ] **Step 3: Commit**

```bash
git add tests/test_attribution.py
git commit -m "test(attribution): regression guard — invariant survives v3 migration

Verifies that adding messages.service_tier and the message_tool_uses
table does not perturb the Σ rollups == Σ messages invariant. Multi-tool
turn included to exercise the new writer code path."
```

---

## Task 11: Redaction script + real fixture refresh

**Files:**
- Modify: `scripts/redact_jsonl.py`
- Modify: `tests/fixtures/real/redacted_session.jsonl` (regenerated)

- [ ] **Step 1: Inspect the redaction script**

Run: `cat scripts/redact_jsonl.py | head -80`

Identify the allow-list / pass-through field set for the usage block.

- [ ] **Step 2: Add `service_tier` to the usage allow-list**

Locate where the script copies fields from `usage` (likely something like `for field in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):`). Add `"service_tier"` to that tuple/list.

If the script copies the entire usage dict verbatim, no change is needed — verify by reading the script.

- [ ] **Step 3: Regenerate the redacted fixture**

The fixture's source is whatever real session was originally redacted. Re-run the script against the same source if available; otherwise, leave the existing fixture in place and add `service_tier: 'standard'` to its usage blocks via a small one-off edit.

Practical approach for the implementer if the source isn't accessible:

```bash
uv run python -c "
import json
from pathlib import Path

p = Path('tests/fixtures/real/redacted_session.jsonl')
out = []
for line in p.read_text().splitlines():
    if not line.strip():
        out.append(line)
        continue
    e = json.loads(line)
    msg = e.get('message') or {}
    usage = msg.get('usage') if isinstance(msg, dict) else None
    if isinstance(usage, dict) and 'service_tier' not in usage:
        usage['service_tier'] = 'standard'
    out.append(json.dumps(e))
Path('tests/fixtures/real/redacted_session.jsonl').write_text('\n'.join(out) + '\n')
"
```

- [ ] **Step 4: Run real-ingest tests**

Run: `uv run pytest tests/test_real_ingest.py -v`

Expected: PASS — fixture updated, ingestion unchanged.

- [ ] **Step 5: Commit**

```bash
git add scripts/redact_jsonl.py tests/fixtures/real/redacted_session.jsonl
git commit -m "test(fixtures): redacted_session carries service_tier

Adds service_tier to the redaction allow-list and backfills the existing
redacted fixture so v3 ingestion populates messages.service_tier from
real-shape data."
```

---

## Task 12: README + CHANGELOG

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Inspect existing structure**

Run: `head -80 README.md && echo '---' && head -40 CHANGELOG.md`

- [ ] **Step 2: Add `tools` example to README**

Find the section that lists CLI commands (likely under "Usage" or similar). Add an example invocation:

````markdown
### Per-tool / per-MCP spend

```bash
ccforensics tools --days 30
```

Default render is server-rolled — every `mcp__<server>__*` tool collapses
to a single row per server. Add `--detail` to expand to per-tool rows.

`Isolated $` is exact (turns where this tool was the only one emitted).
`Shared $≤` is an upper bound (turns where this tool ran alongside others;
the same turn's cost appears under each sibling — never sum across rows).
````

Find or add a `session show` sample output and append the cache-eff line:

```
Cache: 12.4M read · 1.2M created · 87.3% efficiency · saved $3.42
```

If a "What's not supported yet" or "Roadmap" section exists, add:

> **Fast-mode pricing.** `service_tier` is captured from every message;
> when fast-mode usage shows up in the data, the breakdown line surfaces
> it. Pricing branch is deferred until the signal can be verified
> end-to-end against a real fast-mode session.

- [ ] **Step 3: Add CHANGELOG entry**

In `CHANGELOG.md`, under the `[Unreleased]` heading (create the heading if absent — match the existing style):

```markdown
## [Unreleased]

### Added
- `tools` command — per-tool / per-MCP-server spend with honest
  isolated/shared accounting. Use `--detail` to drill into individual
  MCP tools.
- Cache efficiency + cache savings on `session show` and `aggregate`
  (cost-weighted ratio; both metrics are exact arithmetic over stored
  values).
- `service_tier` capture on every message (precursor to fast-mode
  pricing; pricing branch deferred).

### Changed
- Schema migrated from v2 to v3 (adds `messages.service_tier` and a new
  `message_tool_uses` table). First command after upgrade triggers a
  one-shot full re-reconcile to cold-backfill the new table.
```

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: tools command, cache efficiency, service_tier in README + CHANGELOG"
```

---

## Task 13: Full CI gate locally

**Files:** none — verification only.

- [ ] **Step 1: Run the full CI command set**

```bash
uv run ruff check
uv run ruff format --check
uv run mypy src/
uv run pytest --cov=ccforensics --cov-report=term-missing --cov-fail-under=85
```

Expected: every command exits 0. Coverage gate passes.

- [ ] **Step 2: Fix any issues inline**

Common issues:
- Ruff: import sorting / line length. Fix with `uv run ruff format` (NOT `--check`) and re-run `ruff check --fix`.
- Mypy strict: missing type annotations on the new helpers. Add explicit `-> None` / `-> list[ToolRow]` etc.
- Coverage: missing branches in `tools.py` render functions. Add a test for the empty-rows case (covered by `render_text` "No tool usage in scope.").

- [ ] **Step 3: Commit any fixes**

```bash
git add -p   # stage only the lint/type fixes
git commit -m "chore: lint + type fixes for v3 work"
```

- [ ] **Step 4: Final verification — open PR**

```bash
git push -u origin feature/cache-tools-tier
gh pr create --title "feat: cache efficiency, per-tool/per-MCP report, service-tier capture" --body "$(cat <<'EOF'
## Summary
- Cache efficiency + savings columns in `session show` and `aggregate` (cost-weighted, exact)
- New `tools` command for per-tool / per-MCP-server spend with honest isolated / shared exposure framing
- `service_tier` capture on every message (precursor to deferred fast-mode pricing)
- Schema v2 → v3 with cold-backfill on first command after upgrade

Spec: `docs/specs/2026-05-02-cache-tools-tier-design.md`
Plan: `docs/plans/2026-05-02-cache-tools-tier.md`

## Test plan
- [x] Full CI gate passes locally (ruff, format, mypy, pytest with ≥85% coverage)
- [ ] CI green on Ubuntu + macOS (Python 3.13)
- [ ] Manual: run `ccforensics tools --days 30` against real `~/.claude/projects` and sanity-check output
- [ ] Manual: run `ccforensics session show <id>` and confirm Cache: line appears
- [ ] Manual: confirm first run after upgrade triggers expected re-reconcile (mtime reset)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review summary

**Spec coverage:**
- §1 Architecture → Tasks 1, 3, 4, 7, 8 (all touched modules covered)
- §2 Data model (schema v3) → Task 1
- §3 Reconcile pipeline → Tasks 2, 3
- §4 Cache efficiency → Tasks 4, 5, 6
- §5 Per-tool / per-MCP report → Tasks 7, 8
- §6 Service-tier capture → Tasks 2, 3, 9 (capture in models + writer; surface in reports)
- §7 Testing + migration + rollout → Tasks 10, 11, 12, 13

**Placeholders:** None for code or commands. The few `...` markers in test scaffolding (Tasks 5, 8, 9) are explicitly flagged with implementer notes pointing to the existing CLI test pattern to copy — this is intentional because the existing test invocation style (env vars vs. fixtures vs. `monkeypatch`) is something the implementer must mirror exactly, not invent. Each note specifies the assertions to add.

**Type/name consistency:** `ToolRow`, `CacheRow`, `CacheMetrics`, `query_tool_costs`, `cache_metrics` are used consistently across tests and implementations. `render_text` / `render_json` / `render_csv` paired in tools.py. The spec uses `mcp_server` as the column name — matched in schema, dataclass, query, and renderer.
