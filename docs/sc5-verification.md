# SC5 Verification — `session --list`

**Date:** 2026-04-22
**Tester:** jason@lixfeld.ca
**Corpus:** `~/.claude/projects/` on dev machine. At test time: ~12,000 sessions,
summary_source breakdown `claude-summary: 1013`, `first-prompt: 11285`, `none: 72`.
**Build:** `feature/initial-implementation` @ HEAD (M4.1–M4.4 + M4.4.5 pydantic fix
landed; M4.5 CLI wiring about to commit).

## SC5 target

> Re-finding a remembered past session (scan + identify) should complete in
> ≤30 seconds end-to-end.

## Method

Picked a session from memory: the previous Claude Code session on this
ccforensics feature branch where I kicked off M4 execution via
`subagent-driven-development` against the plan at
`docs/plans/2026-04-21-initial-implementation.md`. Distinct enough that I'd
expect the first-prompt summary to contain "initial-implementation" or
"subagent-driven-development".

Terminal width: 200 cols (via `COLUMNS=200`). Actual tty was 80 but the
summary column at 80 cols is unreadable — fold-wrapping makes each summary
span ~30 rows. Noted below under "surprises".

## Results

### 1. Scan — top 20 by last-active (includes reconcile)

```
$ time COLUMNS=200 uv run ccforensics session list --sort last-active --limit 20
```

- Wall clock: **0.29s** (0.18s user + 0.10s sys).
- Reconcile on ~12,000 sessions: near-instantaneous because every file
  was (mtime_ns, size)-unchanged since the last rebuild — incremental
  path works.
- Identified the target row by eye on the first screen:
  - `b3e848  ccforensics  2026-04-21 23:30  12h52m  405  $53.28  execute the plan at docs/plans/2026-04-21-initial-implementation.md using subagent-driven-development.`
  - Identification time: **~2s** of visual scan (row 3).

### 2. Scan-time grep — by summary substring

```
$ time COLUMNS=200 uv run ccforensics session list --no-refresh --grep "initial-implementation"
```

- Wall clock: **0.12s**.
- Returned exactly 2 rows — the current session (`eb7190`) and the M4
  execution session (`b3e848`). No false positives.
- Identification time: **<1s**; result set was unambiguous.

### 3. Project-level narrowing

```
$ time COLUMNS=200 uv run ccforensics session list --no-refresh --project ccforensics --limit 10
```

- Wall clock: **0.11s**.
- Returned 3 rows — same two above plus one with summary `"a"`
  (zero-value typo prompt from `c93bc8`). Useful as a "show me every
  ccforensics session" view; the noisy row is a real data point, not a
  tool defect.

### Timing summary

| Step                                 |   Time |
|--------------------------------------|-------:|
| Scan top-20 + reconcile              |  0.29s |
| Visual identification from top-20    |  ~2s   |
| `--grep initial-implementation`      |  0.12s |
| `--project ccforensics`              |  0.11s |
| End-to-end (scan + identify)         |  ~3s   |

## SC5 verdict: **PASS** — 3 seconds end-to-end, well under the 30s budget.

## Observations / surprises

### Summary quality is mostly good

Walking the top 20 rows, every summary I inspected was either a real
first-prompt or a legitimate Claude-emitted compact summary. The
`first-prompt` fallback does its job: sanitization strips command
wrappers cleanly, hook-bootstrap blobs are correctly filtered, and
summaries I recognize match the sessions I remember.

### Summary gotchas (not blocking, but worth noting)

- **Single-character prompts make useless summaries.** Session `c93bc8`
  has `summary_text='a'` — my own typo/quick-test when I was poking at
  something. Not a tool bug; the schema faithfully records what I
  wrote. A user wanting to re-find that session can still identify it
  by `(project, started_at, cost)`.
- **`<local-command-stdout>Set model to ...</local-command-stdout>` landed
  as a first-prompt summary** for session `65c788`. Mechanism: the user
  invoked a slash-command whose output Claude Code surfaces as a user
  message, and `_sanitize_prompt` only strips `<command-name>` /
  `<command-message>` / `<command-args>`, not `<local-command-stdout>`.
  This is a real but minor summary-extraction gap — fixable in a future
  sweep by extending the wrapper regex. Not in scope for M4.5.
- **Continuation-of-previous-conversation prompts dominate row space** —
  several `mediamcp-refactor` rows show the same "This session is being
  continued from a previous conversation..." preamble. That's real
  Claude Code behavior (context auto-compact), not summarizer error.
  Could be collapsed to a shorter prefix in a UX polish pass; not
  scoped to M4.5.
- **`--grep "ccforensics"` returned zero rows** against summary_text.
  Rationale: most ccforensics-project summaries are phrased as
  "execute the plan at docs/plans/..." — the project name itself
  doesn't appear in the summary. Users intending to filter by project
  should use `--project`, which hits `project_path`. This is the
  designed behavior; documenting it here so future debugging doesn't
  re-discover it as a "bug".

### Output width (real UX concern, out of scope)

At a default 80-col terminal, `rich` wraps the Summary column down to
~3 chars wide, making each summary span ~30 rows. The table is
effectively unreadable piped or at 80 cols. Mitigations:

- `COLUMNS=200 ccforensics session list ...` works fine.
- `--json` / `--csv` are always width-independent and work cleanly
  for piped consumers.
- A render-side fix (e.g. truncate summary to a fixed width in narrow
  terminals, or detect non-TTY and emit a plain-text simpler form)
  belongs in a follow-up M4-polish task — noted but not blocking SC5
  or M4 exit criteria.

## Coverage / tests

- `tests/test_cli.py`: 12 integration tests (new), full session-list
  surface (no-sessions, JSON, CSV, grep, project, since/until,
  sort/reverse/limit, table render, refresh vs --no-refresh,
  pricing-fetch skipping).
- Full suite: 187 passed, 2 skipped (real-corpus smoke tests).
- Overall coverage: **95%** (cli.py: 92%).
- `uv run ruff check src/ tests/`: clean.
- `uv run ruff format --check src/ tests/test_cli.py`: clean.
  (Pre-existing drift in `tests/test_models.py` from M4.4.5 is noted but
  unrelated to M4.5 — will be handled separately.)
- `uv run mypy src/`: clean.
