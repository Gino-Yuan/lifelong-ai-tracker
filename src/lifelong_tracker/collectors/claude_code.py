"""Claude Code collector (session JSONL transcripts).

Only `type == "assistant"` lines that carry `message.usage` produce events.
Whitelisted fields copied out of each line:

    timestamp, sessionId, requestId, cwd (basename only -> project_name),
    message.id, message.model,
    message.usage.{input_tokens, output_tokens, cache_read_input_tokens,
                   cache_creation_input_tokens, cache_creation.ephemeral_*_input_tokens}

`message.content` and every other field are never read into the event and are
never stored. Thinking is billed inside `output_tokens` by Claude Code, so
`thinking_tokens` stays 0 unless a future log version reports it separately.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from lifelong_tracker.collectors.base import JsonlCollector
from lifelong_tracker.models import UsageEvent, parse_iso_utc


def _tok(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _project_name(cwd: Any, path: Path) -> str:
    if isinstance(cwd, str) and cwd.strip():
        pure = PureWindowsPath(cwd) if "\\" in cwd else PurePosixPath(cwd)
        if pure.name:
            return pure.name
    return path.parent.name or "unknown"


class ClaudeCodeCollector(JsonlCollector):
    tool = "claude-code"
    display_name = "Claude Code"

    def parse_record(self, record: dict[str, Any], path: Path) -> UsageEvent | None:
        if record.get("type") != "assistant":
            return None
        msg = record.get("message")
        if not isinstance(msg, dict):
            return None
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return None
        model = str(msg.get("model") or "unknown")
        if model == "<synthetic>":  # locally generated placeholders, not API calls
            return None
        ts_raw = record.get("timestamp")
        if not isinstance(ts_raw, str) or not ts_raw:
            return None

        cache_write = _tok(usage.get("cache_creation_input_tokens"))
        if cache_write == 0 and isinstance(usage.get("cache_creation"), dict):
            cache_write = sum(
                _tok(v) for k, v in usage["cache_creation"].items()
                if k.startswith("ephemeral_") and k.endswith("_input_tokens")
            )
        tokens = {
            "input_tokens": _tok(usage.get("input_tokens")),
            "output_tokens": _tok(usage.get("output_tokens")),
            "thinking_tokens": _tok(usage.get("thinking_tokens")),
            "cache_read_tokens": _tok(usage.get("cache_read_input_tokens")),
            "cache_write_tokens": cache_write,
        }
        if not any(tokens.values()):
            return None

        request_id = str(record.get("requestId") or "")
        message_id = str(msg.get("id") or "")
        if not (request_id or message_id):
            line_uuid = record.get("uuid")
            if not line_uuid:
                return None
            message_id = f"uuid:{line_uuid}"

        return UsageEvent(
            timestamp=parse_iso_utc(ts_raw),
            tool=self.tool,
            model=model,
            project_name=_project_name(record.get("cwd"), path),
            session_id=str(record.get("sessionId") or ""),
            request_id=request_id,
            message_id=message_id,
            **tokens,
        )
