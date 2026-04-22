# M0–M3 Code Review Findings

Captured after the superpowers:code-reviewer pass on `feature/initial-implementation` at the M3.4 exit point.

**Assessment:** ship M0–M3 as-is. No critical issues. Six "Important" items below are polish-grade, not blockers — fix before the next milestone *touches the same file*, not speculatively.

## Important (fix opportunistically — before the next milestone touches the same file)

1. **`paths.claude_home()` HOME fragility** — `os.environ.get("HOME", "~")` + `.expanduser()` silently returns a relative `~/.claude` when HOME is unset, producing `./~/.claude` on cache write. Switch to `Path.home()` (raises `RuntimeError` when unresolvable) or explicitly guard. *File:* `src/ccforensics/paths.py:10`.

2. **`resolve_pricing` silent substring fallback** — the second loop (`lowered in kl or kl in lowered`) can match the wrong model entry without any warning. Emit a log.warning when substring fallback resolves so `-v` surfaces it. Risk scenario: LiteLLM drops/renames a Claude key → silent misattribution under the ±1% check. *File:* `src/ccforensics/pricing.py:57-62`.

3. **`cost_usd` inconsistency for non-billable types** — `user` entries get `0.0`; `system`, `attachment`, `file-history-snapshot` get `None`. `SUM()` ignores NULL so totals are fine, but downstream queries distinguishing "zero cost" from "not billable" will be ambiguous. Unify on one convention. *File:* `src/ccforensics/jsonl.py:129-130`.

4. **`_classify_file` silently mislabels malformed subagent filenames** — a file under `subagents/` whose name doesn't match `agent-<hex>.jsonl` is classified as `main` with the wrong `session_id`. If Claude Code ever changes subagent naming, every subagent file becomes a "main" session. Add warn + classify as `subagent` with `None` agent_id. *File:* `src/ccforensics/index.py:162-172`.

5. **`reconcile_projects_dir` has no per-file commit** — mid-walk interrupt on the 14,394-file corpus loses all work. Consider `conn.commit()` after each `reconcile_file` or every N files. Resilience + progress-visibility. *File:* `src/ccforensics/index.py:reconcile_projects_dir`.

6. **`parse_file` claims streaming, actually reads whole file** — `path.read_text().splitlines()` buffers everything. Either make it a true streaming iterator (open + for-line + look-ahead for last-line truncation detection) or update the docstring to say "buffered parse." Spec §3.1 says streaming. *File:* `src/ccforensics/jsonl.py:34`.

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
