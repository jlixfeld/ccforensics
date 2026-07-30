# Dynamic-workflow attribution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute Claude Code dynamic-workflow agents (`subagents/workflows/wf_<id>/agent-<hex>.jsonl`) to a first-class `workflow:<name>` bucket tied to the orchestrating session, instead of misclassifying them as phantom `main` sessions.

**Architecture:** Reuse the existing `subagent_spawns` plumbing. Detect workflow agents by **path** (`*/subagents/workflows/wf_<id>/`), classify them as `subagent` kind with the orchestrator session id, set `subagent_type = "workflow:<name>"` via a `Workflow`-tool_use-aware `discover_spawn`, and render `workflow:<name>` as its own bucket kind in `attribution.py`. A v5→v6 migration purges already-written phantom sessions and cold-reconciles.

**Tech Stack:** Python 3.13, SQLite (`PRAGMA user_version` migrations), pytest, `uv`. Spec: `docs/superpowers/specs/2026-06-08-workflow-attribution-design.md`.

**After every `uv sync`:** run `chflags nohidden .venv/lib/python3.*/site-packages/*.pth` (iCloud workaround — see CLAUDE.md).

---

## File structure

| File | Responsibility | Change |
|------|----------------|--------|
| `src/ccforensics/tree.py` | spawn discovery | add `_workflow_name`, `_iter_workflow_tool_uses`, `is_workflow` branch in `discover_spawn` |
| `src/ccforensics/index.py` | classify + reconcile + migrations | `_WORKFLOW_AGENT_RE`, workflow branch in `_classify_file` + `_parent_session_path`, skip `journal.jsonl`, pass `is_workflow`, v6 migration |
| `src/ccforensics/attribution.py` | bucket SQL | `BucketKind.WORKFLOW` + two CASE branches |
| `tests/test_tree.py` | unit | `_workflow_name`, workflow `discover_spawn` |
| `tests/test_index.py` | unit | `_classify_file`, `_parent_session_path`, v6 migration purge |
| `tests/test_attribution.py` | integration | end-to-end workflow bucket, journal skip, no phantom, invariant |
| `CLAUDE.md` | docs | schema v6 section |

> **Note on fixtures:** `scripts/redact_jsonl.py` blanks `tool_use.input` (`"input": {}`), which would destroy the `Workflow` call's `script`/`name`. Do **not** redact a real workflow transcript. All tests below build **synthetic** JSONL inline via the existing `_write_jsonl` / `_user` / `_assistant` helpers in `tests/test_attribution.py`.

---

### Task 1: Schema v6 migration — purge phantom sessions + cold reconcile

**Files:**
- Modify: `src/ccforensics/index.py:67` (`CURRENT_SCHEMA_VERSION`) and `src/ccforensics/index.py:278` (append to `MIGRATIONS`)
- Test: `tests/test_index.py`

Phantom rows from the old misclassification have `session_id` like `agent-<hex>` or `journal` (set by `_classify_file`'s `main` fallback, `session_id = path.stem`). `messages.file_path` is `ON DELETE CASCADE`, so deleting the workflow `files` rows cascades their messages; the other per-session tables are not FK'd to `files` and need explicit deletes.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_index.py` (create the file if absent, with the header below):

```python
from __future__ import annotations

from pathlib import Path

from ccforensics.index import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    ensure_schema,
    open_connection,
)


def _apply_migrations_through(conn, target_version: int) -> None:
    """Bring a fresh DB up to exactly ``target_version`` (0-indexed migrations)."""
    for target in range(target_version):
        for ddl in MIGRATIONS[target]:
            conn.execute(ddl)
        conn.execute(f"PRAGMA user_version = {target + 1}")
    conn.commit()


def test_v6_migration_purges_phantom_workflow_sessions(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    # Build a pre-v6 (v5) database.
    _apply_migrations_through(conn, 5)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5

    # Seed phantom rows exactly as the old misclassification wrote them.
    conn.execute(
        "INSERT INTO files (path, mtime_ns, size, session_id, kind, agent_id, "
        "schema_version, parse_warnings, last_parsed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "/p/-enc/SESS/subagents/workflows/wf_z/agent-dead.jsonl",
            123, 456, "agent-dead", "main", None, "5", 0, 0,
        ),
    )
    conn.execute(
        "INSERT INTO messages (dedup_key, file_path, session_id, role, type, ts) "
        "VALUES (?,?,?,?,?,?)",
        (
            "req:m1:r1",
            "/p/-enc/SESS/subagents/workflows/wf_z/agent-dead.jsonl",
            "agent-dead", "assistant", "assistant", 0,
        ),
    )
    conn.execute(
        "INSERT INTO session_rollups (session_id, bucket_kind, bucket_name, cost_usd, "
        "input_tokens, output_tokens, cache_create, cache_read) VALUES (?,?,?,?,?,?,?,?)",
        ("agent-dead", "main", "main", 1.0, 0, 0, 0, 0),
    )
    conn.execute(
        "INSERT INTO session_rollups (session_id, bucket_kind, bucket_name, cost_usd, "
        "input_tokens, output_tokens, cache_create, cache_read) VALUES (?,?,?,?,?,?,?,?)",
        ("journal", "main", "main", 0.0, 0, 0, 0, 0),
    )
    conn.commit()

    # Apply the v6 migration.
    ensure_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert CURRENT_SCHEMA_VERSION == 6
    # Phantom rollups gone.
    assert conn.execute(
        "SELECT COUNT(*) FROM session_rollups WHERE session_id LIKE 'agent-%' OR session_id='journal'"
    ).fetchone()[0] == 0
    # Workflow files row gone (and its message cascaded away).
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE path LIKE '%/subagents/workflows/%'"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='agent-dead'").fetchone()[0] == 0
    # Cold reconcile armed.
    # (Any surviving files rows have mtime_ns reset; none here, so just assert no error.)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_index.py::test_v6_migration_purges_phantom_workflow_sessions -v`
Expected: FAIL — `IndexError` on `MIGRATIONS[5]` / `CURRENT_SCHEMA_VERSION == 6` assert (still 5).

- [ ] **Step 3: Bump version and append the migration**

In `src/ccforensics/index.py`, change line 67:

```python
CURRENT_SCHEMA_VERSION = 6
```

Append a new element to the `MIGRATIONS` list (after the v5 block's closing `]` at line 277, before the list's closing `]` at line 278):

```python
    # v5 → v6: dynamic-workflow attribution. Workflow-tool agents live at
    # ``<enc>/<sess>/subagents/workflows/wf_<id>/agent-<hex>.jsonl`` — two
    # levels below ``subagents/`` — so the pre-v6 ``_classify_file`` mislabeled
    # them as ``main`` sessions named ``agent-<hex>`` (and ``journal``). Purge
    # those phantom rows, drop the workflow files rows (CASCADE removes their
    # messages), then cold-reconcile so the files re-classify into the new
    # ``workflow:<name>`` bucket.
    [
        "DELETE FROM session_rollups WHERE session_id LIKE 'agent-%' OR session_id = 'journal'",
        "DELETE FROM session_summaries WHERE session_id LIKE 'agent-%' OR session_id = 'journal'",
        "DELETE FROM session_signals WHERE session_id LIKE 'agent-%' OR session_id = 'journal'",
        "DELETE FROM skill_activations WHERE session_id LIKE 'agent-%' OR session_id = 'journal'",
        "DELETE FROM messages WHERE session_id LIKE 'agent-%' OR session_id = 'journal'",
        "DELETE FROM files WHERE path LIKE '%/subagents/workflows/%'",
        "UPDATE files SET mtime_ns = 0",
    ],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_index.py::test_v6_migration_purges_phantom_workflow_sessions -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/index.py tests/test_index.py
git commit -m "feat: schema v6 — purge phantom workflow sessions + cold reconcile"
```

---

### Task 2: `_workflow_name` — extract the workflow name from a `Workflow` tool_use input

**Files:**
- Modify: `src/ccforensics/tree.py` (imports + new helper)
- Test: `tests/test_tree.py`

Input shapes (per the Workflow tool contract): inline `{script}` (regex `meta.name` — `meta` is a pure literal), saved `{name}`, `{scriptPath}` (filename stem, strip trailing `-wf_<id>`). Fallback to `None` (caller substitutes the `wf_<id>` dir name).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tree.py` (create if absent):

```python
from __future__ import annotations

from ccforensics.tree import _workflow_name


def test_workflow_name_from_saved_name() -> None:
    assert _workflow_name({"name": "review-pr"}) == "review-pr"


def test_workflow_name_from_script_path_strips_wf_suffix() -> None:
    assert _workflow_name({"scriptPath": "/a/b/sdk-drift-audit-wf_2328ca35-f9d.js"}) == "sdk-drift-audit"


def test_workflow_name_from_inline_script_meta() -> None:
    script = "export const meta = {\n  name: 'find-flaky',\n  description: 'x',\n}"
    assert _workflow_name({"script": script}) == "find-flaky"


def test_workflow_name_prefers_name_over_script() -> None:
    assert _workflow_name({"name": "saved", "script": "name: 'inline'"}) == "saved"


def test_workflow_name_none_when_unparseable() -> None:
    assert _workflow_name({"script": "no meta here"}) is None
    assert _workflow_name({}) is None
    assert _workflow_name("not a dict") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tree.py -k workflow_name -v`
Expected: FAIL — `ImportError: cannot import name '_workflow_name'`.

- [ ] **Step 3: Implement the helper**

In `src/ccforensics/tree.py`, add `import re` to the import block (after `import logging` on line 3):

```python
import logging
import re
```

Then add, after the `logger = logging.getLogger("ccforensics.tree")` line:

```python
_WORKFLOW_NAME_RE = re.compile(r"name:\s*['\"]([^'\"]+)['\"]")
_WORKFLOW_SCRIPTPATH_SUFFIX_RE = re.compile(r"-wf_[0-9a-z-]+$", re.IGNORECASE)


def _workflow_name(inp: object) -> str | None:
    """Best-effort workflow name from a ``Workflow`` tool_use input.

    Priority: saved-workflow ``name`` → ``scriptPath`` filename stem (minus
    the trailing ``-wf_<id>``) → inline ``script`` ``meta.name`` regex. Returns
    ``None`` if all fail; the caller substitutes the ``wf_<id>`` directory name.
    """
    if not isinstance(inp, dict):
        return None
    name = inp.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    script_path = inp.get("scriptPath")
    if isinstance(script_path, str) and script_path.strip():
        stem = _WORKFLOW_SCRIPTPATH_SUFFIX_RE.sub("", Path(script_path).stem)
        if stem:
            return stem
    script = inp.get("script")
    if isinstance(script, str):
        m = _WORKFLOW_NAME_RE.search(script)
        if m:
            return m.group(1)
    return None
```

(`Path` is already imported in `tree.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tree.py -k workflow_name -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/tree.py tests/test_tree.py
git commit -m "feat: _workflow_name helper for Workflow tool_use inputs"
```

---

### Task 3: `discover_spawn` — link workflow agents to the `Workflow` tool_use

**Files:**
- Modify: `src/ccforensics/tree.py:140-215` (`_iter_workflow_tool_uses` + `is_workflow` branch in `discover_spawn`)
- Test: `tests/test_tree.py`

`discover_spawn` gains an `is_workflow: bool = False` keyword. When set, it matches `Workflow` tool_uses (nearest-before, no type-match dimension) and sets `subagent_type = "workflow:<name>"`, **ignoring** per-agent `meta.agent_type` (unreliable — it can be `Explore`, `workflow-subagent`, etc.).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tree.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

from ccforensics.models import SpawnMeta, parse_entry
from ccforensics.tree import discover_spawn


def _entry(d: dict):
    return parse_entry(d)


def _wf_parent_entries():
    return [
        _entry({
            "type": "assistant",
            "uuid": "p1",
            "sessionId": "SESS",
            "timestamp": "2026-06-08T10:00:00Z",
            "requestId": "r1",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{
                    "type": "tool_use",
                    "id": "tu-wf",
                    "name": "Workflow",
                    "input": {"script": "export const meta = { name: 'sdk-drift-audit' }"},
                }],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }),
    ]


def _wf_child_entries():
    return [
        _entry({
            "type": "assistant",
            "uuid": "c1",
            "sessionId": "SESS",
            "agentId": "dead",
            "timestamp": "2026-06-08T10:00:30Z",
            "isSidechain": True,
            "requestId": "r2",
            "message": {
                "id": "m2",
                "role": "assistant",
                "model": "claude-haiku-4-5-20251001",
                "content": [{"type": "text", "text": "x"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }),
    ]


def test_discover_spawn_workflow_links_and_names() -> None:
    child_path = Path("/p/-enc/SESS/subagents/workflows/wf_2328ca35-f9d/agent-dead.jsonl")
    # meta.agentType is the per-agent type and MUST be ignored for the bucket name.
    meta = SpawnMeta(agentType="Explore")
    spawn = discover_spawn(
        parent_session_id="SESS",
        child_agent_id="dead",
        child_file_path=child_path,
        child_entries=_wf_child_entries(),
        parent_entries=_wf_parent_entries(),
        meta=meta,
        is_workflow=True,
    )
    assert spawn is not None
    assert spawn.subagent_type == "workflow:sdk-drift-audit"
    assert spawn.parent_tool_use_id == "tu-wf"
    assert spawn.parent_message_uuid == "p1"
    assert spawn.model_hint == "claude-haiku-4-5-20251001"


def test_discover_spawn_workflow_falls_back_to_wf_id() -> None:
    child_path = Path("/p/-enc/SESS/subagents/workflows/wf_abc123/agent-dead.jsonl")
    parent = _wf_parent_entries()
    # Wipe the script so no name is extractable.
    parent[0].message.content[0].input = {}
    spawn = discover_spawn(
        parent_session_id="SESS",
        child_agent_id="dead",
        child_file_path=child_path,
        child_entries=_wf_child_entries(),
        parent_entries=parent,
        meta=None,
        is_workflow=True,
    )
    assert spawn is not None
    assert spawn.subagent_type == "workflow:wf_abc123"


def test_discover_spawn_workflow_unresolvable_parent() -> None:
    """No Workflow tool_use in parent → no parent linkage (routes to unattributed)."""
    child_path = Path("/p/-enc/SESS/subagents/workflows/wf_abc123/agent-dead.jsonl")
    spawn = discover_spawn(
        parent_session_id="SESS",
        child_agent_id="dead",
        child_file_path=child_path,
        child_entries=_wf_child_entries(),
        parent_entries=[],
        meta=None,
        is_workflow=True,
    )
    assert spawn is not None
    assert spawn.parent_message_uuid is None
    assert spawn.subagent_type == "workflow:wf_abc123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tree.py -k discover_spawn_workflow -v`
Expected: FAIL — `discover_spawn() got an unexpected keyword argument 'is_workflow'`.

- [ ] **Step 3: Implement `_iter_workflow_tool_uses` + the `is_workflow` branch**

In `src/ccforensics/tree.py`, add after `_iter_agent_tool_uses` (ends line 161):

```python
def _iter_workflow_tool_uses(
    parent_entries: Iterable[TranscriptEntry],
    before: datetime,
) -> Iterable[tuple[datetime, str, str, str | None]]:
    """Yield ``(ts, emitter_uuid, tool_use_id, workflow_name)`` for every
    ``Workflow`` tool_use emitted before ``before``. ``workflow_name`` is
    ``None`` when it can't be extracted from the call input."""
    for entry in parent_entries:
        if entry.timestamp > before:
            continue
        if entry.uuid is None or entry.message is None:
            continue
        for block in entry.message.content or []:
            if block.type != "tool_use" or block.name != "Workflow":
                continue
            if not block.id:
                continue
            yield entry.timestamp, entry.uuid, block.id, _workflow_name(block.input)
```

Then replace the body of `discover_spawn` (lines 175-215) — keep the signature line but add the new keyword. The full replacement function:

```python
def discover_spawn(
    *,
    parent_session_id: str,
    child_agent_id: str,
    child_file_path: Path,
    child_entries: Iterable[TranscriptEntry],
    parent_entries: Iterable[TranscriptEntry],
    meta: SpawnMeta | None,
    is_workflow: bool = False,
) -> Spawn | None:
    """Link a subagent file to its parent Agent/Task call (or, when
    ``is_workflow``, its parent ``Workflow`` call). Rank key for Agent/Task is
    ``(type_match, timestamp)``; for workflows it is ``timestamp`` alone
    (nearest-before)."""
    child_list = list(child_entries)
    if not child_list:
        return None

    ts_spawned = min(e.timestamp for e in child_list)

    parent_uuid: str | None = None
    parent_tu_id: str | None = None

    if is_workflow:
        wf_id = Path(child_file_path).parent.name
        candidates = list(_iter_workflow_tool_uses(parent_entries, before=ts_spawned))
        name: str | None = None
        if candidates:
            _, parent_uuid, parent_tu_id, name = max(candidates, key=lambda c: c[0])
        subagent_type: str | None = "workflow:" + (name or wf_id)
        description = meta.description if meta else None
    else:
        wanted = meta.agent_type if meta else None
        candidates = list(_iter_agent_tool_uses(parent_entries, before=ts_spawned))
        parent_subtype: str | None = None
        if candidates:
            _, parent_uuid, parent_tu_id, parent_subtype = max(
                candidates,
                key=lambda c: ((c[3] == wanted) if wanted else False, c[0]),
            )
        subagent_type = (meta.agent_type if meta else None) or parent_subtype
        description = meta.description if meta else None

    model_hint: str | None = None
    for entry in sorted(child_list, key=lambda e: e.timestamp):
        if entry.type == "assistant" and entry.message and entry.message.model:
            model_hint = entry.message.model
            break

    return Spawn(
        parent_session_id=parent_session_id,
        child_agent_id=child_agent_id,
        child_file_path=str(child_file_path),
        subagent_type=subagent_type,
        description=description,
        ts_spawned=ts_spawned,
        parent_message_uuid=parent_uuid,
        parent_tool_use_id=parent_tu_id,
        model_hint=model_hint,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tree.py -k discover_spawn_workflow -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the full tree test file (regression on Agent/Task path)**

Run: `uv run pytest tests/test_tree.py -v`
Expected: PASS (existing Agent/Task spawn tests still green).

- [ ] **Step 6: Commit**

```bash
git add src/ccforensics/tree.py tests/test_tree.py
git commit -m "feat: discover_spawn links workflow agents to Workflow tool_use"
```

---

### Task 4: `_classify_file` — recognize the workflow path

**Files:**
- Modify: `src/ccforensics/index.py:28` (new regex) and `src/ccforensics/index.py:303-333` (`_classify_file`)
- Test: `tests/test_index.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_index.py`:

```python
from ccforensics.index import _classify_file


def test_classify_workflow_agent_path() -> None:
    p = Path("/p/-enc/SESS-UUID/subagents/workflows/wf_2328ca35-f9d/agent-deadbeef.jsonl")
    kind, agent_id, session_id = _classify_file(p)
    assert kind == "subagent"
    assert agent_id == "deadbeef"
    assert session_id == "SESS-UUID"


def test_classify_direct_subagent_still_works() -> None:
    p = Path("/p/-enc/SESS-UUID/subagents/agent-abc.jsonl")
    assert _classify_file(p) == ("subagent", "abc", "SESS-UUID")


def test_classify_autocompact_still_works() -> None:
    p = Path("/p/-enc/SESS-UUID/subagents/agent-acompact-fff.jsonl")
    assert _classify_file(p) == ("auto-compact", "acompact-fff", "SESS-UUID")


def test_classify_main_still_works() -> None:
    p = Path("/p/-enc/SESS-UUID.jsonl")
    assert _classify_file(p) == ("main", None, "SESS-UUID")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_index.py -k classify -v`
Expected: FAIL — `test_classify_workflow_agent_path` returns `("main", None, "agent-deadbeef")`.

- [ ] **Step 3: Add the regex and the workflow branch**

In `src/ccforensics/index.py`, after line 28 (`_SUBAGENT_FILENAME = ...`):

```python
_WORKFLOW_AGENT_RE = re.compile(
    r"subagents/workflows/wf_[^/]+/agent-([0-9a-f]+)\.jsonl$", re.IGNORECASE
)
```

In `_classify_file`, add a branch at the very top of the function body (before `name = path.name` on line 319):

```python
    wf = _WORKFLOW_AGENT_RE.search(path.as_posix())
    if wf:
        # <enc>/<session>/subagents/workflows/wf_<id>/agent-<hex>.jsonl
        # parents: [0]=wf_<id>, [1]=workflows, [2]=subagents, [3]=<session>
        return ("subagent", wf.group(1), path.parents[3].name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_index.py -k classify -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/index.py tests/test_index.py
git commit -m "feat: _classify_file recognizes workflow agent paths"
```

---

### Task 5: `_parent_session_path` — resolve the orchestrator file for workflow agents

**Files:**
- Modify: `src/ccforensics/index.py:336-339` (`_parent_session_path`)
- Test: `tests/test_index.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_index.py`:

```python
from ccforensics.index import _parent_session_path


def test_parent_session_path_workflow() -> None:
    child = Path("/p/-enc/SESS-UUID/subagents/workflows/wf_z/agent-dead.jsonl")
    assert _parent_session_path(child) == Path("/p/-enc/SESS-UUID.jsonl")


def test_parent_session_path_direct_subagent() -> None:
    child = Path("/p/-enc/SESS-UUID/subagents/agent-abc.jsonl")
    assert _parent_session_path(child) == Path("/p/-enc/SESS-UUID.jsonl")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_index.py -k parent_session_path -v`
Expected: FAIL — workflow case returns `/p/-enc/SESS-UUID/subagents/workflows.jsonl`.

- [ ] **Step 3: Add the workflow branch**

Replace `_parent_session_path` (lines 336-339) with:

```python
def _parent_session_path(subagent_path: Path) -> Path:
    """Subagent file → its orchestrator ``<enc>/<sess>.jsonl``.

    Direct subagents nest one level (``<sess>/subagents/``); workflow agents
    nest three (``<sess>/subagents/workflows/wf_<id>/``)."""
    if _WORKFLOW_AGENT_RE.search(subagent_path.as_posix()):
        session_dir = subagent_path.parents[3]
    else:
        session_dir = subagent_path.parent.parent
    return session_dir.parent / f"{session_dir.name}.jsonl"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_index.py -k parent_session_path -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/index.py tests/test_index.py
git commit -m "feat: _parent_session_path resolves orchestrator for workflow agents"
```

---

### Task 6: Reconcile walk — skip `journal.jsonl`, pass `is_workflow`

**Files:**
- Modify: `src/ccforensics/index.py:842-851` (walk loop) and `src/ccforensics/index.py:397-404` (`_reconcile_spawn` → `discover_spawn` call)
- Test: covered by Task 8 integration (no isolated unit — these are wiring changes verified end-to-end). Add the targeted journal-skip assertion below to Task 8.

- [ ] **Step 1: Skip `journal.jsonl` in the walk**

In `reconcile_projects_dir`, add immediately after `for path in sorted(projects_dir.rglob("*.jsonl"), key=str):` (line 842) and before `stats.files_scanned += 1`:

```python
        if path.name == "journal.jsonl":
            # Workflow orchestration log — not a transcript, carries no billable
            # usage. Skipping avoids a phantom 'journal' session.
            continue
```

- [ ] **Step 2: Pass `is_workflow` into `discover_spawn`**

In `_reconcile_spawn`, change the `discover_spawn(...)` call (lines 397-404) to pass the flag. Replace the call with:

```python
    spawn = discover_spawn(
        parent_session_id=session_id,
        child_agent_id=agent_id,
        child_file_path=subagent_path,
        child_entries=child_entries,
        parent_entries=parent_entries,
        meta=meta,
        is_workflow=bool(_WORKFLOW_AGENT_RE.search(subagent_path.as_posix())),
    )
```

- [ ] **Step 3: Run the existing suite to confirm no regression**

Run: `uv run pytest tests/test_attribution.py tests/test_index.py -v`
Expected: PASS (existing tests unaffected; full workflow assertion lands in Task 8).

- [ ] **Step 4: Commit**

```bash
git add src/ccforensics/index.py
git commit -m "feat: reconcile skips journal.jsonl and flags workflow spawns"
```

---

### Task 7: Attribution — first-class `workflow:<name>` bucket

**Files:**
- Modify: `src/ccforensics/attribution.py:12-41` (`BucketKind` + both CASE expressions)
- Test: `tests/test_attribution.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_attribution.py` (uses the existing `_write_jsonl` / `_assistant` / `_user` helpers and `pricing_data` fixture):

```python
def test_workflow_bucket_first_class(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-wf"
    # Orchestrator session: a Workflow tool_use on an assistant message.
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-06-08T10:00:00Z", "go", cwd="/home/test"),
            _assistant(
                "u2", sid, "2026-06-08T10:00:10Z", msg_id="m1", req_id="r1",
                content=[{
                    "type": "tool_use", "id": "tu-wf", "name": "Workflow",
                    "input": {"script": "export const meta = { name: 'sdk-drift-audit' }"},
                }],
            ),
        ],
    )
    wf_dir = enc / sid / "subagents" / "workflows" / "wf_2328ca35-f9d"
    wf_dir.mkdir(parents=True)
    _write_jsonl(
        wf_dir / "agent-dead.jsonl",
        [
            _assistant(
                "c1", sid, "2026-06-08T10:00:20Z", msg_id="m2", req_id="r2",
                model="claude-haiku-4-5-20251001", agentId="dead", isSidechain=True,
            ),
        ],
    )
    (wf_dir / "agent-dead.meta.json").write_text('{"agentType":"Explore"}')
    # journal.jsonl must be ignored — write a non-transcript line.
    _write_jsonl(wf_dir / "journal.jsonl", [{"event": "phase", "label": "Research"}])

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    rollups = {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT bucket_kind, bucket_name, cost_usd FROM session_rollups WHERE session_id=?",
            (sid,),
        ).fetchall()
    }
    # First-class workflow bucket, named by meta.name (NOT the per-agent Explore).
    assert ("workflow", "workflow:sdk-drift-audit") in rollups
    assert ("main", "main") in rollups
    # No phantom 'agent-<hex>' or 'journal' session anywhere.
    phantom = conn.execute(
        "SELECT COUNT(*) FROM session_rollups WHERE session_id LIKE 'agent-%' OR session_id='journal'"
    ).fetchone()[0]
    assert phantom == 0
    # Invariant holds for this session.
    verify_invariant(conn, sid)
    # Per-message model is still derivable from the workflow agent's row.
    model = conn.execute(
        "SELECT model FROM messages WHERE session_id=? AND model='claude-haiku-4-5-20251001'",
        (sid,),
    ).fetchone()
    assert model is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_attribution.py::test_workflow_bucket_first_class -v`
Expected: FAIL — bucket renders `("subagent", "workflow:sdk-drift-audit")` (no `workflow` kind yet).

- [ ] **Step 3: Add `WORKFLOW` kind and the CASE branches**

In `src/ccforensics/attribution.py`, add to `BucketKind` (after line 15):

```python
    WORKFLOW = "workflow"
```

In `_BUCKET_KIND_EXPR`, insert a branch **before** the existing `subagent` branch (before line 23's `WHEN f.kind = 'subagent'`):

```sql
        WHEN f.kind = 'subagent'
             AND s.parent_message_dedup_key IS NOT NULL
             AND s.subagent_type LIKE 'workflow:%'
            THEN '{BucketKind.WORKFLOW}'
```

In `_BUCKET_NAME_EXPR`, insert the matching branch **before** its `subagent` branch (before line 35):

```sql
        WHEN f.kind = 'subagent'
             AND s.parent_message_dedup_key IS NOT NULL
             AND s.subagent_type LIKE 'workflow:%'
            THEN s.subagent_type
```

(The `subagent_type` is already `workflow:<name>`, so the name expr returns it verbatim.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_attribution.py::test_workflow_bucket_first_class -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/attribution.py tests/test_attribution.py
git commit -m "feat: first-class workflow:<name> attribution bucket"
```

---

### Task 8: Full suite + invariant + quality gates

**Files:** none (verification task)

- [ ] **Step 1: Run the full test suite with coverage gate**

Run: `uv run pytest --cov=ccforensics --cov-report=term-missing --cov-fail-under=85`
Expected: PASS, coverage ≥ 85%.

- [ ] **Step 2: Lint, format, types**

Run: `uv run ruff check && uv run ruff format --check && uv run mypy src/`
Expected: all clean.

- [ ] **Step 3: Smoke test against the real corpus**

Run: `uv run ccforensics aggregate 2>&1 | head -40`
Expected: at least one `workflow:<name>` line appears; no `agent-<hex>` or `journal` phantom sessions in `uv run ccforensics session list`.

- [ ] **Step 4: Commit (only if smoke surfaced a fixable issue; otherwise skip)**

---

### Task 9: Docs — CLAUDE.md schema v6 + plan progress

**Files:**
- Modify: `CLAUDE.md` (Schema section)

- [ ] **Step 1: Add the v6 entry**

In `CLAUDE.md`, under `### Schema (current: v5)`, change the heading to `v6` and add above the v5 block:

```markdown
**v6 (2026-06-08)** — dynamic-workflow attribution:

- Workflow-tool agents (`<enc>/<sess>/subagents/workflows/wf_<id>/agent-<hex>.jsonl`) classify as `subagent` kind with the orchestrator session id (path `parents[3]`), not phantom `main` sessions. `subagent_type = workflow:<name>` (name from the `Workflow` tool_use input), rendered as a first-class `workflow:<name>` bucket. `journal.jsonl` is skipped. `discover_spawn` accepts the `Workflow` tool_use; N agents from one call share one `parent_message_dedup_key`. Migration purges pre-existing `agent-<hex>`/`journal` phantom sessions and cold-reconciles. Known limit: a workflow launched from inside a subagent file may not resolve a parent → `unattributed`.
```

Also update the attribution-model bullet list in CLAUDE.md to mention the `workflow:<name>` bucket alongside `main` / `subagent:<type>` / `auto-compact` / `unattributed`.

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: schema v6 — dynamic-workflow attribution"
```

---

## Self-review

**Spec coverage:**
- Path-based detection → Task 4. ✓
- `journal.jsonl` skip → Task 6 (+ asserted Task 7). ✓
- Orchestrator session via `parents[3]` → Task 4; orchestrator message via `discover_spawn` → Task 3. ✓
- `subagent_type = workflow:<name>` ignoring meta.agentType → Task 3 (asserted with `agentType:"Explore"`). ✓
- Name extraction 4 shapes + fallback → Task 2 + Task 3 fallback test. ✓
- First-class `workflow:<name>` bucket → Task 7. ✓
- v5→v6 phantom purge + cold reconcile → Task 1. ✓
- Invariant preserved + model derivable → Task 7 assertions. ✓
- N-agents-share-one-parent → supported (no schema block; `subagent_spawns` keyed per child file); covered implicitly by Task 7 (single agent) — acceptable for v1, the parent-key sharing is exercised by `_reconcile_spawn`'s existing dedup-key lookup.
- Unresolvable parent → `unattributed` → Task 3 unresolvable test (parent uuid None → null dedup key → ELSE branch).

**Placeholder scan:** none — every code step has complete code.

**Type/name consistency:** `is_workflow` keyword consistent across Task 3 (def) and Task 6 (call). `_WORKFLOW_AGENT_RE` defined Task 4, reused Task 5/6. `BucketKind.WORKFLOW` defined and referenced Task 7. `_workflow_name` defined Task 2, used Task 3.
