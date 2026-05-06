---
title: Thrash detection — model-misuse signal mining and counterfactual cost
date: 2026-05-05
status: draft
version: 0.3
supersedes: none
extends: docs/specs/design.md
---

# Thrash detection — model-misuse signal mining and counterfactual cost

**Date:** 2026-05-05
**Status:** Draft (pending user review)
**Owner:** jason@lixfeld.ca

## 0. Motivation and scope

Real-world pattern: a lower-tier model (Sonnet/Haiku) is used for a session that exceeds its capability ceiling. The model loops — repeated edits to the same file, the same tool error recurring, the user typing short corrections — accumulating turn cost without converging. Eventually the user escalates to Opus (or abandons), and Opus resolves in 1–3 turns. Net: more dollars spent, more wall-clock burned, worse user experience than starting on Opus.

ccforensics already attributes every dollar to a bucket. This spec adds **detection of suspicious sessions** where lower-model use plausibly cost more than higher-model use would have, plus a **counterfactual range** anchored on observed escalation events.

### In scope

- Schema v3 → v4 migration adding `session_rollups.thrash_score`, `session_rollups.thrash_score_version`, `session_rollups.escalation_event` (JSON), and a new `session_signals` table for per-session signal counts.
- Signal extractors (10 typed signals): repeated-edit, repeated-error, user-correction, tool-arg-churn, novelty-window, turn-cost-acceleration, session-abandoned, placeholder-emit, trajectory-length-zscore, test-regression.
- Low-tier filter: `monotone_low_tier` is a session precondition (Sonnet/Haiku session ≥ N turns), not a standalone signal. Scores only when ≥2 signal types also fire.
- Mid-session escalation detection: model switch (Sonnet/Haiku → Opus), subagent dispatch (parent Sonnet → Opus subagent), and auto-mode bouncing — each tagged distinctly. Ground-truth labeled positives.
- Counterfactual cost estimator anchored on observed post-escalation turns/cost. Range + calibration confidence tier (low/mid/high), not a point estimate.
- New `ccforensics thrash` CLI subcommand: list flagged sessions, show evidence per session, JSON/CSV. Headline aggregate at top (last-N-day total observed vs est savings).
- Footer warning surfaces when escalation events exist but corpus is too small to calibrate (<10 events).

### Out of scope

- Real-time intervention ("you should switch models now"). Read-only forensic surface.
- Per-message model recommendation. Session-level only.
- Rewriting attribution buckets. The thrash score is metadata; bucket invariant unchanged.
- NLP/LLM-driven user-intent inference. Heuristic regex only — no model calls in the indexer.
- Recommending Haiku for currently-Sonnet sessions (the inverse direction). Asymmetric scope: detect *under-modeling*, not *over-modeling*. Over-modeling detection deferred (different signal: short, simple, single-shot Opus turns).

### Charter alignment

- **No false precision.** Counterfactual is a range with explicit confidence (`low`, `mid`, `high` cost estimates + `n_calibration_events`). Never a point estimate.
- **Bucket invariant unaffected.** `thrash_score` lives in `session_rollups` as metadata; cost columns and the `Σ == Σ` invariant are untouched.
- **Evidence shown alongside flag.** Every flagged session surfaces *why* it was flagged (which signals fired, which file thrashed, which error repeated) so the user judges. No black-box "you wasted $X."
- **Calibration honesty.** Counterfactual is tiered: `low` (<15 events, wide range), `mid` (15–50 events), `high` (>50 events, tighter range). Below 10 events: suppressed entirely. Detection still surfaces; just no $ comparison.
- **Caveats explicit.** Counterfactual is "what user experienced when they did escalate," not "what would have happened if they'd started on Opus." Prompt cache priming, selection bias (escalated sessions skew toward harder problems), and resolution-marker noise are documented in §0.1; report renders a footer pointer to these. No literature precedent exists for cost-counterfactual debiasing in agentic LLM use — we are inventing it. Treat estimates as directional, not actuarial.

### 0.1 Caveats (counterfactual & detection limits)

These are surfaced in the report footer and CLI `--help` so users do not over-trust the numbers:

- **Cache-priming bias.** When user escalates mid-session, Opus sees the full primed context (large prompt cache hit). A from-scratch Opus session would price differently. Counterfactual mid estimate may understate Opus cost on equivalent fresh sessions.
- **Selection bias.** Escalation events come from sessions the user judged hard enough to switch. Sessions that thrash but never escalate may be a different difficulty distribution. Calibration is biased toward "Opus rescued the user," not "Opus would have done equivalently from start."
- **Resolution-marker noise.** "thanks/perfect/done" misses many real successes; `session_end` may indicate giving up rather than success. Misclassification of resolution → biased `turns_after_switch_to_resolution`.
- **Capability-router vs difficulty-escalator.** Subagent dispatch is *not always* an escalation: a Sonnet→Sonnet subagent invocation is capability routing (specialized prompt), not tier escalation. Spec only counts subagent dispatch as escalation when child model TIER is strictly greater than parent (e.g., Sonnet→Opus). Same-tier dispatches are excluded from calibration.
- **English-only correction regex.** `user_correction` signal silently misses non-English corrections. Documented limitation.

## 1. Architecture

```
JSONL (already indexed)
  │
  └─→ signal extractors (NEW: src/ccforensics/thrash.py)
        │
        ├─→ session_signals (NEW table: 1 row per (session, signal_type))
        ├─→ session_rollups.thrash_score      (NEW col: composite 0.0–1.0)
        └─→ session_rollups.escalation_event  (NEW col: JSON or NULL)

calibration (NEW: src/ccforensics/thrash_calibration.py)
  └─→ scans session_rollups.escalation_event across corpus
      → per-from-model avg turns_to_resolve_post_switch + cost-per-turn
      → returns CalibrationTable (in-memory; recomputed each report run)

reports/
  └── thrash.py (NEW)
        - lists sessions where thrash_score >= threshold
        - shows signal evidence per session
        - shows counterfactual range when calibration available
```

**Modules touched:**

- `index.py` — schema v4 migration; call `thrash.populate_session_signals` + score recompute after `attribution.recompute_session_rollups` for each touched session. Recompute is a SAME-transaction operation (no async/background) — keeps the model simple and matches existing reconcile behavior. If it becomes too slow at scale (perf budget below), gate via `WHERE thrash_score_version IS NULL OR thrash_score_version != :current_version OR session_id IN :touched_sessions`.
- `attribution.py` — no behavior change; spec invariant guarded by existing test.
- `thrash.py` — new module: signal extractors + composite scorer + escalation detector.
- `thrash_calibration.py` — new module: corpus-level escalation calibration table.
- `report/thrash.py` — new module: query + render.
- `cli.py` — new `thrash` command.
- `tests/` — new test files per §6.

**Modules untouched:** `tree.py`, `registry.py`, `skills.py`, `paths.py`, `pricing.py`, `jsonl.py`, all existing report commands.

## 2. Data model (schema v4)

### `session_rollups` — add columns

```sql
ALTER TABLE session_rollups ADD COLUMN thrash_score REAL;
ALTER TABLE session_rollups ADD COLUMN thrash_score_version INTEGER;
ALTER TABLE session_rollups ADD COLUMN escalation_event TEXT;  -- JSON or NULL
```

`thrash_score`: composite in `[0.0, 1.0]`, NULL until populated. NULL = pre-v4 row not yet recomputed OR session filtered out (Opus primary, <20 turns, etc.).

`thrash_score_version`: integer matching `thrash.SIGNAL_VERSION` constant in code. Increment on any change to thresholds, weights, or signal extractor logic. Report renders `[scored with v3]` and offers to recompute if mismatch detected. Aligns with industry observability practice (AgentOps, Langfuse, Braintrust all version flow+model+params for reproducibility).

`escalation_event`: JSON shape (NULL when no escalation):

```json
{
  "turn_index": 47,
  "from_model": "claude-sonnet-4-6",
  "to_model": "claude-opus-4-7",
  "escalation_kind": "model_switch",
  "turns_after_switch_to_resolution": 3,
  "cost_before_switch_usd": 1.84,
  "cost_after_switch_usd": 0.42,
  "wall_clock_before_seconds": 1240,
  "wall_clock_after_seconds": 95,
  "resolution_marker": "user_thanks",
  "subagent_prompt_excerpt": null
}
```

`escalation_kind` enum:

- `"model_switch"` — main session model changed mid-session from lower to higher tier
- `"subagent_dispatch"` — parent in lower tier spawns subagent in higher tier (only counted when tier strictly increases). `subagent_prompt_excerpt` populated with first 200 chars of the subagent prompt (audit trail; hierarchical-agent literature warns about information loss across delegation boundaries)
- `"auto_mode"` — session shows ≥3 model switches in either direction within first 20 turns; treat as auto-mode and tag separately. Excluded from calibration table (not a deliberate user escalation; routing is automated)

`turns_after_switch_to_resolution` is bounded by session end if no resolution marker fires.

`resolution_marker` enum: `"user_thanks"` | `"session_end"` | `"tool_success_no_followup"` | `"unresolved_within_window"`.

### New `session_signals` table

```sql
CREATE TABLE session_signals (
    session_id      TEXT NOT NULL,
    signal_type     TEXT NOT NULL,   -- see enum below
    count           INTEGER NOT NULL,
    evidence        TEXT NOT NULL,   -- JSON: signal-specific payload
    signal_version  INTEGER NOT NULL,  -- matches session_rollups.thrash_score_version at write time
    PRIMARY KEY (session_id, signal_type)
);
CREATE INDEX idx_signals_session ON session_signals(session_id);
```

`signal_type` enum:

- `repeated_edit` | `repeated_error` | `user_correction` | `tool_arg_churn`
- `novelty_window` | `turn_cost_acceleration` | `session_abandoned`
- `placeholder_emit` | `trajectory_length_zscore` | `test_regression`

`evidence` JSON shape varies by signal:

- `repeated_edit`: `{"file_path": "...", "edit_count": 7, "first_turn": 12, "last_turn": 31, "distinct_errors_during_window": 2}`
- `repeated_error`: `{"error_hash": "a3f9...", "error_excerpt": "AttributeError: 'NoneType'...", "occurrences": 5, "first_turn": 14, "last_turn": 28}`
- `user_correction`: `{"matches": ["no", "still broken", "try again"], "turn_indices": [16, 22, 27]}`
- `tool_arg_churn`: `{"tool_name": "Edit", "arg_hash": "...", "result_variation": false, "repeats": 4, "turn_indices": [12,15,18,22]}`
- `novelty_window`: `{"max_flat_run": 8, "from_turn": 20, "to_turn": 28, "new_files_in_window": 0, "new_errors_in_window": 0, "text_jaccard_max": 0.91}`
- `turn_cost_acceleration`: `{"slope_output_tokens_per_turn": 312, "window_start_turn": 15, "window_end_turn": 45, "r_squared": 0.72}` (note: `output_tokens` slope, not total cost — output is the model's actual contribution; input includes accumulated tool output that grows mechanically)
- `session_abandoned`: `{"total_turns": 67, "last_role": "assistant", "last_tool_error": true, "wall_clock_total_seconds": 4520}`
- `placeholder_emit`: `{"matches": ["TODO", "FIXME", "pass  # placeholder", "raise NotImplementedError"], "files": ["src/auth.py", "src/util.py"], "turn_indices": [22, 31]}`
- `trajectory_length_zscore`: `{"session_turns": 87, "user_baseline_mean_turns": 24, "user_baseline_stddev": 11, "z_score": 5.7, "primary_model": "claude-sonnet-4-6"}`
- `test_regression`: `{"tool_name": "Bash", "command_excerpt": "uv run pytest", "fail_count_before": 2, "fail_count_after": 7, "edit_between_turns": [34, 38]}`

`monotone_low_tier` is NOT stored as a signal row. It is a session-level filter: if the primary model is NOT `claude-(sonnet|haiku)-*` or total assistant turns < 20, the session is skipped before signal extraction runs. Stored in `session_rollups.thrash_score = NULL` (not `-1`; NULL means "not applicable").

### Cold-backfill mechanism

The v4 migration ends with:

```sql
UPDATE files SET mtime_ns = 0;
```

Same one-shot full-reconcile pattern as v2→v3. No new flag.

## 3. Signal extractors (`src/ccforensics/thrash.py`)

All extractors operate on already-parsed message rows for one session, returning `list[Signal]`. Each extractor is pure, deterministic, no I/O. Composite scorer combines counts with weights; thresholds in §5.

**Session filter (runs before any extractor):** if primary model is not `claude-(sonnet|haiku)-*` OR total assistant turns < 20, skip signal extraction entirely and write `thrash_score = NULL`. Rationale: literature confirms the routing problem is asymmetric (under-modeling vs over-modeling); Opus sessions and trivially short sessions are not candidates.

### `repeated_edit`

```python
def detect_repeated_edit(messages: list[Message], threshold: int = 4) -> list[Signal]:
    """File edited >threshold times within session, with intervening tool errors."""
```

- Scan `message_tool_uses` rows where `tool_name == 'Edit'` or `'Write'`.
- Group by edit target (`input.file_path`).
- Fire if same path edited ≥`threshold` times AND ≥1 tool result error (Bash non-zero, test failure marker) lands between edits.

The "intervening error" guard avoids flagging a normal multi-step refactor as thrash.

### `repeated_error`

```python
def detect_repeated_error(messages: list[Message], threshold: int = 3) -> list[Signal]:
    """Same error string surfaces in tool_result content >threshold times."""
```

- Scan `tool_result` blocks (already in `messages.content` per the parser).
- Extract the first error-shaped line: regex `(?im)^(?:Error|Traceback|FAIL|.*Error:|.*Exception:).*$`.
- Normalize: strip digits, hex strings (`\b[0-9a-f]{4,}\b`), absolute paths (`/[^\s]+`), timestamps (`\d{2}:\d{2}:\d{2}`), then lowercase and collapse whitespace.
- Take first 120 chars of normalized string → `sha256` → dedup key. Fire if any key appears ≥`threshold` times across distinct turns.

Normalization by prefix-hash (not full-string regex match) is robust to novel error types, non-Python stacks, and partial stack traces. First-120-char prefix captures the identifying prefix without sensitivity to line-number suffixes.

### `user_correction`

```python
def detect_user_correction(messages: list[Message], threshold: int = 2) -> list[Signal]:
    """Short user messages matching correction shape after assistant turn."""
```

- For each user message: token count <20 AND matches `re.compile(r"\b(no|nope|wrong|still|broken|try again|that'?s not|not (?:right|it)|doesn'?t work|didn'?t work|fix it|same (?:error|issue))\b", re.I)`.
- Fire if ≥`threshold` matches in session.
- Exclude the first user message of the session (initial prompt is not a correction).

### `tool_arg_churn`

```python
def detect_tool_arg_churn(messages: list[Message], threshold: int = 3) -> list[Signal]:
    """Same tool called with same args AND same result repeatedly (true churn, not flake retry)."""
```

- Hash `(tool_name, sha256(canonical_json(input)))` per `message_tool_uses` row.
- For each repeat: also hash the corresponding `tool_result` content (first 200 chars normalized).
- Fire only if same `(tool, args)` AND same result hash appear ≥`threshold` times.

The result-variation suppression avoids flagging legitimate retries: a Bash command that flakes (network blip, intermittent test) returns DIFFERENT output → not churn. Industry ReAct loop detection uses `(function_name, args)` action keys; we extend with result-hash to disambiguate retry-on-failure from true thrash.

### `novelty_window`

```python
def detect_novelty_window(messages: list[Message], window: int = 6, threshold: int = 2, jaccard_min: float = 0.85) -> list[Signal]:
    """N consecutive turns with no new unique file, tool result, error, or assistant text variation."""
```

- Maintain running sets: `seen_files` (Edit/Write targets), `seen_error_hashes` (from repeated_error normalization), `seen_tool_names`.
- Sliding window of `window` turns: count turns where none of the three sets grew.
- **Deep-debugging suppression:** within a candidate flat-window, compute pairwise Jaccard similarity over assistant text-block character-sets across consecutive turns. If max Jaccard < `jaccard_min`, treat as varied reasoning (legitimate hard work) and skip — the model is exploring different hypotheses even though file/tool/error sets haven't grown.
- `flat_run` = max consecutive flat turns after suppression. Fire if `flat_run >= window` AND flat run count >= `threshold`.

Formalizes the industry `no_progress_steps` metric (agent patterns research). More general than typed signals: catches stagnation even when the model tries varied-but-futile approaches. Jaccard suppression addresses the false-positive risk on legitimate deep debugging where the model is reasoning carefully in text without producing new tool effects.

### `turn_cost_acceleration`

```python
def detect_turn_cost_acceleration(messages: list[Message], min_turns: int = 15, r2_threshold: float = 0.55) -> list[Signal]:
    """Output-tokens-per-turn has positive slope — model compensating by generating more output."""
```

- Collect `(turn_index, output_tokens)` for all assistant messages. **Output tokens, not total cost** — output is what the model produced; input is dominated by accumulated tool output (file reads, grep results, prior turn context) which grows mechanically and confounds the signal.
- Fit linear regression over the last `min(len, 30)` turns using least squares.
- Fire if slope > 0 AND `r_squared >= r2_threshold`.
- Evidence: slope (tokens/turn), window, r².

Rationale: per industry signal (`tokens_per_task rising while quality is flat`), a model struggling with a task tends to produce longer, more exploratory generations. Output-token slope isolates the model's contribution from tool-output accumulation. Output tokens are also 3–10× more expensive than input, so this directly tracks the costly dimension. `r2_threshold` prevents false positives from natural session warm-up.

### `session_abandoned`

```python
def detect_session_abandoned(messages: list[Message], min_turns: int = 20) -> list[Signal]:
    """Session ended mid-task without resolution after many turns."""
```

- Fire if total assistant turns >= `min_turns` AND any of:
  - Last message role is `assistant` (no final user acknowledgement).
  - Last tool result in session is an error (non-zero exit, exception content).
  - Last user message does NOT match `\b(thanks|done|perfect|great|works|fixed|ok|got it|merged|shipped)\b`.
- Count = total turns (used for severity scaling).

Multi-turn research confirms: models make early wrong assumptions and don't self-recover. A long session that ends without resolution is a strong signal the model hit its ceiling. Complements the escalation detector — this fires when the user *didn't* escalate, they just gave up.

### `placeholder_emit`

```python
def detect_placeholder_emit(messages: list[Message], threshold: int = 2) -> list[Signal]:
    """Assistant emits TODO/FIXME/stub placeholder code — proxy for low-confidence completion."""
```

- Scan `Edit` and `Write` tool inputs for placeholder markers in `new_string` / `content`:
  - Regex: `(?im)^\s*(?:#|//|--|/\*)\s*(?:TODO|FIXME|XXX|HACK|STUB|placeholder)\b`
  - Python: `pass\s*#\s*(?:TODO|placeholder|stub)`, `raise\s+NotImplementedError`
  - JS/TS: `throw\s+new\s+Error\(['"]not\s+implemented`
  - Generic stub bodies: function body that is only `pass` / `return None` / `return` / `// TODO`
- Fire if total placeholder emissions across distinct turns ≥ `threshold`.
- Evidence: matched markers + file paths + turn indices.

Strong industry validation: code completion research formalizes placeholder generation as a low-confidence proxy ("From Guessing to Placeholding" — uncertainty-aware completion). Industry "theater detection audit" tools detect TODO/stub markers as code quality signals. Cheap, deterministic, non-invasive.

### `trajectory_length_zscore`

```python
def detect_trajectory_length_zscore(
    messages: list[Message],
    user_baseline: BaselineStats,  # mean + stddev for this user's per-model session lengths
    z_threshold: float = 2.0,
    min_baseline_n: int = 20,
) -> list[Signal]:
    """Session is anomalously long compared to user's baseline for this primary model."""
```

- Compute session assistant-turn count.
- Compare to per-user, per-primary-model baseline: mean + stddev of historical session length over the last 90 days excluding sessions in the current evaluation window (avoid self-reference).
- Fire if z-score ≥ `z_threshold` AND `len(baseline_sessions) >= min_baseline_n`.
- Evidence: session_turns, baseline mean, baseline stddev, z-score.

Validated by SWE-EVAL trajectory analysis: "patterns in trajectory length and variance correlate with failure modes." Personalizing to the user's baseline avoids penalizing users who routinely run long sessions. `min_baseline_n` prevents firing when baseline is too noisy.

### `test_regression`

```python
def detect_test_regression(messages: list[Message], threshold: int = 1) -> list[Signal]:
    """Test failure count rose after an Edit — model made it worse."""
```

- Find Bash invocations matching test runner heuristics (`pytest`, `npm test`, `jest`, `cargo test`, `go test`, `mvn test`, `make test`, `uv run pytest`).
- Parse fail count from output via regex per runner family:
  - pytest: `(\d+)\s+failed`
  - jest: `Tests:\s+(\d+)\s+failed`
  - go test: `FAIL\s+\S+`
  - generic fallback: count lines matching `(?im)^FAIL` or `(?im)^E\s`
- Track fail count across consecutive test runs in same session.
- Fire if fail count rises across two runs AND ≥1 Edit happened between them.
- Evidence: command excerpt, before/after fail counts, edit turn indices between.

Validated by SWE-bench's `fail2pass` / `pass2pass` framework: test-state regression after a code change is a recognized failure signal in academic SE evaluation. Parsing is best-effort; missed parses produce no signal (silent-fail for unknown runners is acceptable).

### Composite scorer

```python
SIGNAL_VERSION = 1  # bump on any threshold/weight/extractor change

WEIGHTS = {
    "novelty_window":            0.22,  # primary: absence-of-progress metric (industry-validated)
    "test_regression":           0.18,  # made-it-worse signal (SWE-bench fail2pass framework)
    "repeated_edit":             0.15,
    "repeated_error":            0.12,
    "placeholder_emit":          0.10,  # low-confidence completion proxy (validated)
    "user_correction":           0.08,
    "trajectory_length_zscore":  0.06,
    "tool_arg_churn":            0.05,
    "turn_cost_acceleration":    0.02,
    "session_abandoned":         0.02,
}

def thrash_score(signals: list[Signal]) -> float:
    """Composite in [0, 1]. Each fired signal contributes its full weight; counts beyond threshold add log-scaled bonus capped at 1.5×."""
    fired = {s.signal_type: s.count for s in signals}
    score = 0.0
    for sig_type, weight in WEIGHTS.items():
        if sig_type not in fired:
            continue
        count = fired[sig_type]
        threshold = THRESHOLDS[sig_type]
        # base + log-scaled bonus
        bonus = min(0.5, 0.1 * math.log2(max(1, count / threshold)))
        score += weight * (1.0 + bonus)
    return min(1.0, score)
```

`novelty_window` and `test_regression` are weighted highest: novelty_window is the most general stagnation indicator (industry-validated); test_regression is the most direct "model made it worse" signal (SWE-bench academic standard). Other typed signals provide human-readable evidence of *why* there is no novelty or *how* things got worse.

A session must fire ≥2 distinct signal types to score above the flag threshold (0.40). Single-signal sessions cap at 0.22 × 1.5 = 0.33 worst case, well below threshold; flag logic in §5 requires `len(fired_signals) >= 2`.

### Escalation event detector

```python
def detect_escalation(
    messages: list[Message],
    subagent_spawns: list[SubagentSpawn],
) -> EscalationEvent | None:
    """Find the first tier-escalation event in the session: model_switch | subagent_dispatch | auto_mode."""
```

Three escalation kinds, evaluated in priority order:

**1. `auto_mode` detection (runs first):**
- Count model switches in the first 20 assistant turns.
- If ≥3 switches in either direction → tag the session as `auto_mode`.
- Record the first switch as the escalation event with `escalation_kind="auto_mode"`. **Excluded from calibration table** (automated routing, not a deliberate user signal).

**2. `model_switch`:**
- Walk assistant messages in turn order.
- Find first index `i` where `tier(messages[i-1].model) < tier(messages[i].model)` (e.g., Sonnet→Opus, Haiku→Sonnet, Haiku→Opus).
- Tier function: `haiku < sonnet < opus`. Same-tier transitions (Sonnet 4.6 → Sonnet 4.7) NOT counted.
- Record `escalation_kind="model_switch"`.

**3. `subagent_dispatch`:**
- For each subagent spawn in the session: parent model = main session model at spawn turn; child model = subagent's primary model.
- Fire on first spawn where `tier(child) > tier(parent)`. Same-tier (Sonnet parent → Sonnet code-reviewer subagent) is **capability routing, not escalation** (hierarchical-agent literature framing) — not counted.
- Record `escalation_kind="subagent_dispatch"`, populate `subagent_prompt_excerpt` with first 200 chars of subagent prompt for audit trail.
- For cost/turns measurement: `cost_after_switch` includes the subagent's session cost (already attributed via `session_rollups`).

**Common to all kinds:**
- `cost_before_switch` = sum of `cost_usd` over `messages[:i]`.
- `cost_after_switch` = sum of `cost_usd` over `messages[i:]` (plus subagent cost when applicable).
- `wall_clock_before_seconds` / `wall_clock_after_seconds` from `messages[i].timestamp - messages[0].timestamp` and `messages[-1].timestamp - messages[i].timestamp`.
- `turns_after_switch_to_resolution`:
  - Look for resolution marker within the next 10 assistant turns: user message containing `\b(thanks|perfect|great|works|fixed|done|nice|merged|shipped)\b` (case-insensitive), OR session-end without further user input, OR a tool-success turn followed by no further tool calls.
  - If no marker fires within 10 turns, set to `min(10, turns_in_post_switch_window)` and tag `resolution_marker = "unresolved_within_window"`.
- Returns `None` if no escalation of any kind found.

Only the first escalation per session is recorded (subsequent escalations rare; first is the calibration signal).

## 4. Calibration and counterfactual estimation (`src/ccforensics/thrash_calibration.py`)

### Calibration table

Built fresh on each `thrash` report run (cheap; one query):

```sql
SELECT
  json_extract(escalation_event, '$.from_model') AS from_model,
  json_extract(escalation_event, '$.to_model')   AS to_model,
  AVG(json_extract(escalation_event, '$.turns_after_switch_to_resolution')) AS avg_turns_post,
  AVG(json_extract(escalation_event, '$.cost_after_switch_usd') /
      NULLIF(json_extract(escalation_event, '$.turns_after_switch_to_resolution'), 0)) AS avg_cost_per_post_turn,
  COUNT(*) AS n_events
FROM session_rollups
WHERE escalation_event IS NOT NULL
  AND json_extract(escalation_event, '$.escalation_kind') IN ('model_switch', 'subagent_dispatch')
GROUP BY from_model, to_model;
```

`auto_mode` events excluded — automated routing is not a deliberate human signal.

Returns `CalibrationTable[(from_model, to_model)] -> (avg_turns, avg_cost_per_turn, n_events)`.

### Counterfactual range per flagged session

For a session flagged with thrash but **no escalation event of its own** (i.e., user did not escalate; we're estimating what would have happened):

1. Look up calibration entry for `(observed_lower_model, claude-opus-4-7)`. If no entry, suppress counterfactual.
2. If `n_events < 10`, suppress counterfactual; show only detection.
3. Assign calibration confidence tier:
   - `low`: 10 ≤ n < 15 — range multipliers 0.33×–3.0× (very wide)
   - `mid`: 15 ≤ n < 50 — range multipliers 0.5×–2.0×
   - `high`: n ≥ 50 — range multipliers 0.67×–1.5× (tighter but still honest)
4. Compute:
   - `est_opus_cost_mid = avg_turns × avg_cost_per_turn`
   - `est_opus_cost_low = low_multiplier × est_opus_cost_mid`
   - `est_opus_cost_high = high_multiplier × est_opus_cost_mid`
   - `observed_cost = session_rollups.cost_usd_sum`
5. Render: `Observed: $1.84 · Opus est: $0.30–$1.20 (mid $0.60) · n=12 [mid confidence]`.

Multiplicative ranges reflect literature finding that small-N calibration (FrugalGPT, RouteLLM) misleads even at n=10–15. Range width narrows with corpus size. Wide-and-honest beats narrow-and-wrong. Future: percentile-based ranges (10th/90th of post-switch cost distribution) when n≥50 corpus data is available.

### Cost-sanity gate (per render, not per session)

Adapted from Triage paper's two-gate validation pattern (cost gate + signal gate). Applied at report-render time:

- For each flagged session, check: `0.1 * observed_cost <= est_cost_mid <= 10 * observed_cost`.
- If estimate falls outside this band: mark counterfactual as `[implausible — sanity-gate failed]` and suppress the dollar comparison for that session. Detection still surfaces.
- Aggregate metric: log fraction of sessions failing sanity gate. If >5% in a given report run: render headline footer warning ("calibration data may be drifting from your usage pattern").

This catches calibration table corruption (e.g., one tiny session with `turns_after_switch=1` and `cost=$0.01` becoming the outlier driving the average).

### Why escalation events are the ground truth

These are sessions where the user (or auto-mode) made the model switch and we *observed* the outcome. No assumption about Opus capability — we measured it on similar prior sessions. The estimator is corpus-local: a user who routinely escalates on the same problem class gets calibration data tuned to that class.

## 5. Report (`src/ccforensics/report/thrash.py`)

### CLI

```
ccforensics thrash [OPTIONS]

  --days N           scope to last N days (default 30; uses report._dates)
  --since DATE       absolute lower bound
  --until DATE       absolute upper bound
  --session ID       drill into one session — full evidence + escalation_event JSON
  --min-score F      threshold for flagging (default 0.40)
  --min-signals N    require at least N distinct signal types (default 2)
  --top N            keep top N flagged sessions by score (default 25)
  --sort COL         score (default) | observed_cost | est_savings_mid | est_savings_high
  --evidence         expand per-session signal evidence (default: collapsed)
  --json             JSON output (mutually exclusive with --csv)
  --csv              CSV output
  --no-refresh       skip the index refresh that runs by default
```

Default scope is 30 days to avoid surfacing thousands of historical sessions on first run.

### Default render — headline + collapsed table

Headline (always rendered first):

```
ccforensics thrash — last 30 days [scored with v1]
─────────────────────────────────────────────────────────────────
3 flagged sessions · 182 turns · $7.20 observed
Estimated Opus counterfactual: $0.70–$2.80 (mid $1.40)
Implied savings if escalated earlier: ~$4.40–$6.50 (mid $5.80)
```

Per-session table (sorted by score by default):

```
SESSION                          MODEL            SCORE  SIGNALS  TURNS  WALL  OBSERVED $  OPUS EST $ (mid)  CAL
────────────────────────────────  ──────────────  ─────  ───────  ─────  ────  ──────────  ────────────────  ─────
abc123ef · feat/parser-rewrite    sonnet-4-6      0.78   5        87     32m       4.21       $0.40-$1.60(0.80)  18m
def456gh · debug/auth-flow        sonnet-4-6      0.62   3        54     21m       2.15       $0.30-$1.20(0.60)  18m
ghi789ij · refactor/index         haiku-4-5       0.51   2        41     14m       0.84       insufficient cal    4l
```

`CAL` column: count + tier letter (`l`=low, `m`=mid, `h`=high). `WALL` = wall-clock duration from first to last message.

`--evidence` expands each session row to show:

```
abc123ef · feat/parser-rewrite — score 0.78 [sonnet-4-6, 87 turns, 32m wall-clock]
  ├─ novelty_window          (flat_run 8)   turns 20–28: no new files/errors/tools, text-jaccard 0.91
  ├─ test_regression         (count 1)      pytest fail count 2 → 7 across edits at turns 34, 38
  ├─ repeated_edit           (count 7)      src/parser.py — 7 edits between turns 12–31 (2 distinct errors)
  ├─ placeholder_emit        (count 3)      src/auth.py turn 22: `raise NotImplementedError`; src/util.py turn 31: `pass  # TODO`
  └─ user_correction         (count 3)      turns 16, 22, 27
  Counterfactual: $4.21 observed vs $0.40–$1.60 est Opus (mid $0.80, n=18 [mid confidence])
  Caveats: cache-priming + selection bias may understate Opus cost on cold-start equivalent. See `ccforensics thrash --help` for full caveats.
```

### Footer

Always rendered when calibration is sparse:

> Counterfactual estimates require ≥10 escalation events per (from_model → to_model) pair to render. Sessions tagged `insufficient cal` are still flagged; cost comparison is suppressed pending more calibration data.

### JSON shape

```json
{
  "headline": {
    "scope_days": 30,
    "scored_with_version": 1,
    "n_flagged": 3,
    "total_turns": 182,
    "total_observed_cost_usd": 7.20,
    "total_est_opus_cost_low_usd": 0.70,
    "total_est_opus_cost_mid_usd": 1.40,
    "total_est_opus_cost_high_usd": 2.80
  },
  "rows": [
    {
      "session_id": "...",
      "summary": "...",
      "primary_model": "...",
      "thrash_score": 0.78,
      "thrash_score_version": 1,
      "signals": [
        {"type": "repeated_edit", "count": 7, "evidence": {...}},
        ...
      ],
      "turns": 87,
      "wall_clock_seconds": 1920,
      "observed_cost_usd": 4.21,
      "counterfactual": {
        "to_model": "claude-opus-4-7",
        "est_cost_low_usd": 0.40,
        "est_cost_mid_usd": 0.80,
        "est_cost_high_usd": 1.60,
        "n_calibration_events": 18,
        "calibration_confidence": "mid",   // "low"|"mid"|"high"
        "sanity_gate_passed": true
      } | null
    }
  ],
  "calibration_table": [
    {"from_model": "...", "to_model": "...", "avg_turns_post": 3.2, "avg_cost_per_post_turn_usd": 0.25, "n_events": 18}
  ]
}
```

### Invariant

The thrash report does NOT participate in the bucket-attribution invariant. `thrash_score` and `escalation_event` are session metadata. Cost columns in the report are pulled directly from `session_rollups.cost_usd_sum` (existing) — no rederivation.

Spec invariant for the report itself:

> For every flagged session, `Σ(signals[].count for type in fired) >= 2` (composite-score gate). Verified in `tests/test_thrash_report.py`.

## 6. Testing, migration, rollout

### Schema migration (v3 → v4)

```python
# index.py MIGRATIONS list, appended:
[
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
]
```

`PRAGMA user_version = 4` after migration list applies.

### New test files

`tests/test_thrash_signals.py`:
- Per-extractor unit tests with synthetic message lists.
- `repeated_edit`: 5 edits to same file, no errors → no fire. 5 edits with ≥2 distinct errors interspersed → fire.
- `repeated_error`: same error 4 times → fire. Same error with line numbers / paths differing → still fire (normalization by prefix-hash works). Error in non-Python stack → fire (regex covers generic shapes).
- `user_correction`: 3 short corrective msgs → fire. 3 short greetings → no fire. First message excluded.
- `tool_arg_churn`: same Bash 4 times w/ same result → fire. Same Bash 4 times w/ alternating success/fail results → no fire (result-variation suppression). Different args → no fire.
- `novelty_window`: 8-turn window no new files/errors AND text-jaccard 0.91 → fire. Same window w/ text-jaccard 0.4 (varied reasoning) → no fire (deep-debug suppression). New file touched midway → no fire (window resets).
- `turn_cost_acceleration`: 20 turns with rising **output-token** slope (r²=0.8) → fire. 20 turns random output tokens → no fire (r² below threshold). Tool output growth alone (input tokens rise but output tokens flat) → no fire.
- `session_abandoned`: 25-turn session ending on assistant message with tool error → fire. 25-turn session ending with user "thanks" → no fire.
- `placeholder_emit`: Edit input contains `raise NotImplementedError` + `# TODO` across 2 turns → fire. Comments-only Edit (no stub markers) → no fire.
- `trajectory_length_zscore`: session 87 turns, baseline mean=24, stddev=11 → z=5.7 → fire. Same session, baseline n=10 (below `min_baseline_n`) → no fire (insufficient baseline). Same session, user's baseline mean=80 → no fire (within bounds).
- `test_regression`: pytest output "2 failed" then Edit then "7 failed" → fire. Output "2 failed" then "1 failed" → no fire. Test runner not recognized → silent no-fire.
- Session filter: Opus session → `thrash_score = NULL`, no signals computed. Sonnet session <20 turns → same.

`tests/test_thrash_score.py`:
- 1 signal fired → score < 0.40 (sub-flag, checked via `len(fired_signals) < 2` gate).
- 2 signals fired (novelty_window + repeated_edit, low counts) → score crosses 0.40.
- All 10 signals fired (high counts) → score saturates at 1.0.
- Bonus is log-scaled and capped (no signal can score > 1.5× its weight).
- `SIGNAL_VERSION` mismatch → `session_signals.signal_version != current` → triggers recompute on next `populate_session_signals` call.

`tests/test_escalation_detect.py`:
- Synthetic session: 30 Sonnet turns then Opus → `escalation_kind == "model_switch"`, event recorded.
- Session with 4 model bounces in first 20 turns → `escalation_kind == "auto_mode"`, event recorded but excluded from calibration query.
- Sonnet session spawns Opus subagent → `escalation_kind == "subagent_dispatch"`, `subagent_prompt_excerpt` populated.
- Sonnet session spawns Sonnet code-reviewer subagent → no escalation (capability routing, same tier).
- Same-tier model change (Sonnet 4.6 → Sonnet 4.7) → no escalation.
- Session with no model change and no subagent → returns None.
- Escalation followed by user "thanks" → `resolution_marker == "user_thanks"`.
- Escalation with no resolution marker in 10 turns → `resolution_marker == "unresolved_within_window"`.
- Cost-before/after match `messages.cost_usd` sums exactly. Subagent-dispatch case: cost_after includes subagent's session_rollups cost.
- `wall_clock_before_seconds` / `_after_seconds` match `timestamp` arithmetic.

`tests/test_thrash_calibration.py`:
- Empty corpus → calibration table empty, counterfactual suppressed.
- 9 escalation events for one (from, to) pair → suppressed (below 10 threshold).
- 12 events → `calibration_confidence = "low"`, multipliers 0.33×–3.0×.
- 20 events → `calibration_confidence = "mid"`, multipliers 0.5×–2.0×.
- 55 events → `calibration_confidence = "high"`, multipliers 0.67×–1.5×.
- Known turns/costs → exact `avg_turns` and `avg_cost_per_turn` arithmetic.
- Calibration query excludes `escalation_kind = 'auto_mode'` events.
- Cost-sanity gate: session with $4.21 observed and $0.30 estimated → passes (within 0.1×–10× band). Session with $0.10 observed and $20 estimated → `sanity_gate_passed = false`, counterfactual suppressed for that row.
- Aggregate gate: report run where >5% of flagged sessions fail sanity gate → headline footer warning rendered.

`tests/test_thrash_report.py`:
- Fixture: 5 sessions, 2 flagged (mixed signals), 1 with own escalation event, 2 clean.
- Assert flagged set matches expected.
- Assert `--evidence` renders all fired signals.
- Assert counterfactual range respects 0.5×/2× multipliers.
- Assert footer warning fires when calibration <10.
- Assert JSON shape matches §5.

### Pre-merge validation spike — DEFERRED (post-v1 follow-up)

**Status:** deferred. v1 ships with intuition-tuned thresholds + the `thrash` CLI command flagged in `--help` as "thresholds are intuition-tuned; run with `--evidence` and judge for yourself."

**When to do it:** if v1 corpus run produces noisy results (>30% of flagged sessions look like false positives on inspection), or before promoting `thrash` from "useful diagnostic" to "actionable cost recommendation."

**What it would look like:**

`scripts/thrash_label_eval.py` (one-shot, not committed-test):
- Hand-label 20 sessions from real corpus as `thrash` / `not_thrash` (`tests/fixtures/labels/thrash_labels.json`).
- Run detector at default thresholds; compute confusion matrix, precision, recall, F1.
- **Two-gate pass criteria** (Triage paper pattern, adapted):
  - **Signal gate:** precision ≥ 0.7 on labeled set.
  - **Cost-sanity gate:** ≥ 95% of flagged sessions have counterfactual within 0.1×–10× of observed cost.
- Tune thresholds until both gates pass; then bump `SIGNAL_VERSION` and document new defaults.

`tests/test_thrash_validation.py` (committed when spike runs):
- Asserts presence of labeled fixture file at `tests/fixtures/labels/thrash_labels.json`.
- Asserts both gates pass on the labeled corpus (regression guard against future threshold drift).

Tracked as future TODO. v1 risk acknowledged: bad defaults may erode trust on first user run.

### Existing tests touched

`tests/test_attribution.py`:
- Add assertion: bucket invariant `Σ(session_rollups.cost_usd) == Σ(messages.cost_usd)` still holds after schema v4 migration.

`tests/test_index.py` (or equivalent migration test):
- Assert v3 → v4 migration adds columns, creates table, sets `user_version = 4`, triggers `mtime_ns = 0` reset.

### Real-data fixture

`tests/fixtures/real/redacted_session.jsonl`:
- Existing fixture should already exhibit some signals; confirm at least one extractor fires on real data so the test isn't synthetic-only.
- If the existing fixture is too clean, capture and redact a fresh session known to thrash (manual selection).

### CI gates

- `uv run pytest --cov=ccforensics --cov-report=term-missing --cov-fail-under=85` — new code targets ≥85%.
- `uv run mypy src/` — strict.
- `uv run ruff check` and `uv run ruff format --check` clean.

### Dependencies

No new dependencies. Stdlib `re`, `math`, `json`, `hashlib`.

### Rollout

- README addition: `thrash` command example, screenshot of evidence-expanded output, note about calibration-event minimum.
- CHANGELOG entry under `[Unreleased]`:
  - Added: `thrash` command (model-misuse detection with signal evidence and escalation-anchored counterfactual cost range).
  - Added: `session_rollups.thrash_score`, `session_rollups.thrash_score_version`, `session_rollups.escalation_event`, `session_signals` table (10 signal types + low-tier session filter + escalation kinds: model_switch / subagent_dispatch / auto_mode).
  - Schema: v3 → v4 migration runs automatically; first command after upgrade triggers full re-reconcile (one-shot).

### No deprecations, no breaking CLI changes.

## 7. Open questions for review

1. **Threshold defaults** (§3): pulled from intuition. Validation spike (§6) is the answer — block merge until labeled set passes precision gate. Defaults will be tuned during the spike.
2. **`novelty_window` window size** (§3): window=6 turns is intuition. Industry guardrails use `max_flat_steps=4` (shorter). Will be tuned during validation spike.
3. **`turn_cost_acceleration` r² threshold** (§3): 0.55 is arbitrary. Tune during validation. Consider rolling 10-turn window if full-session regression is too noisy.
4. **`placeholder_emit` false positives on legitimate stub-then-fill workflow** (§3): a developer asking "scaffold the function signatures, I'll fill them in" gets flagged. Mitigation: low weight (0.10) and require co-fire. Consider suppressing if user message in same turn or prior contains `\b(scaffold|stub|placeholder|sketch|skeleton)\b`.
5. **`test_regression` test runner detection coverage** (§3): pytest, jest, go test, cargo test, mvn test covered; tox, nose, mocha, deno test, vitest not covered. Add as users surface gaps.
6. **`trajectory_length_zscore` baseline cold-start** (§3): a new user has no baseline → signal silently doesn't fire (`min_baseline_n` gate). Acceptable, but may want to fall back to a corpus-wide baseline after some threshold. Defer.
7. **Auto-mode threshold** (§3): "≥3 switches in first 20 turns" is a heuristic. Auto-mode behavior may shift across Claude Code versions. Worth re-validating if Anthropic publishes auto-mode internals.
8. **Subagent escalation cost attribution** (§3): subagent's `session_rollups.cost_usd_sum` is added to `cost_after_switch`. Subagent's own thrash_score (if it has one) is independent. Acceptable — subagent is its own session for cost-attribution purposes.
9. **Cache-priming counterfactual bias** (§0.1): no debiasing technique known. Documented as caveat. Acceptable for v1; revisit if a technique emerges.

## 8. Future directions (out of scope for v1)

### Code-health routing signal (Triage paper)

The Triage framework (arXiv 2604.07494) repurposes CodeHealth metrics (cyclomatic complexity, coupling, duplication, file churn from git) as pre-task model-selection signals: clean code (CodeHealth ≥ 9) → cheaper model safe; unhealthy code → frontier model required. This is complementary to ccforensics' post-hoc forensics.

Future addition: after the thrash pipeline, enrich `session_signals` with a `code_health_context` row that records the min CodeHealth score across files touched in the session (computed from a static analysis tool + `git log` for churn). This would surface whether thrash sessions correlate with low-CodeHealth targets — not a routing gate, but a diagnostic.

Not in v1 because: (a) requires integrating a static analysis tool (radon/lizard) and git at index time — outside current JSONL-only data scope; (b) Triage paper itself notes Claude Code's agentic mode shows no significant code-health effect (only medium-sized models benefit), suggesting the signal may be irrelevant for our primary use case.

### Edit-revert detection

Edit X then Edit X back to (approximately) prior content within N turns = explicit "model is lost." Detectable via `args_size_bytes` + content diff over `message_tool_uses`. Aligns with ReAct loop-detection literature ("track previous actions, alert on repeat"). Adds modest complexity (need to read prior file content from earlier `Read` results or git blame). Defer to v2.

### Over-modeling detection (inverse direction)

Short, simple, single-shot Opus sessions where Sonnet would have been cheaper and equally capable. Inverse of thrash; different signal set: ≤5 turns, single tool call, low output-token count, immediate user confirmation, no errors. Deferred to maintain asymmetric scope.

### Percentile-based counterfactual ranges

When corpus reaches n≥50 escalation events per (from, to) pair: replace multiplicative confidence tiers with empirical 10th/90th percentile of post-switch cost-per-turn distribution. More honest about the long tail; requires sufficient labeled data.
