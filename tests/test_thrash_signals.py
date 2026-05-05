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
    assistant_turn_count,
    detect_placeholder_emit,
    detect_repeated_edit,
    detect_repeated_error,
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
    return f"2026-04-22T10:{i:02d}:00Z"


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
