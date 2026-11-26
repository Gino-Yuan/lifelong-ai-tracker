"""Builds a fabricated Codex `state_5.sqlite` for tests (no real data, all IDs fake).

The `threads` table mirrors the redacted layout. Content columns (`title`,
`first_user_message`, `preview`) and the private `rollout_path` hold canary values
so tests can prove none of them ever reaches the ledger.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

CANARY = "FAKE_CODEX_CANARY_5b8e"
SECRET_ROLLOUT = "/Users/demo/.codex/sessions/2026/10/17/rollout-secret-fx.jsonl"

SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER,
    updated_at INTEGER,
    created_at_ms INTEGER,
    updated_at_ms INTEGER,
    source TEXT,
    model_provider TEXT,
    model TEXT,
    cwd TEXT,
    title TEXT,
    first_user_message TEXT,
    preview TEXT,
    tokens_used INTEGER,
    archived INTEGER NOT NULL DEFAULT 0
);
"""

COLUMNS = ("id", "created_at_ms", "created_at", "updated_at_ms", "model",
           "model_provider", "cwd", "tokens_used")


def thread(
    thread_id: str,
    *,
    created_ms: Any = None,
    created_s: Any = None,
    updated_ms: Any = None,
    model: Any = "gpt-6-luna",
    provider: Any = "openai",
    cwd: Any = "/Users/demo/work/my-app",
    tokens: Any = 0,
) -> dict:
    return {
        "id": thread_id, "created_at_ms": created_ms, "created_at": created_s,
        "updated_at_ms": updated_ms, "model": model, "model_provider": provider,
        "cwd": cwd, "tokens_used": tokens,
    }


# The spec's redacted sample row, verbatim.
SPEC = thread("thr_fx_001", created_ms=1792260222023, created_s=1792260222,
              updated_ms=1792260300000, tokens=42850)

T_LATE = 1792411200250  # 2026-10-19T12:00:00.250Z


def default_rows() -> list[dict]:
    return [
        SPEC,
        thread("thr_fx_002", created_s=1792195199, model="gpt-6-sol",
               cwd="C:\\Users\\demo\\repos\\win-app", tokens=1200),              # seconds fallback, Windows cwd
        thread("thr_fx_003", created_ms=1792300000000, updated_ms=1792300000500,
               model=None, cwd=None, tokens="350"),                             # unknown model, default project
        thread("thr_fx_004", created_ms=1792300001000, tokens=None),            # NULL -> skipped
        thread("thr_fx_005", created_ms=1792300002000, tokens=0),               # zero -> skipped
        thread("thr_fx_006", created_ms=1792300003000, tokens=-10),             # negative -> skipped
        thread("thr_fx_007", tokens=99),                                        # no timestamp -> malformed
        thread("thr_fx_008", created_ms=1792300004000, model="  ",
               cwd="/Users/demo/work/trailing/", tokens=5.5),                   # blank model, trailing slash
        thread("thr_fx_009", created_ms=T_LATE, updated_ms=T_LATE, cwd="/", tokens=10),
    ]


EXPECTED_EVENTS = {
    # id: (model, project_name, timestamp_utc, input_tokens)
    "thr_fx_001": ("gpt-6-luna", "my-app", "2026-10-17T18:03:42.023Z", 42850),
    "thr_fx_002": ("gpt-6-sol", "win-app", "2026-10-16T23:59:59.000Z", 1200),
    "thr_fx_003": ("unknown", "codex", "2026-10-18T05:06:40.000Z", 350),
    "thr_fx_008": ("unknown", "trailing", "2026-10-18T05:06:44.000Z", 5),
    "thr_fx_009": ("gpt-6-luna", "codex", "2026-10-19T12:00:00.250Z", 10),
}
EXPECTED_ROWS = 9
EXPECTED_BAD = 1


def build(path: Path, rows: list[dict] | None = None, *, wal: bool = False) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        if wal:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        insert(conn, default_rows() if rows is None else rows)
    return path


def insert(conn: sqlite3.Connection, rows: list[dict]) -> None:
    cols = (*COLUMNS, "rollout_path", "title", "first_user_message", "preview")
    sql = (f"INSERT OR REPLACE INTO threads ({', '.join(cols)}) "
           f"VALUES ({', '.join('?' * len(cols))})")
    with conn:
        conn.executemany(sql, [
            (*(r[c] for c in COLUMNS), SECRET_ROLLOUT, f"title {CANARY}",
             f"first message {CANARY}", f"preview {CANARY}")
            for r in rows
        ])
