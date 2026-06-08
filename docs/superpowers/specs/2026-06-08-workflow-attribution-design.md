# Dynamic-workflow attribution — design

**Date:** 2026-06-08
**Status:** approved, pre-implementation
**Schema target:** v6

## Problem

Claude Code's dynamic-workflow feature (the `Workflow` tool) spawns a fleet of
subagents and writes each one's transcript to:

```
<enc>/<orchestrator-session>/subagents/workflows/wf_<id>/agent-<hex>.jsonl
<enc>/<orchestrator-session>/subagents/workflows/wf_<id>/journal.jsonl
<enc>/<orchestrator-session>/subagents/workflows/wf_<id>/agent-<hex>.meta.json
```

`index.py::_classify_file` only recognises subagent files as **direct** children
of a `subagents/` directory (`path.parent.name == "subagents"`). Workflow agent
files are two levels deeper (`.../subagents/workflows/wf_<id>/`), so
`path.parent.name == "wf_<id>"` — they fall through to the final
`return ("main", None, path.stem)` and are misclassified as **phantom `main`
sessions** named `agent-<hex>` (and `journal`).

### Measured impact (real corpus, 2026-06-08)

- 46 files under `subagents/workflows/wf_*/` — **all** classified `main`.
- **1,212 billable assistant rows** mis-bucketed: 751 × `claude-haiku-4-5`,
  461 × `claude-opus-4-8`.
- Each workflow agent file becomes a separate phantom `main` session
  (`agent-<hex>`), polluting `session list` / `aggregate`.
- `journal.jsonl` → phantom `main` session `journal` (0 billable rows — noise).

The bucket invariant `sum(rollups) == sum(messages)` still holds (cost is counted
once), so the failure is **silent misattribution** — exactly the class of bug
ccforensics exists to catch. The dynamic-workflow feature is the new
Agent SDK / Claude Code surface, and ccforensics is currently blind to it.

## Why this was missed

The Workflow tool's on-disk artifact shape is a **harness feature**, not part of
the `@anthropic-ai/claude-agent-sdk` npm changelog or the transcript-format
references — so a changelog/format audit never surfaces it. The artifacts only
appear once you actually run a workflow.

## Goals

1. Attribute every workflow agent's cost to a first-class `workflow:<name>`
   bucket tied back to the orchestrating session and the exact `Workflow`
   tool_use that launched it.
2. Per-workflow-run granularity (one named workflow = one bucket line), with
   per-message **model** still fully derivable from `messages.model`.
3. Purge the phantom `main` sessions already written to existing indexes.

Non-goals (v1): per-agent (`Explore` vs default) sub-breakdown; attributing
workflows launched from inside a subagent file; parsing `journal.jsonl`.

## Decisions (locked)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Bucket model | Reuse `subagent_spawns`; `subagent_type = workflow:<name>` | Smallest schema delta; existing spawn + attribution plumbing |
| Bucket string | `workflow:<name>` (first-class) | Mission-aligned — workflows are a distinct cost driver, visible alongside `main`/`subagent:`/`auto-compact`/`unattributed` |
| Detection signal | **Path** `*/subagents/workflows/wf_<id>/agent-<hex>.jsonl` | meta.json `agentType` is unreliable — it reflects each `agent()`'s `agentType` opt (`Explore`, `workflow-subagent`, …), not "this is a workflow" |
| Granularity | Run-level | Cost-driver view; model still derivable per row |
| `journal.jsonl` | Skip | 0 billable rows; orchestration log, not a transcript |
| Unresolvable parent | `unattributed` | Graceful, same as today's orphan subagents |

## Data flow

Changed nodes marked ⬢:

```
fs → ⬢_classify_file → reconcile_file → ⬢_reconcile_spawn(⬢tree.discover_spawn)
   → subagent_spawns → ⬢attribution CASE → reports
```

## Linkage model

Every workflow agent entry carries `sessionId = <orchestrator-session>` and
`isSidechain: true`; the on-disk path grandparent agrees. Agents do **not**
record `parentToolUseId`/`sourceToolUseID`, so the specific launching message is
resolved the same way subagents already are — by `discover_spawn`'s
nearest-`Workflow`-tool_use-before-first-child-timestamp heuristic (handles
multiple `Workflow` calls per session).

- **orchestrator session** ← `path.parents[3].name` (no file read needed)
- **orchestrator message** ← `discover_spawn` → `parent_message_dedup_key`
- One `Workflow` call → N agent files share one `parent_message_dedup_key`
  (already supported: parallel `Agent` tool_uses share a dedup_key too).

## Component changes

| # | File / symbol | Change |
|---|---------------|--------|
| 1 | `index.py::_classify_file` | New branch: path matches `*/subagents/workflows/wf_<id>/agent-<hex>.jsonl` → `("subagent", <hex>, session=path.parents[3].name)`. Reuses the `subagent` kind. |
| 2 | `index.py` reconcile walk | `if path.name == "journal.jsonl": continue` before classify. |
| 3 | `index.py::_parent_session_path` | Workflow-path branch → orchestrator file `<enc>/<parents[3]>.jsonl`. |
| 4 | `tree.py::_iter_agent_tool_uses` | Accept `"Workflow"` in the matched name set `("Agent","Task","Workflow")`. |
| 5 | `tree.py::discover_spawn` + new `_workflow_name()` | For a workflow spawn, set `subagent_type = "workflow:" + name`, ignoring per-agent `meta.agentType`. Name extraction order: `input.name` (saved workflow) → `scriptPath` filename stem (strip trailing `-wf_<id>`) → regex `name:\s*['"]([^'"]+)['"]` over `input.script` (inline; `meta` is a pure literal per the tool contract) → fallback `wf_<id>`. |
| 6 | `attribution.py` bucket CASE | Add `WHEN subagent_type LIKE 'workflow:%' THEN subagent_type` (→ `workflow:<name>`) **before** the generic `'subagent:' || subagent_type` branch. |
| 7 | `index.py` schema **v5→v6** | Migration: purge phantom rows (`DELETE` from `messages`, `session_rollups`, `session_summaries`, `session_signals` `WHERE session_id LIKE 'agent-%' OR session_id = 'journal'`), `DELETE FROM files WHERE path LIKE '%/subagents/workflows/%'`, trailing `UPDATE files SET mtime_ns = 0` for cold re-reconcile. Bump `CURRENT_SCHEMA_VERSION = 6`. |

## Invariants & safety

- **Cost invariant preserved:** every message still counted exactly once; only
  the bucket + session change. `verify_invariant` (tolerance `1e-6`) must pass.
- **Walk order safe:** `<sess>.jsonl` sorts before `<sess>/subagents/...`
  (`.` 0x2E < `/` 0x2F), so the orchestrator session is indexed before its
  workflow agents and `discover_spawn` finds the `Workflow` call.
- **Phantom purge:** the v6 migration removes `agent-<hex>` / `journal` phantom
  sessions from existing indexes; the cold reconcile re-classifies the real
  workflow agent files into `workflow:<name>`.

## Known limitations (v1, documented)

- A workflow launched **from inside a subagent file** (the `Workflow` tool_use
  lives in a non-orchestrator-session file) may not resolve a parent →
  `unattributed`. Acceptable; mirrors today's orphan-subagent behaviour.
- `scriptPath` name derivation is best-effort filename parsing; falls back to
  `wf_<id>`.
- Nested `workflow()` calls are out of scope (one level only per tool contract).

## Test plan (TDD)

New redacted fixture: one workflow agent `agent-<hex>.jsonl` + a parent session
file containing a `Workflow` tool_use (redact via `scripts/redact_jsonl.py`).

Cases:

1. `_classify_file` on a workflow path → `("subagent", <hex>, <orchestrator-session>)`.
2. `_classify_file` still correct for direct-child subagents and auto-compact (regression).
3. `_workflow_name` — all four input shapes (`name`, `scriptPath`, inline `script` regex, fallback).
4. Reconcile walk skips `journal.jsonl` (no phantom `journal` session, no parse of orchestration records).
5. `discover_spawn` links a workflow agent to the parent `Workflow` tool_use → correct `parent_message_dedup_key`.
6. N agents from one `Workflow` call share one `parent_message_dedup_key` and roll into one `workflow:<name>` line.
7. Attribution bucket renders `workflow:<name>` (not `subagent:workflow:<name>`, not `main`).
8. `verify_invariant` holds on a session containing a workflow.
9. v6 migration purges pre-existing phantom `agent-<hex>` / `journal` sessions and cold-reconciles.
10. Unresolvable parent → `unattributed`.

(No LLM-boundary mock in scope — no new LLM call is introduced.)
