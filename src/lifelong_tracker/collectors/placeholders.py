"""Placeholder adapters for tools whose on-disk log format is not yet confirmed.

They register the plug-in name and document exactly which metadata fields are
needed, but refuse to scan anything until a redacted sample is provided and a
real parser is written. No guessing at, or probing of, real user directories.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from lifelong_tracker.collectors.base import BaseCollector, FileScan
from lifelong_tracker.models import FileState


class _PlaceholderCollector(BaseCollector):
    implemented = False
    required_fields: ClassVar[tuple[str, ...]] = ()

    def _refuse(self) -> NotImplementedError:
        fields = "\n  - ".join(self.required_fields)
        return NotImplementedError(
            f"{self.display_name} collector is a placeholder. Provide 1-2 redacted sample "
            f"records containing:\n  - {fields}"
        )

    def discover(self, source: Path) -> list[Path]:
        raise self._refuse()

    def read_incremental(self, path: Path, checkpoint: FileState | None) -> FileScan:
        raise self._refuse()


class T3Collector(_PlaceholderCollector):
    tool = "t3"
    display_name = "T3 Code"
    required_fields = (
        "storage shape: file extension + one-record-per-line JSONL, one JSON per file, or SQLite",
        "event timestamp (and its timezone/format)",
        "model identifier as written (e.g. with provider prefix?)",
        "session / thread id",
        "request id and/or message id (what is unique per model response?)",
        "usage keys for input / output / reasoning(thinking) / cache read / cache write",
        "whether usage is cumulative per streamed chunk or per-turn final",
        "project / workspace path field (only the basename will be kept)",
    )

