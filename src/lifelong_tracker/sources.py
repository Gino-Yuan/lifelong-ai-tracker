"""Multi-source config (`sources.json`) and fault-tolerant batch sync (`sync-all`).

Config shape (a bare list of sources is also accepted)::

    {"sources": [
        {"name": "claude-code", "tool": "claude-code", "path": "~/.claude/projects", "enabled": true}
    ]}

Optional `"billing": "subscription" | "api"` (default "subscription") says how a
source's usage is paid for: under a flat subscription plan (counted in the plan's
ROI) or pay-as-you-go with an API key (real spend, kept out of the ROI and
reported as direct API spend). Events are attributed to a source by the folder
of the log file they were read from.

`~` is expanded; relative paths resolve against the config file's folder. Keys
starting with `_` are comments; any other unknown key is an error, so a typo such
as `"enable"` never silently changes behaviour.

A failing source (missing path, placeholder collector, parse or database error)
is recorded and the remaining sources still run.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lifelong_tracker.collectors import COLLECTORS, get_collector
from lifelong_tracker.pricing import PricingTable
from lifelong_tracker.sync import SyncReport, sync_source

_ALLOWED_KEYS = {"name", "tool", "path", "enabled", "billing"}
BILLING_SUBSCRIPTION = "subscription"
BILLING_API = "api"
BILLING_MODES = (BILLING_SUBSCRIPTION, BILLING_API)


class SourcesConfigError(ValueError):
    """The sources config file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class SourceSpec:
    name: str
    tool: str
    path: Path
    enabled: bool = True
    billing: str = BILLING_SUBSCRIPTION


@dataclass
class SourceResult:
    spec: SourceSpec
    status: str  # "ok" | "failed" | "disabled"
    error: str | None = None
    report: SyncReport | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def load_sources(config_path: Path | str) -> list[SourceSpec]:
    path = Path(config_path).expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SourcesConfigError(f"config file not found: {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise SourcesConfigError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SourcesConfigError(f"{path} is not valid JSON: {exc}") from exc

    items = raw.get("sources") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise SourcesConfigError(f'{path}: expected {{"sources": [...]}} or a JSON list')

    base = path.resolve().parent
    specs: list[SourceSpec] = []
    seen: set[str] = set()
    for i, item in enumerate(items, start=1):
        where = f"{path.name} source #{i}"
        if not isinstance(item, dict):
            raise SourcesConfigError(f"{where}: must be an object")
        unknown = {k for k in item if not k.startswith("_")} - _ALLOWED_KEYS
        if unknown:
            raise SourcesConfigError(
                f"{where}: unknown key(s) {', '.join(sorted(unknown))}; "
                f"allowed: {', '.join(sorted(_ALLOWED_KEYS))}"
            )
        tool = item.get("tool")
        if not isinstance(tool, str) or not tool:
            raise SourcesConfigError(f"{where}: 'tool' is required")
        if tool not in COLLECTORS:
            raise SourcesConfigError(
                f"{where}: unknown tool {tool!r}; known: {', '.join(sorted(COLLECTORS))}"
            )
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise SourcesConfigError(f"{where}: 'path' is required")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise SourcesConfigError(f"{where}: 'enabled' must be true or false")
        name = item.get("name", tool)
        if not isinstance(name, str) or not name.strip():
            raise SourcesConfigError(f"{where}: 'name' must be a non-empty string")
        name = name.strip()
        billing = item.get("billing", BILLING_SUBSCRIPTION)
        if billing not in BILLING_MODES:
            raise SourcesConfigError(f"{where}: 'billing' must be \"subscription\" or \"api\"")
        if name in seen:
            raise SourcesConfigError(f"{where}: duplicate name {name!r} (give each source a unique 'name')")
        seen.add(name)

        p = Path(raw_path.strip()).expanduser()
        if not p.is_absolute():
            p = base / p
        specs.append(SourceSpec(name=name, tool=tool, path=p, enabled=enabled, billing=billing))
    return specs


def sync_one(conn: sqlite3.Connection, spec: SourceSpec, pricing: PricingTable) -> SourceResult:
    """Sync a single source; never raises for source-level problems."""
    if not spec.enabled:
        return SourceResult(spec, "disabled")
    try:
        collector = get_collector(spec.tool)
        if not collector.implemented:
            return SourceResult(
                spec, "failed",
                f"collector {spec.tool!r} is a placeholder and cannot sync yet",
            )
        if not spec.path.exists():
            return SourceResult(spec, "failed", f"path does not exist: {spec.path}")
        if not spec.path.is_dir() and not (collector.accepts_file and spec.path.is_file()):
            return SourceResult(spec, "failed", f"path is not a directory: {spec.path}")
        report = sync_source(conn, collector, spec.path, pricing)
    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
        if conn.in_transaction:  # defensive: transaction() already rolls back
            conn.execute("ROLLBACK")
        return SourceResult(spec, "failed", f"{type(exc).__name__}: {exc}")
    return SourceResult(spec, "ok", report=report)


def sync_all(
    conn: sqlite3.Connection,
    specs: list[SourceSpec],
    pricing: PricingTable,
    on_result: Callable[[SourceResult], None] | None = None,
) -> list[SourceResult]:
    results = []
    for spec in specs:
        res = sync_one(conn, spec, pricing)
        results.append(res)
        if on_result:
            on_result(res)
    return results
