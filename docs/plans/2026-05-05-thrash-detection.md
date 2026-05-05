# Thrash detection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `ccforensics thrash` command — detection of suspicious Sonnet/Haiku sessions where lower-model use plausibly cost more than higher-model use would have. Surfaces signal evidence + escalation-anchored counterfactual cost range. Read-only forensic surface; no real-time intervention.

**Architecture:** Schema v3 → v4 adds `session_rollups.{thrash_score, thrash_score_version, escalation_event}` columns and a new `session_signals` table. New `thrash.py` module hosts 10 signal extractors + composite scorer + escalation detector. New `thrash_calibration.py` builds in-memory CalibrationTable per report run. New `report/thrash.py` queries + renders. Bucket-attribution invariant unaffected — `thrash_score` is metadata only.

**Tech Stack:** Python 3.13, uv, SQLite (stdlib `sqlite3`), pydantic v2, click, pytest, ruff, mypy strict.

**Spec:** `docs/specs/2026-05-05-thrash-detection-design.md`

---

## File map

**Modify:**
- `src/ccforensics/index.py` — schema v4 migration, `CURRENT_SCHEMA_VERSION` bump, hook into `attribution.recompute_session_rollups`
- `src/ccforensics/attribution.py` — call `thrash.populate_session_signals` + `compute_thrash_score` after rollup recompute
- `src/ccforensics/cli.py` — add `thrash` command
- `src/ccforensics/report/__init__.py` — re-export new module
- `tests/test_attribution.py` — bucket invariant guard for v4 migration
- `tests/test_index_schema.py` — v4 schema test
- `README.md` — `thrash` command example, sample evidence output, caveats note
- `CHANGELOG.md` — Unreleased entry

**Create:**
- `src/ccforensics/thrash.py` — signal extractors + scorer + escalation detector + `SIGNAL_VERSION` constant
- `src/ccforensics/thrash_calibration.py` — CalibrationTable builder + counterfactual + sanity gate
- `src/ccforensics/report/thrash.py` — query + render (collapsed, --evidence, --session, JSON, CSV)
- `tests/test_thrash_signals.py` — per-extractor unit tests (10 signals)
- `tests/test_thrash_score.py` — composite scorer + version mismatch
- `tests/test_escalation_detect.py` — model_switch + subagent_dispatch + auto_mode kinds
- `tests/test_thrash_calibration.py` — confidence tiers + sanity gate + auto_mode exclusion
- `tests/test_thrash_report.py` — render + JSON/CSV + headline aggregate
- `tests/test_thrash_filter.py` — Opus/short-session filter

---

## Milestones

- **T0** — Schema v4 migration + version constants land; existing tests still green.
- **T1** — Session filter + `placeholder_emit` + `repeated_edit` + `repeated_error` extractors land with unit tests.
- **T2** — `tool_arg_churn` (with result-variation suppression) + `user_correction` + `session_abandoned` + `trajectory_length_zscore` extractors.
- **T3** — `novelty_window` (with text-jaccard suppression) + `turn_cost_acceleration` (output-token slope) + `test_regression` extractors.
- **T4** — Composite scorer + `SIGNAL_VERSION` mismatch handling + `populate_session_signals` orchestrator.
- **T5** — Escalation detector: `model_switch` + same-tier rejection + auto_mode classification.
- **T6** — Subagent_dispatch escalation kind (cross-file: parent session ↔ subagent rollup).
- **T7** — Calibration table query + confidence tiers + sanity gate.
- **T8** — Report module: collapsed table + headline aggregate + caveats footer.
- **T9** — Report `--evidence` expansion + `--session` drill-in.
- **T10** — JSON / CSV outputs.
- **T11** — CLI wiring (`thrash` command), `--no-refresh`, scope flags.
- **T12** — README + CHANGELOG updates.
- **T13** — Cold-backfill validation on real corpus; tag `v0.2.0`.

---

## Task 1: Schema v4 migration

**Files:**
- Modify: `src/ccforensics/index.py` — bump `CURRENT_SCHEMA_VERSION`, append migration to `MIGRATIONS`
- Test: `tests/test_index_schema.py` — append v4 schema test

- [ ] **Step 1: Failing test for v4 schema**

```python
def test_schema_v4_creates_thrash_columns_and_session_signals(tmp_path: Path) -> None:
    from ccforensics.index import ensure_schema, open_connection, CURRENT_SCHEMA_VERSION

    db = tmp_path / "v4.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)

    assert CURRENT_SCHEMA_VERSION >= 4

    rollup_cols = {row[1] for row in conn.execute("PRAGMA table_info(session_rollups)").fetchall()}
    assert "thrash_score" in rollup_cols
    assert "thrash_score_version" in rollup_cols
    assert "escalation_event" in rollup_cols

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "session_signals" in tables

    sig_cols = {row[1] for row in conn.execute("PRAGMA table_info(session_signals)").fetchall()}
    assert sig_cols == {"session_id", "signal_type", "count", "evidence", "signal_version"}
```

- [ ] **Step 2: Apply migration**

Append to `MIGRATIONS`:
```python
"ALTER TABLE session_rollups ADD COLUMN thrash_score REAL",
"ALTER TABLE session_rollups ADD COLUMN thrash_score_version INTEGER",
"ALTER TABLE session_rollups ADD COLUMN escalation_event TEXT",
"""CREATE TABLE session_signals (
    session_id     TEXT NOT NULL,
    signal_type    TEXT NOT NULL,
    count          INTEGER NOT NULL,
    evidence       TEXT NOT NULL,
    signal_version INTEGER NOT NULL,
    PRIMARY KEY (session_id, signal_type)
)""",
"CREATE INDEX idx_signals_session ON session_signals(session_id)",
"UPDATE files SET mtime_ns = 0",
```
Bump `CURRENT_SCHEMA_VERSION = 4`.

- [ ] **Step 3: Verify bucket invariant unaffected**

Add to `tests/test_attribution.py`:
```python
def test_v4_migration_preserves_bucket_invariant(...):
    # Run reconcile post-migration; assert Σ(session_rollups.cost_usd) == Σ(messages.cost_usd) per session
```

---

## Task 2 — Task 13: see Milestones above

Each milestone gets a section here as it's started. Initial pattern:
- Failing test first.
- Implementation matching spec.
- Refactor + ruff/mypy/coverage clean.
- Mark `- [x]` complete.

Detailed task decomposition for T1–T13 added incrementally during impl per `superpowers:subagent-driven-development`. Spec is the authoritative reference for behavior, edge cases, and weights.

---

## Validation gates

CI must pass:
- `uv run pytest --cov=ccforensics --cov-report=term-missing --cov-fail-under=85`
- `uv run ruff check && uv run ruff format --check`
- `uv run mypy src/`

Bucket-attribution invariant `Σ(session_rollups.cost_usd) == Σ(messages.cost_usd)` per session — asserted before and after v4 migration.

Pre-merge validation spike (precision/recall on hand-labeled corpus) **DEFERRED** — see spec §6 "Pre-merge validation spike — DEFERRED." v1 ships with intuition-tuned thresholds; future tracked as TODO.

---

## Out of scope for this plan

Per spec §0 "Out of scope" and §8 "Future directions":
- Real-time intervention.
- Per-message model recommendations.
- NLP/LLM-driven user-intent inference.
- Over-modeling detection (inverse direction).
- Edit-revert detection.
- Code-health routing signal (Triage paper).
- Per-skill / per-plugin thrash rate aggregations.
