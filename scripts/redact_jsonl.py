"""Redact a JSONL file for test fixtures.

Preserves everything cost-related; replaces text content with ``[REDACTED]``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def redact_content_block(block: dict[str, Any]) -> dict[str, Any]:
    t = block.get("type")
    if t == "text":
        return {"type": "text", "text": "[REDACTED]"}
    if t == "tool_use":
        return {
            "type": "tool_use",
            "id": block.get("id"),
            "name": block.get("name"),
            "input": {},
        }
    if t == "tool_result":
        return {
            "type": "tool_result",
            "tool_use_id": block.get("tool_use_id"),
            "content": "[REDACTED]",
        }
    return {"type": t}


def redact(line: str) -> str:
    record = json.loads(line)
    msg = record.get("message")
    if msg and isinstance(msg.get("content"), list):
        msg["content"] = [redact_content_block(b) for b in msg["content"]]
    for k in ("summary", "slug"):
        if k in record and isinstance(record[k], str):
            record[k] = "[REDACTED]"
    if "cwd" in record:
        record["cwd"] = "/redacted"
    return json.dumps(record)


def main() -> None:
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open() as fin, dst.open("w") as fout:
        for raw in fin:
            line = raw.rstrip("\n")
            if not line:
                continue
            try:
                fout.write(redact(line) + "\n")
            except json.JSONDecodeError:
                continue  # drop truncated tail


if __name__ == "__main__":
    main()
