"""Default paths, for a source checkout and for an installed package alike.

* Source checkout (`pip install -e .` or `python -m lifelong_tracker` from the
  repository): defaults resolve relative to the project root, the folder holding
  this project's pyproject.toml - exactly as before.
* Installed package (`pip install .` / a wheel): there is no project folder, so
  defaults resolve relative to the current working directory (`./data/tracker.db`,
  `./pricing.json`, ...). Until a `pricing.json` exists there, prices are read
  from the default table bundled inside the package (`default_pricing.json`); the
  first price edit writes a full copy to `./pricing.json`.

Nothing here reads environment variables or the user's home directory.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PROJECT_NAME = "lifelong-ai-tracker"
PACKAGE_DIR = Path(__file__).resolve().parent
PACKAGED_PRICING_PATH = PACKAGE_DIR / "default_pricing.json"
IS_WINDOWS = os.name == "nt"

_NAME_RE = re.compile(r'^\s*name\s*=\s*["\']' + re.escape(PROJECT_NAME) + r'["\']\s*$', re.M)


def find_source_root(package_dir: Path = PACKAGE_DIR) -> Path | None:
    """The checkout root when the package runs from `<root>/src/lifelong_tracker`, else None."""
    package_dir = Path(package_dir)
    if package_dir.parent.name != "src":
        return None
    root = package_dir.parent.parent
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return root if _NAME_RE.search(text) else None


def resolve_project_root(package_dir: Path = PACKAGE_DIR, cwd: Path | None = None) -> Path:
    """Source checkout root, or (installed package) the current working directory."""
    root = find_source_root(package_dir)
    if root is not None:
        return root
    return Path(cwd if cwd is not None else Path.cwd()).absolute()


def venv_tracker_bin(project_root: Path, windows: bool = IS_WINDOWS) -> Path:
    """The `tracker` entry point inside `<project_root>/.venv` for this OS layout."""
    if windows:
        return Path(project_root) / ".venv" / "Scripts" / "tracker.exe"
    return Path(project_root) / ".venv" / "bin" / "tracker"


def default_tracker_bin(
    project_root: Path, *, source_checkout: bool, windows: bool = IS_WINDOWS,
    python: str | None = None,
) -> Path:
    """Checkout: the project's .venv entry point. Installed: the script beside this Python."""
    if source_checkout:
        return venv_tracker_bin(project_root, windows)
    exe = Path(python if python is not None else sys.executable)
    return exe.parent / ("tracker.exe" if windows else "tracker")


SOURCE_ROOT = find_source_root()
IS_SOURCE_CHECKOUT = SOURCE_ROOT is not None
PROJECT_ROOT = resolve_project_root()
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "tracker.db"
DEFAULT_PRICING_PATH = PROJECT_ROOT / "pricing.json"
DEFAULT_SOURCES_PATH = PROJECT_ROOT / "sources.json"
DEFAULT_SUBSCRIPTIONS_PATH = PROJECT_ROOT / "subscriptions.json"
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "backups"
DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "sync.log"
DEFAULT_PLIST_PATH = PROJECT_ROOT / "deploy" / "com.lifelong-tracker.sync.plist"
DEFAULT_TRACKER_BIN = default_tracker_bin(PROJECT_ROOT, source_checkout=IS_SOURCE_CHECKOUT)


def _same_file(a: Path, b: Path) -> bool:
    return Path(a).absolute() == Path(b).absolute() or Path(a).resolve() == Path(b).resolve()


def pricing_seed(path: Path, default: Path | None = None,
                 packaged: Path = PACKAGED_PRICING_PATH) -> Path | None:
    """The bundled table to fall back to for `path`, or None.

    Only the *default* pricing path falls back (an explicit `--pricing` that does
    not exist stays an error), and only while that file does not exist.
    """
    default = DEFAULT_PRICING_PATH if default is None else default
    if _same_file(path, default) and Path(packaged).is_file():
        return Path(packaged)
    return None


def pricing_read_path(path: Path, default: Path | None = None,
                      packaged: Path = PACKAGED_PRICING_PATH) -> Path:
    """`path` if it exists, else the bundled default when `path` is the default location."""
    path = Path(path)
    if path.exists():
        return path
    seed = pricing_seed(path, default, packaged)
    return seed if seed is not None else path
