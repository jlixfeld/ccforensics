from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("ccforensics.models")


class UsageStats(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


class ContentBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    content: Any = None
    text: str | None = None


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    role: str | None = None
    model: str | None = None
    content: list[ContentBlock] = Field(default_factory=list)
    usage: UsageStats | None = None


class Attachment(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str | None = None
    hook_event: str | None = Field(default=None, alias="hookEvent")
    hook_name: str | None = Field(default=None, alias="hookName")
    tool_use_id: str | None = Field(default=None, alias="toolUseID")
    content: str | None = None
    stdout: str | None = None


class TranscriptEntry(BaseModel):
    """Permissive, versioned container for a single JSONL line.

    Required: type, timestamp. Everything else optional. Unknown top-level fields
    preserved in `.extras`. Normalizes legacy field names at parse time.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str
    timestamp: datetime
    uuid: str | None = None
    parent_uuid: str | None = Field(default=None, alias="parentUuid")
    session_id: str | None = Field(default=None, alias="sessionId")
    request_id: str | None = Field(default=None, alias="requestId")
    source_tool_use_id: str | None = Field(
        default=None,
        validation_alias="sourceToolUseID",
    )
    source_tool_assistant_uuid: str | None = Field(
        default=None,
        validation_alias="sourceToolAssistantUUID",
    )
    agent_id: str | None = Field(default=None, alias="agentId")
    cwd: str | None = None
    version: str | None = None
    is_sidechain: bool = Field(default=False, alias="isSidechain")
    is_meta: bool = Field(default=False, alias="isMeta")
    is_compact_summary: bool = Field(default=False, alias="isCompactSummary")
    slug: str | None = None
    leaf_uuid: str | None = Field(default=None, alias="leafUuid")
    summary: str | None = None
    user_type: str | None = Field(default=None, alias="userType")
    message: Message | None = None
    attachment: Attachment | None = None
    tool_use_result: Any = Field(default=None, alias="toolUseResult")

    @property
    def extras(self) -> dict[str, Any]:
        return self.__pydantic_extra__ or {}


KNOWN_TYPES: frozenset[str] = frozenset(
    {
        "user",
        "assistant",
        "system",
        "attachment",
        "summary",
        "file-history-snapshot",
        "permission-mode",
        "last-prompt",
        "queue-operation",
        "tool_use",
        "tool_result",
    }
)


def _normalize_message_content(raw: dict[str, Any]) -> None:
    """Claude Code sometimes emits ``message.content`` as a bare string
    (common for plain user text prompts); pydantic rejects that because the
    ``Message.content`` field is typed ``list[ContentBlock]``. Wrap the
    string into a single-element text block so validation proceeds and
    downstream consumers see the list-of-blocks shape unchanged."""
    msg = raw.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = [{"type": "text", "text": content}]


class SpawnMeta(BaseModel):
    """Schema for ``agent-<id>.meta.json`` — the sibling metadata file
    Claude Code writes next to a subagent JSONL. Only ``agentType`` and
    ``description`` are consumed downstream; extras are preserved in
    ``__pydantic_extra__`` for future use.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    agent_type: str | None = Field(default=None, alias="agentType")
    description: str | None = None


def load_meta_json(path: Path) -> SpawnMeta | None:
    """Read and parse ``agent-<id>.meta.json``. Returns ``None`` on any
    failure (missing file, malformed JSON, unexpected top-level shape),
    with a warning logged so the caller can proceed without that linkage.

    Missing-file is silent: older Claude Code versions and auto-compact
    artifacts don't emit meta.json, and every caller must already handle
    ``None``. Raising would just force try/except at every call site.
    """
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("failed to read meta.json at %s", path, exc_info=True)
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("malformed JSON in meta.json at %s", path, exc_info=True)
        return None

    if not isinstance(data, dict):
        logger.warning(
            "meta.json at %s has unexpected top-level shape (expected object, got %s)",
            path,
            type(data).__name__,
        )
        return None

    try:
        return SpawnMeta.model_validate(data)
    except Exception:
        logger.warning("meta.json at %s failed schema validation", path, exc_info=True)
        return None


def parse_entry(raw: dict[str, Any]) -> TranscriptEntry:
    """Normalize legacy field names, then pydantic-parse. Never raises on
    unknown types or extra fields."""
    if "parentToolUseId" in raw and "sourceToolUseID" not in raw:
        raw["sourceToolUseID"] = raw.pop("parentToolUseId")
    if "parentToolAssistantUuid" in raw and "sourceToolAssistantUUID" not in raw:
        raw["sourceToolAssistantUUID"] = raw.pop("parentToolAssistantUuid")
    _normalize_message_content(raw)
    return TranscriptEntry.model_validate(raw)
