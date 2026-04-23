# Codebase Audit — 2026-04-23

One-time holistic review of the full source tree on `feature/initial-implementation` after M0–M9, before establishing incremental per-PR review.

**Agents run in parallel** via the `codebase-audit` skill:
`code-reviewer`, `silent-failure-hunter`, `pr-test-analyzer`, `comment-analyzer`, `type-design-analyzer`, `security-reviewer`.

**Headline:** no critical security issues, no SQL injection, no secrets. The dominant problem is **silent-failure patterns that can subtly corrupt cost attribution** — starting with the fact that `logging.basicConfig` is never wired up in the CLI, so every `logger.warning` in the codebase is currently a no-op.

---

## Tier 1 — Highest leverage, quick fixes ✅

| ✓ | File:line | Issue |
|---|---|---|
| ✅ | `cli.py:48-51` | **No `logging.basicConfig` — every `logger.warning` is silently dropped.** One-line fix mitigates 10+ other findings. |
| ✅ | `index.py:501` | `next(iter(set))` picks non-deterministic `schema_version`. Use `min(result.seen_versions, default=None)`. |
| ✅ | `report/session.py:370`, `report/plugins.py:181-188` | Falsy-check (`if cost`) drops legit `0.0`. Use `is not None`. |
| ✅ | `skills.py:42-44,32,435-436` | Dead code: unused `_USER_SKILL_PATH_RE`, `_ = Any`, unused `Any` import. |

## Tier 2 — Correctness (silent misattribution) ✅

| ✓ | File:line | Issue |
|---|---|---|
| ✅ | `pricing.py:62-71` | Bidirectional substring match over 1000+ LiteLLM keys is non-deterministic and can mis-price. Narrowed to one direction + longest-match determinism. |
| ✅ | `pricing.py:137-157` | Hardcoded 6-model fallback silently fabricates prices on fetch failure — needs visible banner or `--allow-stale-pricing`. CLI banner added on stderr for stale + fallback. |
| ✅ | `index.py:744-761` | Broad `except Exception` around 4 recompute ops silently breaks the attribution invariant. Narrowed to `(OSError, sqlite3.Error)`. |
| ✅ | `index.py:736-740` | Same pattern around `populate_registry`. Narrowed to `(OSError, sqlite3.Error)`. |
| ✅ | `jsonl.py:210-218` | `unresolved` set built but never returned — now surfaced via `logger.warning` in `reconcile_file` per unresolved model per file. |
| ✅ | `skills.py:83-89, 85-86, 239-244, 319-325` | Silent-skip paths now emit `logger.warning` with session/manifest context. |
| ✅ | `skills.py:96-99` | Comment claim was false — setdefault kept FS-first. Fixed by grouping installs per plugin and picking highest version via `version_sort_key`. |

**Deferred follow-up (flagged by audit but out of scope for this PR):**
- `skills.py:write_activations` atomicity — DELETE + INSERTs aren't savepoint-wrapped, so a mid-loop `sqlite3.Error` leaves a session with fewer rows than it started. Narrowed-except still catches it. Fix separately with a SAVEPOINT.

## Tier 3 — Security polish (all LOW) ✅

| ✓ | File | Issue |
|---|---|---|
| ✅ | `export.py:14-26` | CSV formula injection — prefix `'` on cells starting with `=+-@\t\r`. |
| ✅ | `pricing.py:159-166` | Added `follow_redirects=False`, `verify=True`, and 10 MB streamed response cap. |
| ✅ | `jsonl.py:50-51` | Per-line read capped at 16 MB; oversize lines counted as parse errors and drained. |

## Tier 4 — Test gaps ✅

| ✓ | Gap |
|---|---|
| ✅ | Pricing cache corruption (partial JSON, missing `data` key, non-int `fetched_at`) |
| ✅ | Schema downgrade guard (`user_version > CURRENT` RuntimeError path) |
| ✅ | CLI resolver errors — `AmbiguousPrefix` / `SessionNotFound` exit code 2 + stderr |
| ✅ | `verify_invariant` with NULL-cost messages (unresolved pricing) |
| ✅ | Zero-cost / empty-bucket session rendering |
| ✅ | Skill channel-A + channel-B on same session both fire |
| ✅ | Skill resolver — plugin+user skills with same name coexist |
| ✅ | CLI narrow-terminal via `COLUMNS=80` env drops Project column |
| ✅ | Replaced `time.sleep(1.1)` with monkeypatched parse counter |
| ✅ | Deleted redundant snapshot test (kept explicit ones that assert content) |

## Tier 5 — CLAUDE.md compliance (comment + style) ✅

| ✓ | Change |
|---|---|
| ✅ | Deleted spec `§X.Y` leakage from `attribution.py`, `skills.py`, `registry.py`, `report/{aggregate,plugins,session}.py` |
| ✅ | Deleted milestone refs ("M8", "deferred to a later pass") |
| ✅ | Shrunk module-level docstrings to one-liner where appropriate |
| ✅ | Shrunk narrative function docstrings in index/jsonl/models/tree/attribution/registry/skills |
| ✅ | Added `pricing.py` cache-multiplier rationale, `skills.py` plugin cache layout comment |
| ⊘ | **Not done**: `dedup_entries` consolidation — both paths share `dedup_key` + `_dedup_preference` primitives; no real logic duplication, just different callers (bare entries vs (entry, cost) pairs) |
| ⊘ | **Not done**: `_maybe_refresh` helper — the 6× `PricingCache` expansion was already replaced with `_load_pricing()` in Tier 2; the reconcile-then-commit pattern isn't worth further DRY |

## Tier 6 — Type design ✅ (scoped)

| ✓ | Change |
|---|---|
| ✅ | `BucketKind` StrEnum replacing string literals in `attribution.py` CASE expressions |
| ✅ | `UserLevelArtifact.kind: ArtifactKind` (`Literal["skill", "agent"]`) |
| ⊘ | **Deferred**: `TranscriptEntry.type` Literal — pydantic would reject unknown types at validation, contrary to the existing permissive-by-design parse pipeline that warns-and-keeps via `KNOWN_TYPES` |
| ⊘ | **Deferred**: `NewType` aliases (`SessionId`, `DedupKey`, etc.) — touches every function signature in 15+ modules; separate refactor PR |
| ⊘ | **Deferred**: Split `SkillActivation` into resolved/unresolved variants — churn across detection + write paths + tests; separate PR |

## Excluded (not findings)

- `jsonl.py:126-153` dedup preference tuple — exemplary WHY comment, leave alone
- `tree.py:199-201` `wanted=None` rank key — documented, matches spec
- No hardcoded secrets, no SQL injection, no unsafe deserialization

## Sequencing

1. **Tier 1 + Tier 2** together — correctness fixes where the attribution invariant is on the line
2. **Tier 3** — small hardening PR
3. **Tier 4** — test-only PR
4. **Tier 5 + 6** — cleanup/refactor PR; run `code-simplifier` afterward per audit skill Step 4
