"""Antigravity collector (per-conversation `transcript.jsonl` step logs).

Layout under the `--source` root (e.g. a `brain` folder)::

    <conversation-id>/.system_generated/logs/transcript.jsonl       <- read
    <conversation-id>/.system_generated/logs/transcript_full.jsonl  <- never read (duplicate)

Only lines with `source == "MODEL"` that carry at least one numeric token field
produce events (all-zero usage is skipped, as for Claude Code). Whitelisted
fields copied out of each line:

    step_index, created_at, model,
    input_tokens, output_tokens, cache_read_tokens, thinking_tokens, cache_write_tokens

`content`, `thinking`, `tool_calls`, `media` and every other field are never
read into the event and are never stored.

Dedup key: (tool="antigravity", request_id=<conversation-id>, message_id="step_<n>"),
so a step that is re-written or replayed merges into one row via the UPSERT.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from lifelong_tracker.collectors.base import JsonlCollector
from lifelong_tracker.models import UsageEvent, parse_iso_utc

LOG_NAME = "transcript.jsonl"
LOG_DIRS = (".system_generated", "logs")

# event field <- log field
TOKEN_MAP: dict[str, str] = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "thinking_tokens": "thinking_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "cache_write_tokens": "cache_write_tokens",
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _tok(value: Any) -> int:
    return max(0, int(value)) if _is_number(value) else 0


def _step_index(value: Any) -> int | None:
    if _is_number(value) and float(value).is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def conversation_id(path: Path) -> str:
    """`<conversation-id>` for `.../<conversation-id>/.system_generated/logs/transcript.jsonl`."""
    return path.parent.parent.parent.name


class AntigravityCollector(JsonlCollector):
    tool = "antigravity"
    display_name = "Antigravity"
    project_name: ClassVar[str] = "antigravity"

    def discover(self, source: Path) -> list[Path]:
        root = Path(source)
        files = [
            p for p in root.rglob(LOG_NAME)
            if p.name == LOG_NAME  # exact: transcript_full.jsonl is never matched
            and (p.parent.parent.name, p.parent.name) == LOG_DIRS
            and p.is_file()
            and not p.is_symlink()
        ]
        return sorted(files)

    def parse_record(self, record: dict[str, Any], path: Path) -> UsageEvent | None:
        if record.get("source") != "MODEL":
            return None
        if not any(_is_number(record.get(field)) for field in TOKEN_MAP.values()):
            return None
        tokens = {dst: _tok(record.get(src)) for dst, src in TOKEN_MAP.items()}
        if not any(tokens.values()):
            return None
        step = _step_index(record.get("step_index"))
        if step is None:
            return None
        ts_raw = record.get("created_at")
        if not isinstance(ts_raw, str) or not ts_raw.strip():
            return None
        session = conversation_id(path)
        if not session:
            return None
        model = record.get("model")
        return UsageEvent(
            timestamp=parse_iso_utc(ts_raw),
            tool=self.tool,
            model=model if isinstance(model, str) and model.strip() else "unknown",
            project_name=self.project_name,
            session_id=session,
            request_id=session,
            message_id=f"step_{step}",
            **tokens,
        )
