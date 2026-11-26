"""Shared fixtures. Every test runs on fabricated data under tests/fixtures or pytest's
workspace-local tmp dir (--basetemp=.tmp/pytest); no real log directory is touched."""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from lifelong_tracker.collectors import ClaudeCodeCollector
from lifelong_tracker.pricing import PricingTable
from lifelong_tracker.storage import connect

FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_FIXTURES = FIXTURES / "claude_code"
TEST_PRICING = FIXTURES / "pricing_test.json"
CANARY = "FAKE_CONTENT_CANARY_7f3a"

# The browser-side tests run the real app.js under JavaScriptCore's `jsc` shell. It ships
# with macOS; elsewhere it is found on PATH if installed, otherwise those tests are skipped.
_MACOS_JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")


def _find_jsc() -> Path | None:
    if sys.platform == "darwin" and _MACOS_JSC.is_file():
        return _MACOS_JSC
    found = shutil.which("jsc")
    return Path(found) if found else None


JSC = _find_jsc()
requires_jsc = pytest.mark.skipif(JSC is None, reason="JavaScriptCore `jsc` shell not available (macOS only by default)")


@pytest.fixture(autouse=True, scope="session")
def _utc_local_time():
    """Pin the process timezone so local-day / local-hour buckets are deterministic.

    time.tzset() exists only on Unix; on Windows the TZ variable is still set (SQLite and
    CI runners there use UTC by default).
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    if hasattr(time, "tzset"):
        time.tzset()


@pytest.fixture
def pricing() -> PricingTable:
    return PricingTable.load(TEST_PRICING)


@pytest.fixture
def collector() -> ClaudeCodeCollector:
    return ClaudeCodeCollector()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger" / "tracker.db"


@pytest.fixture
def conn(db_path: Path):
    with connect(db_path) as c:
        yield c


@pytest.fixture
def claude_source(tmp_path: Path) -> Path:
    """A private copy of the static Claude Code fixtures (safe to mutate)."""
    dst = tmp_path / "claude_src"
    shutil.copytree(CLAUDE_FIXTURES, dst)
    return dst
