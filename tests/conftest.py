"""Shared fixtures. Every test runs on fabricated data under tests/fixtures or pytest's
workspace-local tmp dir (--basetemp=.tmp/pytest); no real log directory is touched."""

from __future__ import annotations

import os
import shutil
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


@pytest.fixture(autouse=True, scope="session")
def _utc_local_time():
    """Pin the process timezone so local-day / local-hour buckets are deterministic."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
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
