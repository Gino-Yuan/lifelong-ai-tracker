"""Codex (GPT) collector (`state_5.sqlite`, SQLite).

Source handling, read-only opening and incremental state are shared with the
other SQLite collectors - see `sqlite_source.py`.

Table read: `threads`. Only these metadata columns are ever selected:

    id, created_at_ms, created_at, updated_at_ms, model, cwd, tokens_used

`title`, `first_user_message`, `preview` and every other column are never
selected, so conversation text never reaches Python.

Codex's state database stores only a running *per-thread total* (`tokens_used`),
not a per-request or per-category breakdown. Each thread therefore becomes one
event:

    request_id = session_id = <thread id>, message_id = "thread_total"
    input_tokens = tokens_used; output / thinking / cache_read / cache_write = 0

When a thread grows, the same dedup key is UPSERTed and MAX() keeps the newest
total. The event keeps the thread's creation time, so a long thread's whole
usage counts on the day it was started. Rows with NULL / non-positive
`tokens_used` are skipped. High-water mark: COALESCE(updated_at_ms,
created_at_ms, created_at * 1000).
"""

from __future__ import annotations

from typing import Any, ClassVar

from lifelong_tracker.collectors.sqlite_source import (
    SqliteCollector,
    is_number,
    ms_to_utc,
    path_basename,
    to_ms,
)
from lifelong_tracker.models import UsageEvent

DB_NAME = "state_5.sqlite"
TABLE = "threads"
COLUMNS = ("id", "created_at_ms", "created_at", "updated_at_ms", "model", "cwd", "tokens_used")
REQUIRED_COLUMNS = frozenset(COLUMNS)
MESSAGE_ID = "thread_total"

_STAMP = "COALESCE(updated_at_ms, created_at_ms, created_at * 1000, 0)"
_QUERY = f"""
SELECT {", ".join(COLUMNS)}
FROM {TABLE}
WHERE {_STAMP} >= ?
ORDER BY {_STAMP}, id
"""


def _total(value: Any) -> int:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not is_number(value) or value != value:
        return 0
    return max(0, int(value))


class CodexCollector(SqliteCollector):
    tool = "codex"
    display_name = "Codex"
    default_project: ClassVar[str] = "codex"
    db_name = DB_NAME
    table = TABLE
    required_columns = REQUIRED_COLUMNS
    product = "a Codex state"
    query = _QUERY
    max_query = f"SELECT MAX({_STAMP}) FROM {TABLE}"

    def row_stamp(self, row: tuple[Any, ...]) -> int:
        _id, created_ms, created_s, updated_ms = row[:4]
        return to_ms(updated_ms) or to_ms(created_ms) or to_ms(created_s, scale=1000) or 0

    def parse_row(self, row: tuple[Any, ...]) -> UsageEvent | None:
        thread_id, created_ms, created_s, _updated_ms, model, cwd, tokens_used = row
        total = _total(tokens_used)
        if total <= 0:
            return None
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread id missing")
        ms = to_ms(created_ms) or to_ms(created_s, scale=1000)
        if ms is None:
            raise ValueError("no usable timestamp")
        return UsageEvent(
            timestamp=ms_to_utc(ms),
            tool=self.tool,
            model=model.strip() if isinstance(model, str) and model.strip() else "unknown",
            project_name=path_basename(cwd) or self.default_project,
            session_id=thread_id,
            request_id=thread_id,
            message_id=MESSAGE_ID,
            input_tokens=total,
        )
