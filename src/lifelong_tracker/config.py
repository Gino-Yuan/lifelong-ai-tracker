"""Workspace-anchored default paths.

Defaults resolve relative to the project root (the directory holding
pyproject.toml), never to the user's home directory. Nothing here reads
environment variables.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "tracker.db"
DEFAULT_PRICING_PATH = PROJECT_ROOT / "pricing.json"
DEFAULT_SOURCES_PATH = PROJECT_ROOT / "sources.json"
DEFAULT_SUBSCRIPTIONS_PATH = PROJECT_ROOT / "subscriptions.json"
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "backups"
DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "sync.log"
DEFAULT_PLIST_PATH = PROJECT_ROOT / "deploy" / "com.lifelong-tracker.sync.plist"
DEFAULT_TRACKER_BIN = PROJECT_ROOT / ".venv" / "bin" / "tracker"
