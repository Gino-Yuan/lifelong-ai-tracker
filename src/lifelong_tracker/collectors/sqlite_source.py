"""Shared machinery for collectors that read another tool's SQLite database.

Guarantees for every subclass:
* `--source` is the database file itself, or the directory that *directly*
  contains `db_name` (no recursion; a symlinked database in a directory is skipped);
* the database is opened strictly read-only: a `file:` URI built with
  `Path.as_uri()` (spaces, '?', '#' are percent-encoded) + `?mode=ro`, then
  `PRAGMA query_only = ON` and `PRAGMA trusted_schema = OFF`. The tracker never
  writes to it, never leaves a journal next to it and never checkpoints its WAL;
* subclasses select only whitelisted metadata columns in their own `query`.

Incremental state (stored in the generic FileState):
* inode / size / mtime_ns  - signature of the database plus its `-wal` file;
                             unchanged signature -> fast skip, database not opened;
* head_hash = "ms:<n>"      - high-water mark: the largest row timestamp (epoch ms)
                             seen so far. The next scan re-reads rows whose stamp is
                             >= the mark (ties are re-read; the UPSERT makes that
                             harmless). `offset` / `head_len` stay 0: they are byte
                             positions for JSONL files and the ledger requires
                             offset <= size;
* different inode          -> database replaced -> rescan from 0;
* newest row older than the mark -> database restored from an older copy -> rescan.
"""

from __future__ import annotations

import sqlite3
from abc import abstractmethod
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, ClassVar

from lifelong_tracker.collectors.base import BaseCollector, FileScan
from lifelong_tracker.models import FileState, UsageEvent

SQLITE_MAGIC = b"SQLite format 3\x00"
MARK_PREFIX = "ms:"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z


# ------------------------------------------------------------------ value helpers

def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def tok(value: Any) -> int:
    return max(0, int(value)) if is_number(value) else 0


def to_ms(value: Any, *, scale: int = 1) -> int | None:
    """Epoch value (x `scale` to reach ms) from an int/float/digit string, or None."""
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not is_number(value) or value != value:  # NaN guard
        return None
    try:
        ms = int(value * scale)
    except (OverflowError, ValueError):
        return None
    return ms if 0 < ms <= _MAX_MS else None


def ms_to_utc(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime (exact, no float rounding)."""
    return _EPOCH + timedelta(milliseconds=ms)


def path_basename(value: Any) -> str | None:
    """Last directory name of a POSIX or Windows path string, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    pure = PureWindowsPath(text) if "\\" in text else PurePosixPath(text)
    return pure.name or None


# ------------------------------------------------------------------ database helpers

def is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open `path` read-only. `as_uri()` percent-encodes spaces, '?' and '#' in the path."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        # never run functions embedded in a foreign schema (views / triggers)
        conn.execute("PRAGMA trusted_schema = OFF")
    except BaseException:
        conn.close()
        raise
    return conn


def signature(path: Path) -> tuple[int, int, int]:
    """(inode, size, mtime_ns) of the database, folding in its WAL file if present."""
    st = path.stat()
    size, mtime = st.st_size, st.st_mtime_ns
    try:
        wst = path.with_name(path.name + "-wal").stat()
    except FileNotFoundError:
        pass
    else:
        size += wst.st_size
        mtime = max(mtime, wst.st_mtime_ns)
    return st.st_ino, size, mtime


def read_mark(state: FileState) -> int:
    if state.head_hash.startswith(MARK_PREFIX):
        value = state.head_hash[len(MARK_PREFIX):]
        if value.isdigit():
            return int(value)
    return 0


def check_schema(
    conn: sqlite3.Connection, path: Path, table: str, required: frozenset[str], product: str
) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not cols:
        raise ValueError(f"{path.name}: no '{table}' table - is this {product} database?")
    missing = required - cols
    if missing:
        raise ValueError(f"{path.name}: '{table}' is missing column(s): {', '.join(sorted(missing))}")


# ------------------------------------------------------------------ base collector

class SqliteCollector(BaseCollector):
    """Base for collectors that read one table of a foreign SQLite database."""

    accepts_file = True
    db_name: ClassVar[str]
    table: ClassVar[str]
    required_columns: ClassVar[frozenset[str]]
    product: ClassVar[str]  # for error messages, e.g. "an OpenCode"
    # SELECT ... WHERE <stamp expr> >= ? ORDER BY <stamp expr>; column 0 is the row id
    query: ClassVar[str]
    max_query: ClassVar[str]  # SELECT MAX(<stamp expr>) FROM <table>

    def discover(self, source: Path) -> list[Path]:
        src = Path(source)
        if src.is_file():
            if not is_sqlite_file(src):
                raise ValueError(f"not an SQLite database: {src.name}")
            return [src]
        candidate = src / self.db_name
        if candidate.is_file() and not candidate.is_symlink() and is_sqlite_file(candidate):
            return [candidate]
        return []

    @abstractmethod
    def row_stamp(self, row: tuple[Any, ...]) -> int:
        """Change stamp (epoch ms) of a selected row, used for the high-water mark."""

    @abstractmethod
    def parse_row(self, row: tuple[Any, ...]) -> UsageEvent | None:
        """Map one selected row to an event (None = no usage); raise ValueError if malformed."""

    def read_incremental(self, path: Path, checkpoint: FileState | None) -> FileScan:
        inode, size, mtime = signature(path)
        mark = 0
        reset: str | None = None
        prev_rows = prev_resets = 0
        if checkpoint is not None:
            prev_rows, prev_resets = checkpoint.lines_read, checkpoint.resets
            if (inode, size, mtime) == (checkpoint.inode, checkpoint.size, checkpoint.mtime_ns):
                return FileScan(path=path, state=checkpoint, unchanged=True)
            if inode != checkpoint.inode:
                reset = "replaced"
            else:
                mark = read_mark(checkpoint)

        events: list[UsageEvent] = []
        rows = bad = 0
        with closing(connect_readonly(path)) as conn:
            check_schema(conn, path, self.table, self.required_columns, self.product)
            newest = conn.execute(self.max_query).fetchone()[0] or 0
            if mark and newest < mark:
                reset, mark = "truncated", 0
            high = mark
            for row in conn.execute(self.query, (mark,)):
                rows += 1
                high = max(high, self.row_stamp(row))
                try:
                    event = self.parse_row(row)
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    bad += 1
                    continue
                if event is not None:
                    events.append(event)

        if reset is not None:
            prev_rows = 0
        state = FileState(
            inode=inode, size=size, mtime_ns=mtime, offset=0, head_hash=f"{MARK_PREFIX}{high}",
            lines_read=prev_rows + rows, resets=prev_resets + (1 if reset else 0),
        )
        return FileScan(
            path=path, state=state, events=events, reset_reason=reset,
            lines_read=rows, bad_lines=bad,
        )
