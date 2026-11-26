"""Builders for fabricated Claude Code JSONL lines (all IDs / paths are fake)."""

from __future__ import annotations

import json
from pathlib import Path


def assistant_line(
    msg_id: str,
    *,
    req_id: str | None = None,
    ts: str = "2026-04-01T10:00:00.000Z",
    model: str = "claude-sonnet-4-5-20250929",
    inp: int = 10,
    out: int = 20,
    cr: int = 0,
    cw: int = 0,
    session: str = "sess-fx-factory",
    cwd: str = "/Users/demo/Projects/factory",
    text: str = "FAKE_CONTENT_CANARY_7f3a",
) -> str:
    record = {
        "parentUuid": None,
        "isSidechain": False,
        "cwd": cwd,
        "sessionId": session,
        "type": "assistant",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw,
            },
        },
        "uuid": f"uuid-{msg_id}-{out}",
        "timestamp": ts,
    }
    if req_id is not None:
        record["requestId"] = req_id
    return json.dumps(record) + "\n"


def write(path: Path, *lines: str, mode: str = "w") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as f:
        f.write("".join(lines))
    return path
