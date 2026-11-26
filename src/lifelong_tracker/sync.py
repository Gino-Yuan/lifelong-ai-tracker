"""Sync orchestration: collector -> storage, one atomic transaction per file."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from lifelong_tracker.collectors import BaseCollector
from lifelong_tracker.pricing import PricingTable
from lifelong_tracker.storage import (
    UpsertResult,
    get_checkpoint,
    save_checkpoint,
    transaction,
    upsert_events,
)


@dataclass
class SyncReport:
    tool: str
    source: Path
    files_seen: int = 0
    files_scanned: int = 0
    files_unchanged: int = 0
    files_reset: int = 0
    files_vanished: int = 0
    lines_read: int = 0
    bad_lines: int = 0
    events_parsed: int = 0
    upsert: UpsertResult = field(default_factory=UpsertResult)
    resets: list[tuple[str, str]] = field(default_factory=list)


def sync_source(
    conn: sqlite3.Connection,
    collector: BaseCollector,
    source: Path,
    pricing: PricingTable,
) -> SyncReport:
    source = Path(source).expanduser().resolve()
    if source.is_file():
        if not collector.accepts_file:
            raise NotADirectoryError(f"--source must be a directory for {collector.tool}: {source}")
    elif not source.is_dir():
        raise NotADirectoryError(f"--source is not a directory: {source}")

    report = SyncReport(tool=collector.tool, source=source)
    for path in collector.discover(source):
        report.files_seen += 1
        key = str(path.resolve())
        checkpoint = get_checkpoint(conn, collector.tool, key)
        try:
            scan = collector.read_incremental(path, checkpoint)
        except FileNotFoundError:
            report.files_vanished += 1
            continue
        if scan.unchanged:
            report.files_unchanged += 1
            continue

        # events + checkpoint commit together: a crash never advances the offset
        # past events that were not stored.
        with transaction(conn):
            checkpoint_id = save_checkpoint(conn, collector.tool, key, scan.state)
            report.upsert += upsert_events(conn, scan.events, pricing, checkpoint_id)

        report.files_scanned += 1
        report.lines_read += scan.lines_read
        report.bad_lines += scan.bad_lines
        report.events_parsed += len(scan.events)
        if scan.reset_reason:
            report.files_reset += 1
            report.resets.append((key, scan.reset_reason))
    return report
