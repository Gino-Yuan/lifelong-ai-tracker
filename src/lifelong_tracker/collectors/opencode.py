"""OpenCode collector (`opencode.db`, SQLite).

Source handling, read-only opening and incremental state are shared with the
other SQLite collectors - see `sqlite_source.py`.

Table read: `session_message(id, session_id, type, seq, time_created,
time_updated, data)`. `time_created` / `time_updated` are Unix epoch milliseconds.
`data` is a JSON document; only these sub-trees are extracted, *inside SQLite*
with `json_extract`, so conversation text never even reaches Python:

    $.tokens   {input, output, reasoning, cache: {read, write}}
    $.model    {"id": ...} or a plain string
    $.modelID  fallback model id
    $.time.created  fallback timestamp (epoch ms)

`content`, `parts`, `system`, `cost` and every other field are never selected.

Mapping: input/output/reasoning/cache.read/cache.write -> input/output/thinking/
cache_read/cache_write tokens; rows whose five counts are all zero are skipped.
Dedup key: (tool="opencode", request_id=<id>, message_id=<id>), so a message that
is updated while streaming merges into one row via the UPSERT.
High-water mark: COALESCE(time_updated, time_created).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from lifelong_tracker.collectors.sqlite_source import (  # noqa: F401 - re-exported
    SqliteCollector,
    connect_readonly,
    ms_to_utc,
    tok,
    to_ms,
)
from lifelong_tracker.models import UsageEvent

DB_NAME = "opencode.db"
TABLE = "session_message"
REQUIRED_COLUMNS = frozenset({"id", "session_id", "time_created", "time_updated", "data"})

# event field <- key inside data.tokens
TOKEN_MAP: dict[str, tuple[str, ...]] = {
    "input_tokens": ("input",),
    "output_tokens": ("output",),
    "thinking_tokens": ("reasoning",),
    "cache_read_tokens": ("cache", "read"),
    "cache_write_tokens": ("cache", "write"),
}

_STAMP = "COALESCE(time_updated, time_created, 0)"
# json_extract with several paths returns one JSON array of just those values.
_QUERY = f"""
SELECT id, session_id, time_created, time_updated,
       CASE WHEN json_valid(data)
            THEN json_extract(data, '$.tokens', '$.model', '$.modelID', '$.time.created')
       END AS meta
FROM {TABLE}
WHERE {_STAMP} >= ?
ORDER BY {_STAMP}, id
"""


def _nested(obj: Any, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _model_name(model: Any, model_id: Any) -> str:
    if isinstance(model, dict):
        model = model.get("id")
    for candidate in (model, model_id):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return "unknown"


class OpenCodeCollector(SqliteCollector):
    tool = "opencode"
    display_name = "OpenCode"
    project_name: ClassVar[str] = "opencode"
    db_name = DB_NAME
    table = TABLE
    required_columns = REQUIRED_COLUMNS
    product = "an OpenCode"
    query = _QUERY
    max_query = f"SELECT MAX({_STAMP}) FROM {TABLE}"

    def row_stamp(self, row: tuple[Any, ...]) -> int:
        return to_ms(row[3]) or to_ms(row[2]) or 0

    def parse_row(self, row: tuple[Any, ...]) -> UsageEvent | None:
        msg_id, session_id, created, _updated, meta_raw = row
        if meta_raw is None:
            raise ValueError("data is not valid JSON")
        tokens_obj, model, model_id, data_created = json.loads(meta_raw)
        if not isinstance(tokens_obj, dict):
            return None  # user / system messages carry no usage
        tokens = {dst: tok(_nested(tokens_obj, keys)) for dst, keys in TOKEN_MAP.items()}
        if not any(tokens.values()):
            return None
        if not isinstance(msg_id, str) or not msg_id.strip():
            raise ValueError("message id missing")
        ms = to_ms(created) or to_ms(data_created)
        if ms is None:
            raise ValueError("no usable timestamp")
        return UsageEvent(
            timestamp=ms_to_utc(ms),
            tool=self.tool,
            model=_model_name(model, model_id),
            project_name=self.project_name,
            session_id=session_id if isinstance(session_id, str) else "",
            request_id=msg_id,
            message_id=msg_id,
            **tokens,
        )
