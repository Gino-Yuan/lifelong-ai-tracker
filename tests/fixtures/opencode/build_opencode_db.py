"""Builds a fabricated OpenCode `opencode.db` for tests (no real data, all IDs fake).

The schema mirrors the redacted `session_message` layout; every text field holds
the canary string so tests can prove conversation text never reaches the ledger.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CANARY = "FAKE_OPENCODE_CANARY_91c2"
SECRET_DIR = "/Users/demo/Projects/opencode-secret-project"

SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, directory TEXT NOT NULL
);
CREATE TABLE session_message (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    seq INTEGER NOT NULL,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    data TEXT NOT NULL
);
"""


def ms(iso: str) -> int:
    """'2026-10-16T23:59:59.999Z' -> epoch milliseconds."""
    ts = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    return int(ts.timestamp() * 1000 + 0.5)


def tokens(inp: Any = 0, out: Any = 0, reasoning: Any = 0, read: Any = 0, write: Any = 0) -> dict:
    return {"input": inp, "output": out, "reasoning": reasoning, "cache": {"read": read, "write": write}}


def message(
    msg_id: str,
    session: str,
    seq: int,
    created: int,
    *,
    updated: int | None = None,
    type: str = "assistant",
    data: dict | None = None,
    raw: str | None = None,
) -> tuple:
    """One session_message row. `raw` overrides `data` (e.g. to store malformed JSON)."""
    body = raw if raw is not None else json.dumps(data or {})
    return (msg_id, session, type, seq, created, updated if updated is not None else created, body)


def assistant(created: int, model: Any = None, tok: Any = None, **extra: Any) -> dict:
    data: dict[str, Any] = {
        "role": "assistant",
        "time": {"created": created, "completed": created + 2000},
        "cost": 0,
        "content": CANARY,
        "parts": [{"type": "text", "text": CANARY}],
        "system": [CANARY],
        "path": {"cwd": SECRET_DIR, "root": SECRET_DIR},
    }
    if model is not None:
        data["model"] = model
    if tok is not None:
        data["tokens"] = tok
    data.update(extra)
    return data


# The spec's redacted sample, verbatim timestamps.
SPEC_CREATED = 1792260222023
SPEC_COMPLETED = 1792260225431

T_A2 = ms("2026-10-16T23:59:59.999Z")
T_B1 = ms("2026-10-17T00:00:00.000Z")
T_B3 = ms("2026-10-18T08:30:00.000Z")
T_B4 = ms("2026-10-18T09:00:00.000Z")
T_B6 = ms("2026-10-19T12:00:00.250Z")


def default_rows() -> list[tuple]:
    a1 = {
        "time": {"created": SPEC_CREATED, "completed": SPEC_COMPLETED},
        "model": {"id": "gemini-3-flash", "providerID": "gcp-proxy", "variant": "high"},
        "cost": 0,
        "tokens": {"input": 5113, "output": 14, "reasoning": 316, "cache": {"read": 0, "write": 0}},
        "content": CANARY,
    }
    return [
        message("msg_fx_u1", "ses_fx_alpha", 1, SPEC_CREATED - 5000, type="user",
                data={"role": "user", "time": {"created": SPEC_CREATED - 5000}, "content": CANARY}),
        message("msg_fx_a1", "ses_fx_alpha", 2, SPEC_CREATED, updated=SPEC_COMPLETED, data=a1),
        message("msg_fx_a2", "ses_fx_alpha", 3, T_A2,
                data=assistant(T_A2, "claude-sonnet-4-5", tokens(1200, 340, 0, 800, 200))),
        message("msg_fx_a3", "ses_fx_alpha", 4, T_A2 + 10,
                data=assistant(T_A2 + 10, {"id": "claude-sonnet-4-5"}, tokens())),       # all zero
        message("msg_fx_b1", "ses_fx_beta", 1, T_B1,
                data=assistant(T_B1, None, tokens(100, 50))),                          # no model
        message("msg_fx_b2", "ses_fx_beta", 2, T_B1 + 1000,
                raw='{"tokens": {"input": 999, ' + CANARY),                            # malformed
        message("msg_fx_b3", "ses_fx_beta", 3, T_B3,
                data=assistant(T_B3, {"id": "claude-opus-4-5", "providerID": "anthropic"},
                               tokens(10, 20, 30, "x", -5))),                           # junk counts
        message("msg_fx_b4", "ses_fx_beta", 4, T_B4,
                data=assistant(T_B4, {"providerID": "gcp-proxy"}, tokens(7),
                               modelID="gemini-4-hydrogen")),                           # id fallback
        message("msg_fx_b5", "ses_fx_beta", 5, T_B4 + 1000,
                data=assistant(T_B4 + 1000, "claude-sonnet-4-5", 5)),                   # tokens not an object
        message("msg_fx_b6", "ses_fx_beta", 6, 0, updated=T_B6,
                data=assistant(T_B6, "claude-haiku-4-5", tokens(0, 3))),               # column ts unusable
    ]


# expected outcome of default_rows()
EXPECTED_EVENTS = {
    # id: (session, model, timestamp_utc, input, output, thinking, cache_read, cache_write)
    "msg_fx_a1": ("ses_fx_alpha", "gemini-3-flash", "2026-10-17T18:03:42.023Z", 5113, 14, 316, 0, 0),
    "msg_fx_a2": ("ses_fx_alpha", "claude-sonnet-4-5", "2026-10-16T23:59:59.999Z", 1200, 340, 0, 800, 200),
    "msg_fx_b1": ("ses_fx_beta", "unknown", "2026-10-17T00:00:00.000Z", 100, 50, 0, 0, 0),
    "msg_fx_b3": ("ses_fx_beta", "claude-opus-4-5", "2026-10-18T08:30:00.000Z", 10, 20, 30, 0, 0),
    "msg_fx_b4": ("ses_fx_beta", "gemini-4-hydrogen", "2026-10-18T09:00:00.000Z", 7, 0, 0, 0, 0),
    "msg_fx_b6": ("ses_fx_beta", "claude-haiku-4-5", "2026-10-19T12:00:00.250Z", 0, 3, 0, 0, 0),
}
EXPECTED_ROWS = 10
EXPECTED_BAD = 1


def build(path: Path, rows: list[tuple] | None = None, *, wal: bool = False) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        if wal:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO session VALUES ('ses_fx_alpha', ?, ?)", (CANARY, SECRET_DIR))
        insert(conn, default_rows() if rows is None else rows)
    return path


def insert(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    with conn:
        conn.executemany("INSERT OR REPLACE INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
