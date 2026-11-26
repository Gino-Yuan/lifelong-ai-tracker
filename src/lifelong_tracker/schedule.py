"""Render a macOS launchd LaunchAgent plist for the nightly `sync-all`.

This module only *writes a file inside the workspace* and *formats* install
commands as text. It never runs launchctl and never touches ~/Library.
"""

from __future__ import annotations

import plistlib
import shlex
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LABEL = "com.lifelong-tracker.sync"


@dataclass(frozen=True)
class ScheduleSpec:
    label: str
    tracker_bin: Path
    project_root: Path
    config: Path
    db: Path
    pricing: Path
    log_file: Path
    hour: int = 23
    minute: int = 30
    backup_dir: Path | None = None
    keep: int = 14

    def program_arguments(self) -> list[str]:
        args = [
            str(self.tracker_bin), "sync-all",
            "--config", str(self.config),
            "--db", str(self.db),
            "--pricing", str(self.pricing),
            "--quiet",
        ]
        if self.backup_dir is not None:
            args += ["--backup-dir", str(self.backup_dir), "--keep", str(self.keep)]
        return args


def build_plist(spec: ScheduleSpec) -> dict:
    if not (0 <= spec.hour <= 23 and 0 <= spec.minute <= 59):
        raise ValueError("hour must be 0-23 and minute 0-59")
    for name in ("tracker_bin", "project_root", "config", "db", "pricing", "log_file"):
        if not getattr(spec, name).is_absolute():
            raise ValueError(f"{name} must be an absolute path (launchd has no working-dir context)")
    return {
        "Label": spec.label,
        "ProgramArguments": spec.program_arguments(),
        "WorkingDirectory": str(spec.project_root),
        "StartCalendarInterval": {"Hour": spec.hour, "Minute": spec.minute},
        "StandardOutPath": str(spec.log_file),
        "StandardErrorPath": str(spec.log_file),
        "RunAtLoad": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
    }


def render_plist(spec: ScheduleSpec) -> bytes:
    return plistlib.dumps(build_plist(spec), fmt=plistlib.FMT_XML, sort_keys=True)


def install_commands(plist_path: Path, label: str, log_file: Path) -> dict[str, list[str]]:
    """Shell commands for the user to run by hand (absolute, quoted paths)."""
    agent = f"~/Library/LaunchAgents/{plist_path.name}"
    q = shlex.quote
    return {
        "Validate": [f"plutil -lint {q(str(plist_path))}"],
        "Install": [
            "mkdir -p ~/Library/LaunchAgents",
            f"cp {q(str(plist_path))} {agent}",
            f"launchctl bootstrap gui/$(id -u) {agent}",
        ],
        "Run once now (test)": [
            f"launchctl kickstart -k gui/$(id -u)/{label}",
            f"tail -n 40 {q(str(log_file))}",
            f"launchctl print gui/$(id -u)/{label} | grep -E 'state|last exit code'",
        ],
        "Uninstall": [
            f"launchctl bootout gui/$(id -u)/{label}",
            f"rm {agent}",
        ],
    }


def is_privacy_protected(path: Path, home: Path) -> bool:
    """True if `path` sits in a macOS TCC-protected user folder (Documents/Desktop/Downloads)."""
    try:
        rel = path.resolve().relative_to(home.resolve())
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] in {"Documents", "Desktop", "Downloads"}
