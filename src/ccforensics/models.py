from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("ccforensics.models")


class CacheCreationDetail(BaseModel):
    """TTL split of cache-creation tokens emitted in ``usage.cache_creation``.

    Present on transcripts written by Claude Code versions that send the
    1h-cache beta header (``ENABLE_PROMPT_CACHING_1H`` — default on Max
    subscriptions since 2.1.108). The sum equals ``cache_creation_input_tokens``
    when both sides are present. Older transcripts omit this sub-object and
    only carry the top-level total — treated as all-5m for back-compat.
    """

    model_config = ConfigDict(extra="allow")

    ephemeral_1h_input_tokens: int | None = None
    ephemeral_5m_input_tokens: int | None = None


class UsageStats(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    service_tier: str | None = None
    cache_creation: CacheCreationDetail | None = None
    # ``"standard"`` | ``"fast"``. Fast-mode pricing not yet in LiteLLM —
    # captured here as a precursor; pricing branch deferred until rates land.
    speed: str | None = None


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
    """Permissive container for one JSONL line. Unknown fields preserved in .extras."""

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
    """Wrap bare-string message.content into a single text block so pydantic
    accepts it — Claude Code emits plain strings for simple user prompts."""
    msg = raw.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = [{"type": "text", "text": content}]


class SpawnMeta(BaseModel):
    """Schema for agent-<id>.meta.json."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    agent_type: str | None = Field(default=None, alias="agentType")
    description: str | None = None


def load_meta_json(path: Path) -> SpawnMeta | None:
    """Read agent-<id>.meta.json. Missing-file is silent because older Claude
    Code versions and auto-compact artifacts don't emit it."""
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
    """Normalize legacy field names, then pydantic-parse."""
    if "parentToolUseId" in raw and "sourceToolUseID" not in raw:
        raw["sourceToolUseID"] = raw.pop("parentToolUseId")
    if "parentToolAssistantUuid" in raw and "sourceToolAssistantUUID" not in raw:
        raw["sourceToolAssistantUUID"] = raw.pop("parentToolAssistantUuid")
    _normalize_message_content(raw)
    return TranscriptEntry.model_validate(raw)
