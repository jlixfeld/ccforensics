from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import SpawnMeta, TranscriptEntry

logger = logging.getLogger("ccforensics.tree")


@dataclass(frozen=True)
class SessionGraph:
    """Within-session tool-use graph.

    Nodes: ``TranscriptEntry.uuid``.
    Edges: ``child.source_tool_use_id == parent.(tool_use block).id``.

    Local to one JSONL file. Cross-file (subagent) linkage is handled
    by ``discover_spawn`` + attribution rollup, not here.
    """

    emitter_of_tool_use: dict[str, str]
    children_of_tool_use: dict[str, list[str]]
    parent_of_uuid: dict[str, str]
    orphan_children: list[str]

    def parent_tool_use_id(self, message_uuid: str) -> str | None:
        return self.parent_of_uuid.get(message_uuid)

    def descendants_of(self, tool_use_id: str) -> set[str]:
        """Transitive closure of descendants reachable from ``tool_use_id``.

        Returns message uuids (not tool_use_ids). Guards against cycles
        with a seen-set.
        """
        seen: set[str] = set()
        stack: list[str] = list(self.children_of_tool_use.get(tool_use_id, ()))
        while stack:
            uuid = stack.pop()
            if uuid in seen:
                continue
            seen.add(uuid)
            for tuid, emitter in self.emitter_of_tool_use.items():
                if emitter == uuid:
                    stack.extend(self.children_of_tool_use.get(tuid, ()))
        return seen


def _extract_tool_use_ids(entry: TranscriptEntry) -> list[str]:
    """All ``tool_use`` block ids from this entry's message content."""
    if entry.message is None or not entry.message.content:
        return []
    out: list[str] = []
    for block in entry.message.content:
        if block.type == "tool_use" and block.id:
            out.append(block.id)
    return out


def build_session_graph(entries: Iterable[TranscriptEntry]) -> SessionGraph:
    """Build a ``SessionGraph`` from an unordered iterable of entries.

    Entries without ``uuid`` are skipped. Duplicate ``tool_use_id`` keeps
    the earliest emitter by timestamp (first-wins); a warning is logged
    since this shouldn't happen in well-formed Claude Code output.
    """
    sorted_entries = sorted(
        (e for e in entries if e.uuid),
        key=lambda e: e.timestamp,
    )

    emitter_of_tool_use: dict[str, str] = {}
    children_of_tool_use: dict[str, list[str]] = {}
    parent_of_uuid: dict[str, str] = {}
    orphan_children: list[str] = []

    for entry in sorted_entries:
        assert entry.uuid is not None
        for tool_use_id in _extract_tool_use_ids(entry):
            if tool_use_id in emitter_of_tool_use:
                logger.warning(
                    "duplicate tool_use_id %s (first emitter %s, also in %s); keeping first",
                    tool_use_id,
                    emitter_of_tool_use[tool_use_id],
                    entry.uuid,
                )
                continue
            emitter_of_tool_use[tool_use_id] = entry.uuid

    for entry in sorted_entries:
        assert entry.uuid is not None
        src = entry.source_tool_use_id
        if src is None:
            continue
        if src in emitter_of_tool_use:
            parent_of_uuid[entry.uuid] = src
            children_of_tool_use.setdefault(src, []).append(entry.uuid)
        else:
            orphan_children.append(entry.uuid)

    return SessionGraph(
        emitter_of_tool_use=emitter_of_tool_use,
        children_of_tool_use=children_of_tool_use,
        parent_of_uuid=parent_of_uuid,
        orphan_children=orphan_children,
    )


@dataclass(frozen=True)
class Spawn:
    """One subagent spawn event.

    ``parent_message_uuid`` / ``parent_tool_use_id`` are None when the
    linkage is unresolvable. Callers should route the subagent's cost
    to the ``unattributed`` bucket in that case.

    ``subagent_type`` preference: ``meta.agent_type`` (authoritative),
    else parent ``tool_use.input.subagent_type``, else None.

    ``description`` is from meta.json only.

    ``model_hint`` is the model of the first assistant message in the
    child file (for reports only; cost math uses per-message models).
    """

    parent_session_id: str
    child_agent_id: str
    child_file_path: str
    subagent_type: str | None
    description: str | None
    ts_spawned: datetime
    parent_message_uuid: str | None
    parent_tool_use_id: str | None
    model_hint: str | None


def _iter_agent_tool_uses(
    parent_entries: Iterable[TranscriptEntry],
    before: datetime,
) -> Iterable[tuple[datetime, str, str, str | None]]:
    """Yield ``(ts, emitter_uuid, tool_use_id, input_subagent_type)`` for
    every Agent/Task tool_use emitted before ``before``."""
    for entry in parent_entries:
        if entry.timestamp > before:
            continue
        if entry.uuid is None or entry.message is None:
            continue
        for block in entry.message.content or []:
            if block.type != "tool_use" or block.name not in ("Agent", "Task"):
                continue
            if not block.id:
                continue
            subtype: str | None = None
            if block.input:
                val = block.input.get("subagent_type")
                if isinstance(val, str):
                    subtype = val
            yield entry.timestamp, entry.uuid, block.id, subtype


def discover_spawn(
    *,
    parent_session_id: str,
    child_agent_id: str,
    child_file_path: Path,
    child_entries: Iterable[TranscriptEntry],
    parent_entries: Iterable[TranscriptEntry],
    meta: SpawnMeta | None,
) -> Spawn | None:
    """Link a subagent file to its parent Agent/Task call.

    Heuristic: nearest Agent|Task before ``ts_spawned``, preferring
    candidates whose ``input.subagent_type`` matches ``meta.agent_type``
    when available. Composite rank key ``(type_match, timestamp)``
    picks max — matches dominate; nearest among matches wins; with no
    matches, falls back to nearest across all candidates.

    Returns ``None`` only when the child file has no entries. An
    unresolvable spawn still returns a ``Spawn`` with null parent
    fields so the caller can record it.
    """
    child_list = list(child_entries)
    if not child_list:
        return None

    ts_spawned = min(e.timestamp for e in child_list)

    wanted = meta.agent_type if meta else None
    candidates = list(_iter_agent_tool_uses(parent_entries, before=ts_spawned))

    parent_uuid: str | None = None
    parent_tu_id: str | None = None
    parent_subtype: str | None = None
    if candidates:
        _, uid, tu_id, subtype = max(
            candidates,
            key=lambda c: ((c[3] == wanted) if wanted else False, c[0]),
        )
        parent_uuid = uid
        parent_tu_id = tu_id
        parent_subtype = subtype

    subagent_type = (meta.agent_type if meta else None) or parent_subtype
    description = meta.description if meta else None

    model_hint: str | None = None
    for entry in sorted(child_list, key=lambda e: e.timestamp):
        if entry.type == "assistant" and entry.message and entry.message.model:
            model_hint = entry.message.model
            break

    return Spawn(
        parent_session_id=parent_session_id,
        child_agent_id=child_agent_id,
        child_file_path=str(child_file_path),
        subagent_type=subagent_type,
        description=description,
        ts_spawned=ts_spawned,
        parent_message_uuid=parent_uuid,
        parent_tool_use_id=parent_tu_id,
        model_hint=model_hint,
    )
