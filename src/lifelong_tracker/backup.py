"""Online, WAL-safe ledger backups via `sqlite3.Connection.backup()`.

The source is opened read-only (URI `mode=ro`), copied page-by-page into a
`.partial` file, verified with `PRAGMA integrity_check`, switched to a
single-file journal mode (no -wal / -shm beside the copy) and only then renamed
to `tracker-YYYYMMDD-HHMMSS.db`. Rotation deletes only files matching that
pattern in the backup folder, never anything else.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from lifelong_tracker import fsutil

BACKUP_RE = re.compile(r"^tracker-(\d{8})-(\d{6})(?:-(\d+))?\.db$")


@dataclass
class BackupResult:
    path: Path
    size_bytes: int
    events: int
    integrity: str
    removed: list[Path] = field(default_factory=list)
    kept: list[Path] = field(default_factory=list)


def _sort_key(p: Path) -> tuple[str, str, int]:
    m = BACKUP_RE.match(p.name)
    assert m is not None
    return (m.group(1), m.group(2), int(m.group(3) or 0))


def list_backups(out_dir: Path | str) -> list[Path]:
    """Backups in `out_dir`, oldest first (only names produced by this module)."""
    d = Path(out_dir)
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if p.is_file() and BACKUP_RE.match(p.name)), key=_sort_key)


def rotate_backups(out_dir: Path | str, keep: int) -> tuple[list[Path], list[Path]]:
    """Delete all but the newest `keep` backups. Returns (removed, kept)."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    backups = list_backups(out_dir)
    removed, kept = backups[:-keep], backups[-keep:]
    for p in removed:
        p.unlink(missing_ok=True)
    return removed, kept


def _target_path(out_dir: Path, now: datetime) -> Path:
    stem = f"tracker-{now:%Y%m%d-%H%M%S}"
    candidate = out_dir / f"{stem}.db"
    n = 1
    while candidate.exists():  # two backups within one second
        n += 1
        candidate = out_dir / f"{stem}-{n}.db"
    return candidate


def backup_database(
    db_path: Path | str,
    out_dir: Path | str,
    keep: int = 14,
    now: datetime | None = None,
) -> BackupResult:
    src_path = Path(db_path).resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"no ledger at {src_path}")
    if keep < 1:
        raise ValueError("keep must be at least 1")
    out = Path(out_dir).resolve()
    if out == src_path.parent:
        raise ValueError("backup folder must differ from the ledger's folder")
    out.mkdir(parents=True, exist_ok=True)

    target = _target_path(out, now or datetime.now())
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)

    src = sqlite3.connect(f"{src_path.as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(partial))
        try:
            src.backup(dst)  # consistent snapshot, includes committed WAL frames
            dst.execute("PRAGMA journal_mode=DELETE")  # self-contained single file
            integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
            has_events = dst.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
            ).fetchone()
            events = dst.execute("SELECT COUNT(*) FROM events").fetchone()[0] if has_events else 0
        finally:
            dst.close()
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        src.close()

    if integrity != "ok":
        partial.unlink(missing_ok=True)
        raise sqlite3.DatabaseError(f"backup failed integrity_check: {integrity}")
    fsutil.replace(partial, target)  # retried on a transient Windows lock

    removed, kept = rotate_backups(out, keep)
    return BackupResult(
        path=target,
        size_bytes=target.stat().st_size,
        events=events,
        integrity=integrity,
        removed=removed,
        kept=kept,
    )
