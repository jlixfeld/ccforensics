# M0–M3 Code Review Findings

Captured after the superpowers:code-reviewer pass on `feature/initial-implementation` at the M3.4 exit point.

**Assessment:** ship M0–M3 as-is. No critical issues. Six "Important" items below are polish-grade, not blockers — fix before the next milestone *touches the same file*, not speculatively.

## Important (RESOLVED — all 6 fixed in commits f553d9d → fe8ccad)

1. ✅ **`paths.claude_home()` HOME fragility** → `f553d9d` switched to `Path.home()`, which raises `RuntimeError` when unresolvable.
2. ✅ **`resolve_pricing` silent substring fallback** → `4bf7649` added a `logger.warning` when substring fallback fires (exact-match candidates stay silent).
3. ✅ **`cost_usd` inconsistency for non-billable types** → `69c5412` unified to `0.0` for all non-billable types; `None` is now reserved strictly for the pricing-unresolved case.
4. ✅ **`_classify_file` silently mislabels malformed subagent filenames** → `e654767` warns + classifies as `subagent` with `agent_id=None` (no more silent `main`).
5. ✅ **`reconcile_projects_dir` no per-file commit** → `aabdcb3` added `conn.commit()` after each `reconcile_file`. Test simulates a mid-walk `KeyboardInterrupt` and asserts already-reconciled files are durable.
6. ✅ **`parse_file` claims streaming, actually buffers** → `fe8ccad` rewrote to iterate via `path.open()`; trailing-newline probe handles the last-line truncation classification.

## Minor (defer to M10 polish)

7. **`f"PRAGMA user_version = {target + 1}"`** — safe as written (`target` bounded by a module-constant range, PRAGMA doesn't accept parametrized values anyway). Noted for the audit trail.

8. **`parse_warnings` is a count, not text** — warning *content* is discarded. Spec §3.5 wants surfaceable warnings. Before M7 (session deep report), persist warning text in a JSON blob column or separate table.

9. **`session` click group collides with future `session <id>` positional** — `session list` subcommand works, but adding `session <uuid-prefix>` at the same level requires Click restructuring at M4. Flag before it surprises.

10. **`time.sleep(1.1)` in `test_reconcile_unchanged_file_is_noop`** — 1.1s per CI run. Inject a clock or monkeypatch `time.time` in the index module.

11. **`ccforensics_cache_dir()` coupling via env vars** — tests set `XDG_CACHE_HOME`, but macOS `platformdirs` prefers `~/Library/Caches`. A `--cache-dir` CLI flag plus a parameter on `ccforensics_cache_dir` would decouple tests from platformdirs quirks. Fits naturally when the CLI adds the flag in M10.

12. **`PricingCache._fetch_and_store` type-annotates the response but doesn't validate** — `resp.json()` could return anything at runtime. A one-line `if not isinstance(data, dict): raise ValueError(...)` would fail-loud on a LiteLLM format regression rather than degrading silently.

13. **`jsonl.py` size** — parse + dedup + cost annotation in one 150-line module. Still coherent today. Natural partition when M5 tree reconstruction lands: `jsonl.py` (parse-only), `dedup.py`, `cost.py`.

## Strengths worth preserving

- Parser defense is exactly right (truncation = silent, drift = warn-once, malformed mid-file = count-and-continue).
- Dedup determinism is actually tested (5 shuffle seeds).
- The ccusage ±1% ground-truth test is real — pinned real-number anchor with ±1% tolerance landed at 0.00%.
- Migration scaffold is minimum-viable, not over-built.
- `reconcile_projects_dir` double-checks `_row_is_unchanged` before calling `reconcile_file` — walker idempotence doesn't depend on the reconciler.
- Tests verify content, not mock calls.

## Security

No concerns. DDL is module-constant strings; all other SQL uses `?` placeholders. Path/JSON inputs come from the user's own filesystem — not hostile.

## Coverage

94.11% (target ≥ 85%). Uncovered lines are one-line logging fallbacks and "should never happen" guards.
