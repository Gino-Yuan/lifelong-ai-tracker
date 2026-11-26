"""SQLite storage layer: connections, schema, checkpoints, UPSERT, repricing."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Iterable, Iterator

from lifelong_tracker.models import TOKEN_FIELDS, FileState, UsageEvent, format_utc
from lifelong_tracker.pricing import PricingTable

SCHEMA_VERSION = "1"


def _schema_sql() -> str:
    return resources.files("lifelong_tracker.storage").joinpath("schema.sql").read_text("utf-8")


def _now() -> str:
    """Audit timestamp (microsecond precision; distinct per upsert call)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_schema_sql())
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (SCHEMA_VERSION,),
    )


@contextmanager
def connect(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open the ledger with WAL, foreign keys and a busy timeout; always closed on exit.

    The connection runs in autocommit mode; use `transaction()` for atomic units.
    """
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA temp_store=MEMORY")  # no spill files in the system temp dir
        init_schema(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def connect_readonly(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open the ledger for reading only (PRAGMA query_only), never creating it.

    A missing or uninitialised file yields an empty in-memory ledger, so readers
    (e.g. the web dashboard) work before the first sync without touching disk.
    The connection may be used from a different thread than the one that opened
    it (FastAPI dependencies), but never concurrently.
    """
    path = Path(db_path)
    conn: sqlite3.Connection | None = None
    if path.is_file():
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
        has_events = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
        if not has_events:
            conn.close()
            conn = None
    if conn is None:
        conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        init_schema(conn)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------- checkpoints

def get_checkpoint(conn: sqlite3.Connection, tool: str, path: str) -> FileState | None:
    row = conn.execute(
        "SELECT inode, size, mtime_ns, offset, head_len, head_hash, lines_read, resets "
        "FROM sync_checkpoints WHERE tool = ? AND path = ?",
        (tool, path),
    ).fetchone()
    return None if row is None else FileState(**dict(row))


def save_checkpoint(conn: sqlite3.Connection, tool: str, path: str, state: FileState) -> int:
    row = conn.execute(
        """
        INSERT INTO sync_checkpoints
            (tool, path, inode, size, mtime_ns, offset, head_len, head_hash,
             lines_read, resets, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tool, path) DO UPDATE SET
            inode = excluded.inode, size = excluded.size, mtime_ns = excluded.mtime_ns,
            offset = excluded.offset, head_len = excluded.head_len,
            head_hash = excluded.head_hash, lines_read = excluded.lines_read,
            resets = excluded.resets, updated_at = excluded.updated_at
        RETURNING id
        """,
        (
            tool, path, state.inode, state.size, state.mtime_ns, state.offset,
            state.head_len, state.head_hash, state.lines_read, state.resets, _now(),
        ),
    ).fetchone()
    return int(row[0])


# --------------------------------------------------------------------------- events

@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    def __iadd__(self, other: "UpsertResult") -> "UpsertResult":
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        return self


def merge_batch(events: Iterable[UsageEvent]) -> list[UsageEvent]:
    """Collapse in-memory duplicates (streaming chunks) with the same merge rule as the UPSERT."""
    merged: dict[tuple[str, str, str], UsageEvent] = {}
    for ev in events:
        prev = merged.get(ev.dedup_key)
        if prev is None:
            merged[ev.dedup_key] = ev
            continue
        merged[ev.dedup_key] = UsageEvent(
            timestamp=min(prev.timestamp, ev.timestamp),
            tool=prev.tool,
            model=prev.model if prev.model not in ("", "unknown") else ev.model,
            project_name=prev.project_name or ev.project_name,
            session_id=prev.session_id or ev.session_id,
            request_id=prev.request_id,
            message_id=prev.message_id,
            **{f: max(getattr(prev, f), getattr(ev, f)) for f in TOKEN_FIELDS},
        )
    return list(merged.values())


# Streaming semantics: Claude Code writes one JSONL line per content block of the
# same response, each carrying a cumulative usage snapshot (output grows, input /
# cache stay constant). Per-field MAX keeps the complete final usage regardless of
# line order or replays, never double-counts (no SUM) and never freezes the first
# placeholder chunk (no INSERT OR IGNORE). The WHERE clause makes no-op re-syncs
# leave the row untouched (no RETURNING row -> counted as unchanged).
_UPSERT_SQL = """
INSERT INTO events (
    tool, request_id, message_id, timestamp, model, project_name, session_id,
    input_tokens, output_tokens, thinking_tokens, cache_read_tokens, cache_write_tokens,
    source_checkpoint_id, first_seen_at, updated_at
) VALUES (
    :tool, :request_id, :message_id, :timestamp, :model, :project_name, :session_id,
    :input_tokens, :output_tokens, :thinking_tokens, :cache_read_tokens, :cache_write_tokens,
    :checkpoint_id, :now, :now
)
ON CONFLICT (tool, request_id, message_id) DO UPDATE SET
    input_tokens       = MAX(input_tokens,       excluded.input_tokens),
    output_tokens      = MAX(output_tokens,      excluded.output_tokens),
    thinking_tokens    = MAX(thinking_tokens,    excluded.thinking_tokens),
    cache_read_tokens  = MAX(cache_read_tokens,  excluded.cache_read_tokens),
    cache_write_tokens = MAX(cache_write_tokens, excluded.cache_write_tokens),
    timestamp          = MIN(timestamp,          excluded.timestamp),
    model        = CASE WHEN model IN ('', 'unknown') THEN excluded.model ELSE model END,
    project_name = CASE WHEN project_name = '' THEN excluded.project_name ELSE project_name END,
    session_id   = CASE WHEN session_id   = '' THEN excluded.session_id   ELSE session_id   END,
    updated_at   = excluded.updated_at
WHERE excluded.input_tokens       > input_tokens
   OR excluded.output_tokens      > output_tokens
   OR excluded.thinking_tokens    > thinking_tokens
   OR excluded.cache_read_tokens  > cache_read_tokens
   OR excluded.cache_write_tokens > cache_write_tokens
   OR excluded.timestamp          < timestamp
   OR (model IN ('', 'unknown') AND excluded.model NOT IN ('', 'unknown'))
   OR (project_name = '' AND excluded.project_name <> '')
   OR (session_id   = '' AND excluded.session_id   <> '')
RETURNING id, model, first_seen_at = updated_at AS is_new,
          input_tokens, output_tokens, thinking_tokens, cache_read_tokens, cache_write_tokens
"""


def upsert_events(
    conn: sqlite3.Connection,
    events: Iterable[UsageEvent],
    pricing: PricingTable,
    checkpoint_id: int | None = None,
) -> UpsertResult:
    """Idempotently merge events into the ledger and (re)compute cost for touched rows.

    Call inside `transaction()` so events and the file checkpoint commit together.
    """
    result = UpsertResult()
    now = _now()
    for ev in merge_batch(events):
        params = {
            "tool": ev.tool, "request_id": ev.request_id, "message_id": ev.message_id,
            "timestamp": ev.timestamp_utc, "model": ev.model or "unknown",
            "project_name": ev.project_name, "session_id": ev.session_id,
            "checkpoint_id": checkpoint_id, "now": now,
            **{f: getattr(ev, f) for f in TOKEN_FIELDS},
        }
        row = conn.execute(_UPSERT_SQL, params).fetchone()
        if row is None:
            result.unchanged += 1
            continue
        if row["is_new"]:
            result.inserted += 1
        else:
            result.updated += 1
        cost = pricing.cost_for(row["model"], tool=ev.tool, message_id=ev.message_id,
                                **{f: row[f] for f in TOKEN_FIELDS})
        conn.execute("UPDATE events SET cost_usd = ? WHERE id = ?", (cost, row["id"]))
    return result


# --------------------------------------------------------------------------- pricing

@dataclass
class RepriceResult:
    models_priced: int = 0
    models_unpriced: int = 0
    events_priced: int = 0
    events_unpriced: int = 0
    cost_before: float = 0.0
    cost_after: float = 0.0


# Same rule as pricing.is_codex_thread_total, in SQL.
_CODEX_BLEND_SQL = (
    "(tool = 'codex' AND message_id = 'thread_total' AND output_tokens = 0"
    " AND thinking_tokens = 0 AND cache_read_tokens = 0 AND cache_write_tokens = 0)"
)


def _cost_expr() -> str:
    return (
        f"CASE WHEN {_CODEX_BLEND_SQL} THEN input_tokens * :blended / 1000000.0 ELSE"
        " (input_tokens * :input + output_tokens * :output + thinking_tokens * :thinking"
        " + cache_read_tokens * :cache_read + cache_write_tokens * :cache_write) / 1000000.0 END"
    )


def reprice(conn: sqlite3.Connection, pricing: PricingTable) -> RepriceResult:
    """Recompute events.cost_usd from the current pricing table.

    Only the cost column is written; the five raw token columns are never touched.
    """
    res = RepriceResult()
    with transaction(conn):
        res.cost_before = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM events").fetchone()[0]
        models = conn.execute("SELECT model, COUNT(*) AS n FROM events GROUP BY model").fetchall()
        for row in models:
            hit = pricing.lookup(row["model"])
            if hit is None:
                conn.execute("UPDATE events SET cost_usd = NULL WHERE model = ?", (row["model"],))
                res.models_unpriced += 1
                res.events_unpriced += row["n"]
            else:
                p = hit[1]
                conn.execute(
                    f"UPDATE events SET cost_usd = {_cost_expr()} WHERE model = :model",
                    {"model": row["model"], "input": p.input, "output": p.output,
                     "thinking": p.thinking, "cache_read": p.cache_read,
                     "cache_write": p.cache_write, "blended": p.codex_blended_rate},
                )
                res.models_priced += 1
                res.events_priced += row["n"]
        res.cost_after = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM events").fetchone()[0]
    return res


def pricing_drift(conn: sqlite3.Connection, pricing: PricingTable, tol: float = 1e-6) -> list[str]:
    """Models whose stored cost no longer matches pricing.json (i.e. `reprice` is due)."""
    drifted: list[str] = []
    for row in conn.execute(
        "SELECT model, SUM(cost_usd) AS stored, COUNT(cost_usd) AS priced_rows, COUNT(*) AS n, "
        "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
        "SUM(thinking_tokens) AS thinking_tokens, SUM(cache_read_tokens) AS cache_read_tokens, "
        "SUM(cache_write_tokens) AS cache_write_tokens, "
        f"SUM(CASE WHEN {_CODEX_BLEND_SQL} THEN input_tokens ELSE 0 END) AS blend_input "
        "FROM events GROUP BY model"
    ):
        hit = pricing.lookup(row["model"])
        if hit is None:
            if row["priced_rows"]:
                drifted.append(row["model"])
            continue
        if row["priced_rows"] != row["n"]:
            drifted.append(row["model"])
            continue
        tokens = {f: row[f] for f in TOKEN_FIELDS}
        blend = row["blend_input"] or 0
        tokens["input_tokens"] -= blend
        expected = hit[1].cost(**tokens) + blend * hit[1].codex_blended_rate / 1_000_000
        if abs(expected - (row["stored"] or 0.0)) > tol * max(1.0, expected):
            drifted.append(row["model"])
    return drifted
