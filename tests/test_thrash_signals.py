"""Unit tests for thrash signal extractors (T1: filter + placeholder_emit
+ repeated_edit + repeated_error). Synthetic ``TranscriptEntry`` lists
are built via the same _user / _assistant helpers used elsewhere, then
piped through ``parse_entry`` so tests exercise the same code path as
production parsing.
"""

from __future__ import annotations

from typing import Any

from ccforensics.models import TranscriptEntry, parse_entry
from ccforensics.thrash import (
    SIGNAL_VERSION,
    BaselineStats,
    assistant_turn_count,
    detect_novelty_window,
    detect_placeholder_emit,
    detect_repeated_edit,
    detect_repeated_error,
    detect_session_abandoned,
    detect_test_regression,
    detect_tool_arg_churn,
    detect_trajectory_length_zscore,
    detect_turn_cost_acceleration,
    detect_user_correction,
    primary_model,
    session_eligible,
)

# ---------- helpers ----------


def _user(
    uuid: str,
    ts: str,
    text: str | list[dict[str, Any]] = "ok",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": "sess",
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
        "message": {"role": "user", "content": text},
        **extra,
    }


def _assistant(
    uuid: str,
    ts: str,
    *,
    model: str = "claude-sonnet-4-5-20250929",
    content: list[dict[str, Any]] | None = None,
    msg_id: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "sess",
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
        "requestId": f"r-{uuid}",
        "message": {
            "id": msg_id or f"m-{uuid}",
            "role": "assistant",
            "model": model,
            "content": content or [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _tool_use(name: str, tool_id: str, **input_kwargs: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": input_kwargs}


def _tool_result(tool_use_id: str, content: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }


def _parse(raw: list[dict[str, Any]]) -> list[TranscriptEntry]:
    return [parse_entry(r) for r in raw]


def _ts(i: int) -> str:
    """Return an ISO timestamp ``i`` minutes after a fixed start. Rolls
    over to subsequent hours when ``i >= 60`` so callers can use larger
    indices for long-session fixtures."""
    hour = 10 + i // 60
    minute = i % 60
    return f"2026-04-22T{hour:02d}:{minute:02d}:00Z"


def _bulk_assistant(
    n: int, *, model: str = "claude-sonnet-4-5-20250929", start: int = 0
) -> list[dict[str, Any]]:
    return [_assistant(f"u{start + i}", _ts(start + i), model=model) for i in range(n)]


# ---------- filter ----------


def test_primary_model_picks_majority() -> None:
    entries = _parse(
        [
            _assistant("a1", _ts(1), model="claude-sonnet-4-6"),
            _assistant("a2", _ts(2), model="claude-sonnet-4-6"),
            _assistant("a3", _ts(3), model="claude-opus-4-7"),
        ]
    )
    assert primary_model(entries) == "claude-sonnet-4-6"


def test_primary_model_returns_none_when_no_assistant() -> None:
    entries = _parse([_user("u1", _ts(1), "hi")])
    assert primary_model(entries) is None


def test_assistant_turn_count_excludes_user_turns() -> None:
    entries = _parse(
        [
            _user("u0", _ts(0), "hi"),
            _assistant("a1", _ts(1)),
            _user("u2", _ts(2), "more"),
            _assistant("a3", _ts(3)),
        ]
    )
    assert assistant_turn_count(entries) == 2


def test_session_eligible_true_for_long_sonnet_session() -> None:
    raw = _bulk_assistant(20, model="claude-sonnet-4-6")
    entries = _parse(raw)
    assert session_eligible(entries) is True


def test_session_eligible_false_for_opus() -> None:
    raw = _bulk_assistant(40, model="claude-opus-4-7")
    entries = _parse(raw)
    assert session_eligible(entries) is False


def test_session_eligible_false_for_short_sonnet_session() -> None:
    raw = _bulk_assistant(10, model="claude-sonnet-4-6")
    entries = _parse(raw)
    assert session_eligible(entries) is False


def test_session_eligible_true_for_haiku() -> None:
    raw = _bulk_assistant(25, model="claude-haiku-4-5")
    entries = _parse(raw)
    assert session_eligible(entries) is True


# ---------- placeholder_emit ----------


def test_placeholder_emit_fires_on_todo_and_notimplemented() -> None:
    entries = _parse(
        [
            _assistant(
                "a1",
                _ts(1),
                content=[
                    _tool_use(
                        "Edit",
                        "t1",
                        file_path="/x/foo.py",
                        new_string="def foo():\n    raise NotImplementedError\n",
                    )
                ],
            ),
            _assistant(
                "a2",
                _ts(2),
                content=[
                    _tool_use(
                        "Write",
                        "t2",
                        file_path="/x/bar.py",
                        content="def bar():\n    pass  # TODO: implement\n",
                    )
                ],
            ),
        ]
    )
    signals = detect_placeholder_emit(entries)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "placeholder_emit"
    assert sig.count == 2
    assert "/x/foo.py" in sig.evidence["files"]
    assert "/x/bar.py" in sig.evidence["files"]


def test_placeholder_emit_below_threshold_does_not_fire() -> None:
    entries = _parse(
        [
            _assistant(
                "a1",
                _ts(1),
                content=[
                    _tool_use(
                        "Edit",
                        "t1",
                        file_path="/x/foo.py",
                        new_string="raise NotImplementedError",
                    )
                ],
            ),
        ]
    )
    assert detect_placeholder_emit(entries) == []


def test_placeholder_emit_ignores_non_edit_writes() -> None:
    entries = _parse(
        [
            _assistant(
                "a1",
                _ts(1),
                content=[_tool_use("Bash", "t1", command="echo TODO")],
            ),
            _assistant(
                "a2",
                _ts(2),
                content=[_tool_use("Bash", "t2", command="echo FIXME")],
            ),
        ]
    )
    assert detect_placeholder_emit(entries) == []


def test_placeholder_emit_ignores_clean_code() -> None:
    entries = _parse(
        [
            _assistant(
                "a1",
                _ts(1),
                content=[
                    _tool_use(
                        "Edit",
                        "t1",
                        file_path="/x/foo.py",
                        new_string="def foo():\n    return 42\n",
                    )
                ],
            ),
            _assistant(
                "a2",
                _ts(2),
                content=[
                    _tool_use(
                        "Edit",
                        "t2",
                        file_path="/x/bar.py",
                        new_string="def bar():\n    return 7\n",
                    )
                ],
            ),
        ]
    )
    assert detect_placeholder_emit(entries) == []


# ---------- repeated_edit ----------


def _edit_session_with_errors(*, edits_to_path: int, distinct_errors: int) -> list[TranscriptEntry]:
    """Build a session with N edits to /x/foo.py interleaved with M
    distinct tool_result errors (each error appears at least once)."""
    raw: list[dict[str, Any]] = []
    error_msgs = [f"AttributeError: object has no attribute '{c}'" for c in "abcdefgh"][
        :distinct_errors
    ]
    ts_idx = 0
    for i in range(edits_to_path):
        raw.append(
            _assistant(
                f"a-edit{i}",
                _ts(ts_idx),
                content=[
                    _tool_use(
                        "Edit",
                        f"te{i}",
                        file_path="/x/foo.py",
                        new_string=f"v{i}",
                    )
                ],
            )
        )
        ts_idx += 1
        if i < len(error_msgs):
            raw.append(
                _user(
                    f"u-err{i}",
                    _ts(ts_idx),
                    text=[_tool_result(f"te{i}", error_msgs[i])],
                )
            )
            ts_idx += 1
    return _parse(raw)


def test_repeated_edit_fires_with_errors_interspersed() -> None:
    entries = _edit_session_with_errors(edits_to_path=5, distinct_errors=2)
    signals = detect_repeated_edit(entries)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "repeated_edit"
    assert sig.count == 5
    assert sig.evidence["file_path"] == "/x/foo.py"
    assert sig.evidence["distinct_errors_during_window"] >= 2


def test_repeated_edit_no_errors_does_not_fire() -> None:
    """Five legitimate edits to a single file w/ no errors looks like
    iterative refactor, not thrash."""
    entries = _edit_session_with_errors(edits_to_path=5, distinct_errors=0)
    assert detect_repeated_edit(entries) == []


def test_repeated_edit_below_threshold_does_not_fire() -> None:
    entries = _edit_session_with_errors(edits_to_path=3, distinct_errors=2)
    assert detect_repeated_edit(entries) == []


def test_repeated_edit_only_one_distinct_error_does_not_fire() -> None:
    """One error type repeating across the edit window doesn't satisfy
    the min_distinct_errors guard — that's classic transient breakage,
    not multiple things going wrong."""
    raw: list[dict[str, Any]] = []
    ts_idx = 0
    for i in range(5):
        raw.append(
            _assistant(
                f"a{i}",
                _ts(ts_idx),
                content=[_tool_use("Edit", f"te{i}", file_path="/x/foo.py", new_string=f"v{i}")],
            )
        )
        ts_idx += 1
        raw.append(
            _user(
                f"u-err{i}",
                _ts(ts_idx),
                text=[_tool_result(f"te{i}", "AttributeError: x has no attr y")],
            )
        )
        ts_idx += 1
    entries = _parse(raw)
    assert detect_repeated_edit(entries) == []


# ---------- repeated_error ----------


def test_repeated_error_normalizes_line_numbers_and_paths() -> None:
    """Same logical error w/ varying line numbers + paths should still
    dedup to one excerpt."""
    raw: list[dict[str, Any]] = []
    errors = [
        "AttributeError: 'NoneType' object has no attribute 'split'\n  File '/a/b.py', line 12",
        "AttributeError: 'NoneType' object has no attribute 'split'\n  File '/a/c.py', line 47",
        "AttributeError: 'NoneType' object has no attribute 'split'\n  File '/a/d.py', line 999",
    ]
    ts_idx = 0
    for i, err in enumerate(errors):
        raw.append(
            _assistant(
                f"a{i}",
                _ts(ts_idx),
                content=[_tool_use("Bash", f"tb{i}", command="pytest")],
            )
        )
        ts_idx += 1
        raw.append(_user(f"u{i}", _ts(ts_idx), text=[_tool_result(f"tb{i}", err)]))
        ts_idx += 1
    entries = _parse(raw)
    signals = detect_repeated_error(entries)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "repeated_error"
    assert sig.count == 3
    assert "AttributeError" in sig.evidence["error_excerpt"]


def test_repeated_error_below_threshold_does_not_fire() -> None:
    raw: list[dict[str, Any]] = []
    raw.append(
        _assistant(
            "a1",
            _ts(0),
            content=[_tool_use("Bash", "tb1", command="pytest")],
        )
    )
    raw.append(_user("u1", _ts(1), text=[_tool_result("tb1", "Error: foo")]))
    raw.append(
        _assistant(
            "a2",
            _ts(2),
            content=[_tool_use("Bash", "tb2", command="pytest")],
        )
    )
    raw.append(_user("u2", _ts(3), text=[_tool_result("tb2", "Error: foo")]))
    entries = _parse(raw)
    assert detect_repeated_error(entries) == []


def test_repeated_error_ignores_clean_results() -> None:
    raw: list[dict[str, Any]] = []
    for i in range(5):
        raw.append(
            _assistant(
                f"a{i}",
                _ts(i * 2),
                content=[_tool_use("Bash", f"tb{i}", command="pytest")],
            )
        )
        raw.append(
            _user(
                f"u{i}",
                _ts(i * 2 + 1),
                text=[_tool_result(f"tb{i}", "5 passed in 0.12s")],
            )
        )
    entries = _parse(raw)
    assert detect_repeated_error(entries) == []


# ---------- versioning constant ----------


def test_signal_version_is_positive_int() -> None:
    """Version constant must exist + be int. Bumping it is a deliberate
    code change; this test simply pins the type contract."""
    assert isinstance(SIGNAL_VERSION, int)
    assert SIGNAL_VERSION >= 1


# ---------- tool_arg_churn ----------


def _bash_run(turn_idx: int, tool_id: str, command: str, result: str) -> list[dict[str, Any]]:
    return [
        _assistant(
            f"a-bash-{turn_idx}",
            _ts(turn_idx * 2),
            content=[_tool_use("Bash", tool_id, command=command)],
        ),
        _user(
            f"u-bash-{turn_idx}",
            _ts(turn_idx * 2 + 1),
            text=[_tool_result(tool_id, result)],
        ),
    ]


def test_tool_arg_churn_fires_on_identical_repeated_call_and_result() -> None:
    raw: list[dict[str, Any]] = []
    for i in range(4):
        raw.extend(_bash_run(i, f"tb{i}", "ls /nope", "Error: No such file or directory"))
    entries = _parse(raw)
    signals = detect_tool_arg_churn(entries)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "tool_arg_churn"
    assert sig.count == 4
    assert sig.evidence["result_variation"] is False


def test_tool_arg_churn_suppressed_when_results_vary() -> None:
    """Same args, alternating success/failure → flake retry, not churn."""
    raw: list[dict[str, Any]] = []
    for i, result in enumerate(["pass", "Error: foo", "pass", "Error: foo"]):
        raw.extend(_bash_run(i, f"tb{i}", "pytest", result))
    entries = _parse(raw)
    assert detect_tool_arg_churn(entries) == []


def test_tool_arg_churn_does_not_fire_below_threshold() -> None:
    raw: list[dict[str, Any]] = []
    for i in range(2):
        raw.extend(_bash_run(i, f"tb{i}", "ls /nope", "Error"))
    entries = _parse(raw)
    assert detect_tool_arg_churn(entries) == []


def test_tool_arg_churn_does_not_fire_when_args_differ() -> None:
    raw: list[dict[str, Any]] = []
    for i in range(4):
        raw.extend(_bash_run(i, f"tb{i}", f"ls /opt/{i}", "Error"))
    entries = _parse(raw)
    assert detect_tool_arg_churn(entries) == []


# ---------- user_correction ----------


def test_user_correction_fires_on_short_corrective_messages() -> None:
    entries = _parse(
        [
            _user("u0", _ts(0), "Initial prompt — do something"),
            _assistant("a1", _ts(1)),
            _user("u2", _ts(2), "no, that's wrong"),
            _assistant("a3", _ts(3)),
            _user("u4", _ts(4), "still broken"),
            _assistant("a5", _ts(5)),
            _user("u6", _ts(6), "try again"),
        ]
    )
    signals = detect_user_correction(entries)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "user_correction"
    assert sig.count == 3
    assert sig.evidence["turn_indices"] == [1, 2, 3]


def test_user_correction_excludes_first_user_message() -> None:
    entries = _parse(
        [
            _user("u0", _ts(0), "no this is wrong"),  # first prompt; ignored
            _assistant("a1", _ts(1)),
            _user("u2", _ts(2), "still broken"),
        ]
    )
    assert detect_user_correction(entries) == []


def test_user_correction_does_not_fire_on_long_messages() -> None:
    long_text = "okay so the previous attempt did not work because " * 5
    entries = _parse(
        [
            _user("u0", _ts(0), "init"),
            _assistant("a1", _ts(1)),
            _user("u2", _ts(2), long_text),
            _assistant("a3", _ts(3)),
            _user("u4", _ts(4), long_text),
        ]
    )
    assert detect_user_correction(entries) == []


def test_user_correction_does_not_fire_on_greetings() -> None:
    entries = _parse(
        [
            _user("u0", _ts(0), "init"),
            _assistant("a1", _ts(1)),
            _user("u2", _ts(2), "thanks!"),
            _assistant("a3", _ts(3)),
            _user("u4", _ts(4), "great"),
        ]
    )
    assert detect_user_correction(entries) == []


# ---------- session_abandoned ----------


def _long_session(
    n_turns: int,
    *,
    final_role: str = "assistant",
    final_user_text: str = "thanks",
    final_tool_error: bool = False,
) -> list[TranscriptEntry]:
    raw: list[dict[str, Any]] = [_user("u-init", _ts(0), "go")]
    ts_idx = 1
    for i in range(n_turns):
        raw.append(_assistant(f"a{i}", _ts(ts_idx)))
        ts_idx += 1
    if final_role == "user":
        if final_tool_error:
            raw.append(_user("u-end", _ts(ts_idx), text=[_tool_result("t-x", "Error: boom")]))
        else:
            raw.append(_user("u-end", _ts(ts_idx), text=final_user_text))
    return _parse(raw)


def test_session_abandoned_fires_when_last_role_is_assistant() -> None:
    entries = _long_session(25, final_role="assistant")
    signals = detect_session_abandoned(entries)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "session_abandoned"
    assert sig.evidence["last_role"] == "assistant"
    assert sig.evidence["total_turns"] == 25


def test_session_abandoned_fires_on_final_tool_error() -> None:
    entries = _long_session(25, final_role="user", final_tool_error=True)
    signals = detect_session_abandoned(entries)
    assert len(signals) == 1
    assert signals[0].evidence["last_tool_error"] is True


def test_session_abandoned_does_not_fire_on_thanks() -> None:
    entries = _long_session(25, final_role="user", final_user_text="thanks, that worked")
    assert detect_session_abandoned(entries) == []


def test_session_abandoned_does_not_fire_below_min_turns() -> None:
    entries = _long_session(5, final_role="assistant")
    assert detect_session_abandoned(entries) == []


# ---------- novelty_window ----------


def test_novelty_window_fires_on_long_flat_run_with_repeated_text() -> None:
    """20 assistant turns, all reusing same Edit target + same tool +
    similar text → flat run >= window, jaccard high → fire."""
    raw: list[dict[str, Any]] = [_user("u-init", _ts(0), "go")]
    raw.append(
        _assistant(
            "a-init",
            _ts(1),
            content=[
                _tool_use("Edit", "tinit", file_path="/x/foo.py", new_string="v"),
                {"type": "text", "text": "starting"},
            ],
        )
    )
    for i in range(2, 20):
        raw.append(
            _assistant(
                f"a{i}",
                _ts(i),
                content=[
                    _tool_use("Edit", f"t{i}", file_path="/x/foo.py", new_string="v"),
                    {"type": "text", "text": "trying again with the same approach again"},
                ],
            )
        )
    entries = _parse(raw)
    signals = detect_novelty_window(entries, threshold=1)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "novelty_window"
    assert sig.count >= 6


def test_novelty_window_suppressed_by_low_text_jaccard() -> None:
    """Same flat structure but assistant text diverges turn-to-turn →
    deep debugging, not stagnation."""
    diverse_texts = [
        "alpha beta gamma 123",
        "x y z banana split",
        "lorem ipsum dolor sit amet",
        "qq ww ee rr tt yy uu ii",
        "0987654321 ##$%^&*()",
        "abcdefghijklm nopqrst uvwx",
        "the quick brown fox jumped",
        "another set of completely different words now",
        "and some more variety here too",
        "yet still more textual variation",
    ]
    raw: list[dict[str, Any]] = [_user("u-init", _ts(0), "go")]
    raw.append(
        _assistant(
            "a-init",
            _ts(1),
            content=[
                _tool_use("Edit", "tinit", file_path="/x/foo.py", new_string="v"),
                {"type": "text", "text": diverse_texts[0]},
            ],
        )
    )
    for i, text in enumerate(diverse_texts[1:], start=2):
        raw.append(
            _assistant(
                f"a{i}",
                _ts(i),
                content=[
                    _tool_use("Edit", f"t{i}", file_path="/x/foo.py", new_string="v"),
                    {"type": "text", "text": text},
                ],
            )
        )
    entries = _parse(raw)
    assert detect_novelty_window(entries, threshold=1) == []


def test_novelty_window_resets_on_new_file() -> None:
    """Touching a new file mid-run resets the flat counter."""
    raw: list[dict[str, Any]] = [_user("u-init", _ts(0), "go")]
    raw.append(
        _assistant(
            "a-init",
            _ts(1),
            content=[
                _tool_use("Edit", "tinit", file_path="/x/foo.py", new_string="v"),
                {"type": "text", "text": "trying"},
            ],
        )
    )
    for i in range(3):
        raw.append(
            _assistant(
                f"a{i}",
                _ts(i + 2),
                content=[
                    _tool_use("Edit", f"t{i}", file_path="/x/foo.py", new_string="v"),
                    {"type": "text", "text": "trying"},
                ],
            )
        )
    raw.append(
        _assistant(
            "a-novel",
            _ts(10),
            content=[_tool_use("Edit", "tnov", file_path="/x/bar.py", new_string="v")],
        )
    )
    for i in range(3):
        raw.append(
            _assistant(
                f"a-tail-{i}",
                _ts(20 + i),
                content=[
                    _tool_use("Edit", f"t-tail-{i}", file_path="/x/bar.py", new_string="v"),
                    {"type": "text", "text": "trying again"},
                ],
            )
        )
    entries = _parse(raw)
    assert detect_novelty_window(entries, window=10, threshold=1) == []


# ---------- turn_cost_acceleration ----------


def _assistant_with_output(uuid: str, ts: str, output_tokens: int) -> dict[str, Any]:
    e = _assistant(uuid, ts)
    e["message"]["usage"]["output_tokens"] = output_tokens
    return e


def test_turn_cost_acceleration_fires_on_rising_output_tokens() -> None:
    raw: list[dict[str, Any]] = []
    for i in range(20):
        raw.append(_assistant_with_output(f"a{i}", _ts(i), output_tokens=100 + i * 50))
    entries = _parse(raw)
    signals = detect_turn_cost_acceleration(entries)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "turn_cost_acceleration"
    assert sig.evidence["slope_output_tokens_per_turn"] > 0
    assert sig.evidence["r_squared"] >= 0.55


def test_turn_cost_acceleration_does_not_fire_on_flat_output() -> None:
    raw: list[dict[str, Any]] = []
    for i in range(20):
        raw.append(_assistant_with_output(f"a{i}", _ts(i), output_tokens=100))
    entries = _parse(raw)
    assert detect_turn_cost_acceleration(entries) == []


def test_turn_cost_acceleration_does_not_fire_below_min_turns() -> None:
    raw: list[dict[str, Any]] = []
    for i in range(10):
        raw.append(_assistant_with_output(f"a{i}", _ts(i), output_tokens=100 + i * 50))
    entries = _parse(raw)
    assert detect_turn_cost_acceleration(entries) == []


def test_turn_cost_acceleration_uses_output_tokens_not_input() -> None:
    """A session w/ flat output_tokens but rising input_tokens (tool
    output accumulation) should NOT fire."""
    raw: list[dict[str, Any]] = []
    for i in range(20):
        e = _assistant_with_output(f"a{i}", _ts(i), output_tokens=80)
        e["message"]["usage"]["input_tokens"] = 100 + i * 100
        raw.append(e)
    entries = _parse(raw)
    assert detect_turn_cost_acceleration(entries) == []


# ---------- test_regression ----------


def test_test_regression_fires_when_pytest_fails_after_edit() -> None:
    raw: list[dict[str, Any]] = [
        _assistant(
            "a-test1",
            _ts(0),
            content=[_tool_use("Bash", "tb1", command="uv run pytest")],
        ),
        _user("u-r1", _ts(1), text=[_tool_result("tb1", "2 failed, 8 passed")]),
        _assistant(
            "a-edit",
            _ts(2),
            content=[_tool_use("Edit", "te", file_path="/x/foo.py", new_string="bad")],
        ),
        _user("u-edit-ok", _ts(3), text=[_tool_result("te", "ok")]),
        _assistant(
            "a-test2",
            _ts(4),
            content=[_tool_use("Bash", "tb2", command="uv run pytest")],
        ),
        _user("u-r2", _ts(5), text=[_tool_result("tb2", "7 failed, 3 passed")]),
    ]
    entries = _parse(raw)
    signals = detect_test_regression(entries)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "test_regression"
    assert sig.evidence["fail_count_before"] == 2
    assert sig.evidence["fail_count_after"] == 7


def test_test_regression_does_not_fire_when_failures_decrease() -> None:
    raw: list[dict[str, Any]] = [
        _assistant("a1", _ts(0), content=[_tool_use("Bash", "tb1", command="pytest")]),
        _user("u1", _ts(1), text=[_tool_result("tb1", "5 failed")]),
        _assistant(
            "a-edit",
            _ts(2),
            content=[_tool_use("Edit", "te", file_path="/x/foo.py", new_string="fix")],
        ),
        _user("u-ed", _ts(3), text=[_tool_result("te", "ok")]),
        _assistant("a2", _ts(4), content=[_tool_use("Bash", "tb2", command="pytest")]),
        _user("u2", _ts(5), text=[_tool_result("tb2", "1 failed")]),
    ]
    entries = _parse(raw)
    assert detect_test_regression(entries) == []


def test_test_regression_unrecognized_runner_silently_ignored() -> None:
    raw: list[dict[str, Any]] = [
        _assistant("a1", _ts(0), content=[_tool_use("Bash", "tb1", command="echo hi")]),
        _user("u1", _ts(1), text=[_tool_result("tb1", "hi")]),
    ]
    entries = _parse(raw)
    assert detect_test_regression(entries) == []


# ---------- trajectory_length_zscore ----------


def test_trajectory_length_zscore_fires_for_anomalous_session() -> None:
    entries = _parse(_bulk_assistant(80, model="claude-sonnet-4-6"))
    baseline = BaselineStats(
        primary_model="claude-sonnet-4-6",
        mean_turns=24.0,
        stddev_turns=8.0,
        n_sessions=30,
    )
    signals = detect_trajectory_length_zscore(entries, baseline)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "trajectory_length_zscore"
    assert sig.evidence["session_turns"] == 80
    assert sig.evidence["z_score"] > 2.0


def test_trajectory_length_zscore_suppressed_by_small_baseline() -> None:
    entries = _parse(_bulk_assistant(80, model="claude-sonnet-4-6"))
    baseline = BaselineStats(
        primary_model="claude-sonnet-4-6",
        mean_turns=24.0,
        stddev_turns=8.0,
        n_sessions=10,
    )
    assert detect_trajectory_length_zscore(entries, baseline) == []


def test_trajectory_length_zscore_does_not_fire_for_normal_session() -> None:
    entries = _parse(_bulk_assistant(28, model="claude-sonnet-4-6"))
    baseline = BaselineStats(
        primary_model="claude-sonnet-4-6",
        mean_turns=24.0,
        stddev_turns=8.0,
        n_sessions=30,
    )
    assert detect_trajectory_length_zscore(entries, baseline) == []


def test_trajectory_length_zscore_returns_empty_when_baseline_is_none() -> None:
    entries = _parse(_bulk_assistant(80, model="claude-sonnet-4-6"))
    assert detect_trajectory_length_zscore(entries, None) == []
