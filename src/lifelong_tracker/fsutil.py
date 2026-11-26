"""Small cross-platform file helpers (POSIX and Windows).

* `stable_inode` - a file's inode masked to a non-negative signed 63-bit value.
  NTFS file IDs can use all 64 bits, which overflows SQLite's signed INTEGER;
  masking keeps the value storable and stays identical for every POSIX inode
  below 2**63. 0 means "no usable inode": change detection then relies on
  size / mtime_ns / head hash only (see `inode_replaced`).
* `replace` - `os.replace` that retries a short-lived Windows `PermissionError`
  (antivirus, indexer or a reader briefly holding the target open).
* `safe_chmod` - copy POSIX permission bits where they mean something; never fails
  a write on Windows or on file systems without permissions.
* `atomic_write_text` - temp file in the same folder + fsync + `replace`, UTF-8 and
  LF line endings on every platform, so a file is never half-written.
"""

from __future__ import annotations

import os
import stat as stat_mod
import tempfile
import time
from pathlib import Path
from typing import Callable

INODE_MASK = 0x7FFF_FFFF_FFFF_FFFF
IS_WINDOWS = os.name == "nt"
REPLACE_RETRIES = 3
REPLACE_RETRY_DELAY = 0.02  # seconds


def stable_inode(st: os.stat_result) -> int:
    """`st.st_ino` folded into SQLite's non-negative INTEGER range (0 = unknown)."""
    return int(st.st_ino) & INODE_MASK


def inode_replaced(old: int, new: int) -> bool:
    """True only when both inodes are known and differ (0 on either side = can't tell)."""
    return bool(old) and bool(new) and old != new


def replace(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    retries: int | None = None,
    delay: float = REPLACE_RETRY_DELAY,
    _replace: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] | None = None,
    _sleep: Callable[[float], None] | None = None,
) -> None:
    """`os.replace(src, dst)`, retried on `PermissionError` on Windows (3 x 20 ms by default).

    On POSIX a `PermissionError` is permanent, so it is raised at once. The last
    error is re-raised when every retry fails.
    """
    if retries is None:
        retries = REPLACE_RETRIES if IS_WINDOWS else 0
    do_replace = _replace or os.replace  # looked up per call (tests may patch os.replace)
    sleep = _sleep or time.sleep
    for attempt in range(retries + 1):
        try:
            do_replace(src, dst)
            return
        except PermissionError:
            if attempt >= retries:
                raise
            sleep(delay)


def safe_chmod(path: str | os.PathLike[str], mode: int) -> None:
    """Apply POSIX permission bits; a no-op on Windows (where chmod can only set read-only,
    which would block the next replace) and on file systems that do not support it."""
    if IS_WINDOWS:
        return
    try:
        os.chmod(path, stat_mod.S_IMODE(mode))
    except (NotImplementedError, OSError):
        pass


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    """Write `text` (UTF-8, LF newlines) via temp file + fsync + replace; keeps the old mode."""
    path = Path(path)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        safe_chmod(tmp, mode)
        replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
