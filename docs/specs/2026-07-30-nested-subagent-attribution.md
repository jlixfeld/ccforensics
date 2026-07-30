# Nested (depth-3+) subagent attribution — schema v7

Status: implemented (2026-07-30). Normative for how a subagent spawned *by a
subagent* is linked to the call that created it.

## Problem

Claude Code writes every subagent transcript for a session into one flat
directory:

```
<enc>/<sessionId>/subagents/agent-<hex>.jsonl          # + agent-<hex>.meta.json
<enc>/<sessionId>/subagents/workflows/wf_<id>/agent-<hex>.jsonl
```

Depth is **not** encoded in the path. A subagent spawned from the main loop and
a subagent spawned by another subagent are written to the same directory, with
the same filename shape — only workflow-tool agents get a subdirectory. So a
child three levels down (`main → Explore → Plan`) is indistinguishable, by
path, from a direct spawn.

Before v7, `_parent_session_path` therefore always pointed spawn discovery at
`<enc>/<sessionId>.jsonl`, and `tree.discover_spawn` looked there for the
nearest-preceding `Agent`/`Task` tool_use. For a nested child that call does not
exist in the main file — it lives in the *parent subagent's* transcript. Two
outcomes, both wrong:

1. **Silent mis-linkage (the common case).** Some earlier main-loop Agent/Task
   call almost always exists, so the nested child bound to it. The spawn's
   `parent_message_dedup_key` then pointed at a call that did not create it,
   and `backfill_spawn_totals` hung the child's cost off the wrong parent.
2. **No linkage.** With no candidate before `ts_spawned`, the child fell to
   `unattributed`.

Bucket *totals* were never wrong: `subagent_type` comes from `meta.json`, so a
nested agent's cost always sat in the correct `subagent:<type>` bucket, and the
`sum(rollups) == sum(messages)` invariant always held. What was wrong is the
spawn graph — which call is responsible for which agent.

## The only reliable signal

`agent-<hex>.meta.json` carries `parentAgentId`, and Claude Code writes that key
**only when the spawner was itself an agent**. Its presence is therefore exactly
equivalent to "this is a depth-3-or-deeper spawn"; its absence means main-loop.

The sidecar also carries `spawnDepth`, but Claude Code only emits it on some
spawn paths, so it is deliberately not consumed — depth is derived from the
`parentAgentId` chain instead.

## Design

### Parent transcript selection (`index._spawn_parent_path`)

| meta.json | Transcript scanned for the spawning tool_use |
|---|---|
| no `parentAgentId` | `<enc>/<sessionId>.jsonl` (unchanged) |
| `parentAgentId: X` | `agent-X.jsonl` — sibling in `subagents/`, else found by `rglob` under the session's `subagents/` tree |
| `parentAgentId: X`, no such file | main session file, with a warning (degrades to pre-v7 behaviour) |

The `rglob` fallback exists for one real case: a **workflow** agent lives under
`subagents/workflows/wf_<id>/` but a subagent it spawns is written flat into
`subagents/`, so parent and child are in different directories.

`discover_spawn` itself is unchanged — it scans whatever entries it is handed.
Choosing the file is the indexer's job, which keeps the nearest-before ranking
heuristic in one place.

### Columns

- `subagent_spawns.parent_agent_id` — verbatim from meta.json; NULL for a
  direct spawn. Indexed (`idx_spawns_parent_agent`) because the depth CTE and
  any future tree report join on it.
- `subagent_spawns.spawn_depth` — computed, **transcript-counting**: main = 1,
  direct subagent = 2, nested = 3, and so on. Written by
  `attribution.backfill_spawn_depths`, a recursive CTE seeded from the rows with
  a NULL `parent_agent_id` and capped at `MAX_SPAWN_DEPTH` (a guard against
  cyclic metadata, not a real limit). A row whose named parent has no spawn row
  is left NULL rather than guessed at.

### Ordering

Two ways the incremental walk can strand a nested spawn, both fixed by widening
the existing post-walk `_reresolve_spawns_for_sessions` pass rather than adding
a new mechanism:

- **Child before parent.** Siblings sort by hex agent id, so the walk may
  reconcile a child before its parent's messages exist in `messages`; the
  `parent_message_dedup_key` lookup then finds nothing.
- **Parent re-ingested alone.** Re-reconciling a parent *subagent* purges its
  messages, which cascades every nested child's `parent_message_dedup_key` to
  NULL via `ON DELETE SET NULL` — and the child's own mtime is unchanged, so
  the walk never revisits it.

The pass now covers sessions whose main file was re-ingested (as before), plus
any touched session that contains nesting. One pass is enough at **any** depth:
by the time it runs, every file in the corpus is indexed, so each spawn's parent
messages are present regardless of the order they were walked in.

### Attribution

Unchanged. A nested subagent buckets as `subagent:<its own type>` exactly like a
direct spawn. Nesting alters *which transcript* the parent call is looked up in,
never which bucket the cost lands in — so no code path can now route cost to
more than one bucket, and the hard invariant is untouched.

`backfill_spawn_totals` also stays direct-cost-only: a parent spawn's
`total_cost_usd` does **not** include its nested children's cost. Each file's
messages are counted once, in that file's own spawn row and its own bucket.
`spawn_depth` + `parent_agent_id` are what a caller would use to roll a subtree
up, and rolling up is deliberately left to the reporting layer so the stored
totals stay non-overlapping.

## Migration

v6 → v7 adds the two columns, the index, and a trailing
`UPDATE files SET mtime_ns = 0` to force a cold re-reconcile. The cold pass is
required, not cosmetic: nesting is only visible in `meta.json`, which is read
during reconcile, so existing rows cannot be repaired by SQL alone.
`_reconcile_spawn` writes with `INSERT OR REPLACE`, so mis-linked pre-v7 rows
are corrected in place.

## Known limits

- A nested child whose parent transcript is missing from disk degrades to the
  main-file scan, i.e. it can still mis-link the way pre-v7 did. It warns.
- A workflow launched from inside a subagent still may not resolve a parent
  (pre-existing v6 limit; `_spawn_parent_path` only redirects Agent/Task
  lookups, not `Workflow` ones).
