"""Collector plug-in contract and a reusable incremental JSONL reader.

Every collector turns one tool's local log files into `UsageEvent`s. Collectors
only ever read paths under the `source` directory they are handed explicitly;
there is no implicit default location.
"""

from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from lifelong_tracker.fsutil import inode_replaced, stable_inode
from lifelong_tracker.models import FileState, UsageEvent

HEAD_FINGERPRINT_BYTES = 4096


@dataclass
class FileScan:
    """Result of reading one file incrementally."""

    path: Path
    state: FileState
    events: list[UsageEvent] = field(default_factory=list)
    unchanged: bool = False
    reset_reason: str | None = None  # 'replaced' | 'truncated' | 'rewritten'
    lines_read: int = 0  # complete lines consumed in this scan
    bad_lines: int = 0  # undecodable / malformed lines skipped in this scan


class BaseCollector(ABC):
    """Abstract plug-in. Subclasses set `tool` and implement discovery + reading."""

    tool: ClassVar[str]
    display_name: ClassVar[str]
    implemented: ClassVar[bool] = True
    # True when `--source` may name a single file (e.g. an SQLite database)
    # instead of a directory.
    accepts_file: ClassVar[bool] = False

    @abstractmethod
    def discover(self, source: Path) -> list[Path]:
        """Return the log files under `source` this collector understands."""

    @abstractmethod
    def read_incremental(self, path: Path, checkpoint: FileState | None) -> FileScan:
        """Read what is new in `path` since `checkpoint` and return events + new state."""


def _head_hash(path: Path, length: int) -> str:
    if length <= 0:
        return ""
    with path.open("rb") as f:
        return hashlib.sha256(f.read(length)).hexdigest()


class JsonlCollector(BaseCollector):
    """Base for tools that append one JSON object per line.

    Incremental contract:
    * only complete, newline-terminated lines are consumed; a partially written
      trailing line is left for the next sync (offset is not advanced past it);
    * unchanged inode + size + mtime  -> fast skip, file not opened;
    * different inode                  -> file replaced   -> rescan from 0;
      (inodes are masked to 63 bits; 0 = unknown, e.g. some Windows file
      systems, and then only size / mtime / head hash decide);
    * size < stored offset             -> file truncated  -> rescan from 0;
    * first `head_len` bytes changed   -> rewritten in place -> rescan from 0.
    Rescans are safe because storage UPSERTs are idempotent.
    """

    pattern: ClassVar[str] = "*.jsonl"

    def discover(self, source: Path) -> list[Path]:
        root = Path(source)
        files = [
            p for p in root.rglob(self.pattern)
            if p.is_file() and not p.is_symlink()  # never follow links out of `source`
        ]
        return sorted(files)

    @abstractmethod
    def parse_record(self, record: dict[str, Any], path: Path) -> UsageEvent | None:
        """Map one decoded JSON object to an event, or None if it carries no usage.

        Implementations must copy only whitelisted metadata fields.
        """

    def read_incremental(self, path: Path, checkpoint: FileState | None) -> FileScan:
        st = path.stat()
        start = 0
        reset: str | None = None
        prev_lines = 0
        prev_resets = 0

        if checkpoint is not None:
            prev_lines, prev_resets = checkpoint.lines_read, checkpoint.resets
            inode = stable_inode(st)
            if (
                not inode_replaced(checkpoint.inode, inode)
                and st.st_size == checkpoint.size
                and st.st_mtime_ns == checkpoint.mtime_ns
            ):
                return FileScan(path=path, state=checkpoint, unchanged=True)
            if inode_replaced(checkpoint.inode, inode):
                reset = "replaced"
            elif st.st_size < checkpoint.offset:
                reset = "truncated"
            elif checkpoint.head_len and _head_hash(path, checkpoint.head_len) != checkpoint.head_hash:
                reset = "rewritten"
            else:
                start = checkpoint.offset
            if reset is not None:
                prev_lines = 0

        events: list[UsageEvent] = []
        lines = bad = 0
        offset = start
        with path.open("rb") as f:
            f.seek(start)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # writer is mid-line; pick it up next time
                offset += len(raw)
                lines += 1
                text = raw.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except (ValueError, UnicodeDecodeError):
                    bad += 1
                    continue
                if not isinstance(record, dict):
                    bad += 1
                    continue
                try:
                    event = self.parse_record(record, path)
                except (ValueError, TypeError, KeyError, AttributeError):
                    bad += 1
                    continue
                if event is not None:
                    events.append(event)
            post = os.fstat(f.fileno())

        head_len = min(HEAD_FINGERPRINT_BYTES, offset)
        state = FileState(
            inode=stable_inode(post),
            size=max(post.st_size, offset),
            mtime_ns=post.st_mtime_ns,
            offset=offset,
            head_len=head_len,
            head_hash=_head_hash(path, head_len),
            lines_read=prev_lines + lines,
            resets=prev_resets + (1 if reset else 0),
        )
        return FileScan(
            path=path, state=state, events=events, reset_reason=reset,
            lines_read=lines, bad_lines=bad,
        )
