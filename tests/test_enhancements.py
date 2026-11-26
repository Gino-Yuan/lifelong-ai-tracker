"""Cache savings, subscription ROI, Sync Now, personal records, grouped model variants and
keyboard shortcuts.

Everything runs on fabricated data: events built in code, or private copies of
tests/fixtures, under pytest's workspace-local tmp dir. Every app gets explicit tmp paths
for pricing / subscriptions / sources, so no real sources.json, data/ or home dir is read.
"""

from __future__ import annotations

import itertools
import json
import math
import re
import shutil
import sqlite3
import subprocess
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import CLAUDE_FIXTURES, FIXTURES, TEST_PRICING
from lifelong_tracker import analytics, cli
from lifelong_tracker import sources as sources_mod
from lifelong_tracker.config import DEFAULT_SUBSCRIPTIONS_PATH, PROJECT_ROOT
from lifelong_tracker.models import UsageEvent
from lifelong_tracker.pricing import PricingTable, model_family, set_model_price
from lifelong_tracker.storage import connect
from lifelong_tracker.storage.db import transaction, upsert_events
from lifelong_tracker.subscriptions import (
    DAYS_PER_MONTH,
    MAX_MONTHLY_USD,
    SubscriptionsError,
    load_subscriptions,
    save_subscriptions,
    subscription_roi,
    validate_plans,
)
from lifelong_tracker.collectors import TOOL_ORDER
from lifelong_tracker.web.app import STATIC_DIR, create_app

TODAY = date(2026, 11, 26)
BASE = "http://127.0.0.1:8765"
JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
JS = FIXTURES / "js"
EXTRA_PRICES = {
    "gemini-4-neon": {"input": 3.0, "output": 15.0, "thinking": 15.0, "cache_read": 0.3, "cache_write": 0.0},
    # cache reads dearer than input: savings must clamp at 0
    "odd-cache-model": {"input": 1.0, "output": 2.0, "cache_read": 2.5, "cache_write": 0.0},
}
_ids = itertools.count(1)


# ------------------------------------------------------------------ helpers

@pytest.fixture
def pricing_path(tmp_path) -> Path:
    p = tmp_path / "prices" / "pricing.json"
    p.parent.mkdir()
    raw = json.loads(TEST_PRICING.read_text())
    raw["models"].update(EXTRA_PRICES)
    p.write_text(json.dumps(raw, indent=2))
    return p


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "ledger" / "tracker.db"


def ev(ts: str, *, tool="claude-code", model="claude-sonnet-4-5", session="sess-a", project="proj-x",
       inp=0, out=0, think=0, cr=0, cw=0, msg=None) -> UsageEvent:
    return UsageEvent(
        timestamp=datetime.fromisoformat(ts).replace(tzinfo=timezone.utc),
        tool=tool, model=model, project_name=project, session_id=session,
        message_id=msg or f"msg-fx-{next(_ids)}",
        input_tokens=inp, output_tokens=out, thinking_tokens=think,
        cache_read_tokens=cr, cache_write_tokens=cw,
    )


def put(db: Path, pricing_path: Path, events) -> None:
    with connect(db) as conn:
        with transaction(conn):
            upsert_events(conn, events, PricingTable.load(pricing_path))


def make_app(db, pricing_path, *, subs=None, sources=None):
    return create_app(db, pricing_path, today=lambda: TODAY, subscriptions_path=subs, sources_path=sources)


def make_client(db, pricing_path, **kw) -> TestClient:
    return TestClient(make_app(db, pricing_path, **kw), base_url=BASE)


def fees_of(plans) -> dict:
    """{tool: fee} of legacy (undated, single-period) plans."""
    assert all(len(ps) == 1 and ps[0].legacy for ps in plans.values()), plans
    return {t: ps[0].monthly_usd for t, ps in plans.items()}


def ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def copy_sources(tmp_path: Path) -> tuple[Path, Path]:
    """Private copies of the Claude Code / Antigravity fixtures."""
    claude = tmp_path / "src" / "claude"
    brain = tmp_path / "src" / "brain"
    shutil.copytree(CLAUDE_FIXTURES, claude)
    shutil.copytree(FIXTURES / "antigravity" / "brain", brain)
    return claude, brain


def write_sources(path: Path, entries) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sources": entries}, indent=2))
    return path


# ================================================================== cache savings

def _per_event_savings(events, table: PricingTable) -> float:
    total = []
    for e in events:
        hit = table.lookup(e.model)
        if hit is None or not e.cache_read_tokens:
            continue
        price = hit[1]
        total.append(e.cache_read_tokens / 1_000_000 * max(0.0, price.input - price.cache_read))
    return math.fsum(total)


SAVINGS_EVENTS = [
    ("2026-11-20T10:00:00", "claude-code", "claude-sonnet-4-5-20250929", 1_000_000),
    ("2026-11-20T11:00:00", "claude-code", "claude-sonnet-4-5", 234_567),
    ("2026-11-21T09:00:00", "claude-code", "claude-haiku-4-5", 333),
    ("2026-11-22T09:00:00", "claude-code", "claude-opus-4", 7),
    ("2026-11-22T10:00:00", "claude-code", "claude-sonnet-4-5", 0),
    ("2026-11-23T08:00:00", "antigravity", "gemini-4-neon-high", 12_345),
    ("2026-11-23T09:00:00", "antigravity", "odd-cache-model", 9_999),
    ("2026-11-24T09:00:00", "claude-code", "mystery-model-x", 500),
    ("2026-01-05T09:00:00", "claude-code", "claude-sonnet-4-5", 10_000),
]


@pytest.fixture
def savings_ledger(db, pricing_path):
    events = [ev(ts, tool=tool, model=model, inp=10, cr=cr) for ts, tool, model, cr in SAVINGS_EVENTS]
    put(db, pricing_path, events)
    return events


def test_cache_savings_matches_per_event_formula(db, pricing_path, savings_ledger):
    table = PricingTable.load(pricing_path)
    with connect(db) as conn:
        cs = analytics.cache_savings(conn, table, "lifelong", TODAY)
    expected = _per_event_savings(savings_ledger, table)
    # hand-computed: sonnet 1,244,567 * 2.7, haiku 333 * 0.9, opus-4 7 * 13.5, neon 12,345 * 2.7, odd 0
    by_hand = (1_244_567 * 2.7 + 333 * 0.9 + 7 * 13.5 + 12_345 * 2.7) / 1_000_000
    assert cs.usd == pytest.approx(expected, rel=1e-12, abs=1e-15)
    assert cs.usd == pytest.approx(by_hand, rel=1e-12)
    assert cs.cache_read_tokens == 1_244_567 + 333 + 7 + 12_345 + 9_999  # odd model counted, saves 0
    assert cs.events == 7
    assert cs.unpriced_cache_read_tokens == 500


def test_cache_savings_clamps_negative_discount(db, pricing_path):
    put(db, pricing_path, [ev("2026-11-23T09:00:00", model="odd-cache-model", cr=1_000_000)])
    with connect(db) as conn:
        cs = analytics.cache_savings(conn, PricingTable.load(pricing_path), "lifelong", TODAY)
    assert cs.usd == 0.0 and cs.cache_read_tokens == 1_000_000 and cs.events == 1


@pytest.mark.parametrize("range_name, kw, models", [
    ("lifelong", {"tool": "antigravity"}, {"gemini-4-neon-high", "odd-cache-model"}),
    ("lifelong", {"model": "claude-haiku-4-5"}, {"claude-haiku-4-5"}),
    ("7d", {}, None),
    ("ytd", {"tool": "claude-code"}, None),
])
def test_cache_savings_follows_scope(db, pricing_path, savings_ledger, range_name, kw, models):
    table = PricingTable.load(pricing_path)
    start, end = analytics.range_bounds(range_name, TODAY)
    scoped = [
        e for e in savings_ledger
        if (start is None or start <= e.timestamp.date() <= end)
        and (kw.get("tool") in (None, e.tool)) and (kw.get("model") in (None, e.model))
    ]
    if models is not None:
        assert {e.model for e in scoped if e.cache_read_tokens} == models
    with connect(db) as conn:
        cs = analytics.cache_savings(conn, table, range_name, TODAY, **kw)
    assert cs.usd == pytest.approx(_per_event_savings(scoped, table), rel=1e-12, abs=1e-15)


def test_cache_savings_empty_ledger(db, pricing_path):
    with connect(db) as conn:
        cs = analytics.cache_savings(conn, PricingTable.load(pricing_path), "lifelong", TODAY)
    assert cs == analytics.CacheSavings()


def test_overview_reports_cache_savings_and_follows_live_pricing(db, pricing_path, savings_ledger):
    c = make_client(db, pricing_path)
    body = c.get("/api/overview", params={"range": "lifelong"}).json()
    table = PricingTable.load(pricing_path)
    assert body["cache_savings"]["usd"] == pytest.approx(_per_event_savings(savings_ledger, table), rel=1e-12)
    assert body["cache_savings"]["unpriced_cache_read_tokens"] == 500
    assert set(body["cache_savings"]) == {"usd", "cache_read_tokens", "events", "unpriced_cache_read_tokens"}
    # tool / model filters reach the figure
    only = c.get("/api/overview", params={"range": "lifelong", "model": "claude-opus-4"}).json()
    assert only["cache_savings"]["usd"] == pytest.approx(7 * 13.5 / 1e6, rel=1e-12)
    # a Set Price edit changes the savings immediately (no reprice needed)
    set_model_price(pricing_path, "claude-haiku-4-5", {"input": 1.0, "output": 5.0, "cache_read": 0.0,
                                                       "cache_write": 1.25}, updated="2026-11-26")
    after = c.get("/api/overview", params={"range": "lifelong", "model": "claude-haiku-4-5"}).json()
    assert after["cache_savings"]["usd"] == pytest.approx(333 * 1.0 / 1e6, rel=1e-12)


def test_overview_without_ledger_has_empty_new_fields(db, pricing_path):
    body = make_client(db, pricing_path).get("/api/overview").json()
    assert body["cache_savings"] == {"usd": 0.0, "cache_read_tokens": 0, "events": 0, "unpriced_cache_read_tokens": 0}
    assert body["records"] == {"peak_day": None, "peak_cost_day": None, "heaviest_session": None, "longest_streak": None}
    assert body["subscription"] is None and body["subscription_error"] is None
    assert not db.exists()


# ================================================================== subscriptions.json

def test_load_missing_file_is_empty(tmp_path):
    assert load_subscriptions(tmp_path / "nope.json") == {}


def test_load_valid_file_skips_comments(tmp_path):
    p = tmp_path / "subscriptions.json"
    p.write_text(json.dumps({"_comment": "USD per month", "claude-code": 20, "codex": 20.5, "opencode": 0}))
    assert fees_of(load_subscriptions(p)) == {"claude-code": 20.0, "codex": 20.5, "opencode": 0.0}


@pytest.mark.parametrize("text, needle", [
    ("{not json", "not valid JSON"),
    ("[1, 2]", "JSON object"),
    ('"x"', "JSON object"),
    ('{"cursor": 20}', "unknown tool"),
    ('{"claude-code": "20"}', "must be a number"),
    ('{"claude-code": true}', "must be a number"),
    ('{"claude-code": null}', "must be a number"),
    ('{"claude-code": -0.01}', "between 0 and"),
    ('{"claude-code": 10000.01}', "between 0 and"),
    ('{"claude-code": NaN}', "between 0 and"),
    ('{"claude-code": Infinity}', "between 0 and"),
])
def test_load_rejects_invalid_files(tmp_path, text, needle):
    p = tmp_path / "subscriptions.json"
    p.write_text(text)
    with pytest.raises(SubscriptionsError, match=needle):
        load_subscriptions(p)


def test_validate_fees_bounds():
    assert fees_of(validate_plans({"codex": 0, "t3": MAX_MONTHLY_USD})) == {"codex": 0.0, "t3": MAX_MONTHLY_USD}
    with pytest.raises(SubscriptionsError):
        validate_plans(["claude-code"])
    with pytest.raises(SubscriptionsError):
        validate_plans({1: 2})


def test_save_is_ordered_keeps_comments_and_leaves_no_temp_files(tmp_path):
    p = tmp_path / "cfg" / "subscriptions.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"_comment": "keep me", "codex": 1}))
    stored = save_subscriptions(p, {"codex": 20, "claude-code": 20.0, "antigravity": 0})
    assert fees_of(stored) == {"claude-code": 20.0, "antigravity": 0.0, "codex": 20.0}
    raw = json.loads(p.read_text())
    assert list(raw) == ["_comment", "subscriptions"]
    assert list(raw["subscriptions"]) == ["claude-code", "antigravity", "codex"]
    assert raw["_comment"] == "keep me"
    assert sorted(x.name for x in p.parent.iterdir()) == ["subscriptions.json"]
    assert load_subscriptions(p) == stored


def test_save_replaces_a_broken_file_and_rejects_bad_fees(tmp_path):
    p = tmp_path / "subscriptions.json"
    p.write_text("{broken")
    assert fees_of(save_subscriptions(p, {"opencode": 5})) == {"opencode": 5.0}
    before = p.read_text()
    with pytest.raises(SubscriptionsError):
        save_subscriptions(p, {"opencode": -5})
    with pytest.raises(SubscriptionsError):
        save_subscriptions(p, {"nope": 5})
    assert p.read_text() == before  # a rejected save never touches the file


# ------------------------------------------------------------------ ROI math

@pytest.fixture
def roi_ledger(db, pricing_path):
    put(db, pricing_path, [
        ev("2026-10-01T12:00:00", tool="antigravity", model="gemini-4-neon", inp=1_000_000, session="ag-1"),
        ev("2026-11-01T12:00:00", model="claude-sonnet-4-5", inp=1_000_000, session="cc-1"),  # $3.00
        ev("2026-11-20T12:00:00", model="claude-opus-4", inp=1_000_000, session="cc-2"),      # $15.00
        ev("2026-11-25T12:00:00", model="mystery-model-x", inp=500, session="cc-3"),          # unpriced
        ev("2025-06-01T12:00:00", model="claude-haiku-4-5", inp=2_000_000, session="cc-0"),   # $2.00
    ])
    return db


@pytest.mark.parametrize("range_name, days, value", [
    ("today", 1, 0.0),
    ("7d", 7, 15.0),  # Nov 20-26 holds the opus event
    ("30d", 30, 18.0),
    ("ytd", 330, 18.0),
    ("last_year", 365, 2.0),
    ("lifelong", (TODAY - date(2025, 6, 1)).days + 1, 20.0),
])
def test_roi_prorates_plan_cost_by_days(roi_ledger, range_name, days, value):
    with connect(roi_ledger) as conn:
        roi = subscription_roi(conn, {"claude-code": 20.0, "codex": 0.0}, range_name, TODAY)
    assert roi.days == days
    assert roi.monthly_usd == 20.0
    assert roi.plan_cost_usd == pytest.approx(20.0 * days / DAYS_PER_MONTH, rel=1e-12)
    assert roi.value_usd == pytest.approx(value, rel=1e-12)
    assert roi.multiple == pytest.approx(value / roi.plan_cost_usd, rel=1e-12)
    assert [t.tool for t in roi.tools] == ["claude-code"]
    assert DAYS_PER_MONTH == pytest.approx(30.436875)


def test_roi_lifelong_starts_at_first_active_day_of_paid_tools(db, pricing_path):
    put(db, pricing_path, [
        ev("2026-10-01T12:00:00", tool="antigravity", model="gemini-4-neon", inp=1_000_000),
        ev("2026-11-01T12:00:00", model="claude-sonnet-4-5", inp=1_000_000),
    ])
    with connect(db) as conn:
        cc = subscription_roi(conn, {"claude-code": DAYS_PER_MONTH}, "lifelong", TODAY)  # $1 / day
        both = subscription_roi(conn, {"claude-code": 10.0, "antigravity": 10.0}, "lifelong", TODAY)
    assert cc.days == 26 and cc.plan_cost_usd == pytest.approx(26.0) and cc.value_usd == pytest.approx(3.0)
    assert both.days == 57 and both.value_usd == pytest.approx(6.0)
    assert [t.tool for t in both.tools] == ["claude-code", "antigravity"]


def test_roi_none_without_paid_plans_in_scope(roi_ledger):
    with connect(roi_ledger) as conn:
        assert subscription_roi(conn, {}, "30d", TODAY) is None
        assert subscription_roi(conn, {"claude-code": 0.0, "codex": 0}, "30d", TODAY) is None
        assert subscription_roi(conn, {"claude-code": 20.0}, "30d", TODAY, tool="antigravity") is None
        empty = subscription_roi(conn, {"codex": 20.0}, "lifelong", TODAY)
    assert empty.days == 1 and empty.value_usd == 0.0 and empty.multiple == 0.0  # no codex usage yet


def test_roi_counts_unpriced_events(roi_ledger):
    with connect(roi_ledger) as conn:
        roi = subscription_roi(conn, {"claude-code": 20.0}, "7d", TODAY)
    assert roi.unpriced_events == 1


# ------------------------------------------------------------------ /api/subscriptions

def _subs_file(tmp_path, data=None) -> Path:
    p = tmp_path / "cfg" / "subscriptions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        p.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return p


def test_get_subscriptions_disabled_server(db, pricing_path):
    body = make_client(db, pricing_path).get("/api/subscriptions").json()
    assert body["writable"] is False and body["configured"] is False and body["error"] is None
    assert [t["tool"] for t in body["tools"]] == list(TOOL_ORDER)
    assert all(t["monthly_usd"] == 0.0 for t in body["tools"])
    assert body["currency"] == "USD" and body["period"] == "month"
    assert body["days_per_month"] == pytest.approx(DAYS_PER_MONTH) and body["max_monthly_usd"] == MAX_MONTHLY_USD


def test_get_subscriptions_reads_file(db, pricing_path, tmp_path):
    subs = _subs_file(tmp_path, {"claude-code": 20, "codex": 20})
    body = make_client(db, pricing_path, subs=subs).get("/api/subscriptions").json()
    fees = {t["tool"]: t["monthly_usd"] for t in body["tools"]}
    assert fees == {"claude-code": 20.0, "antigravity": 0.0, "opencode": 0.0, "codex": 20.0, "t3": 0.0}
    assert body["writable"] is True and body["configured"] is True
    assert {t["tool"]: t["label"] for t in body["tools"]}["claude-code"] == "Claude Code"


def test_broken_subscriptions_file_is_reported_not_fatal(db, pricing_path, tmp_path, roi_ledger):
    subs = _subs_file(tmp_path, "{oops")
    c = make_client(db, pricing_path, subs=subs)
    body = c.get("/api/subscriptions").json()
    assert "not valid JSON" in body["error"] and all(t["monthly_usd"] == 0 for t in body["tools"])
    ov = c.get("/api/overview").json()
    assert ov["subscription"] is None and "not valid JSON" in ov["subscription_error"]


def test_post_subscriptions_saves_and_drives_roi(db, pricing_path, tmp_path, roi_ledger):
    subs = _subs_file(tmp_path)
    c = make_client(db, pricing_path, subs=subs)
    assert c.get("/api/overview").json()["subscription"] is None
    r = c.post("/api/subscriptions", json={"fees": {"claude-code": 20, "antigravity": 0, "codex": 0}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok" and r.json()["configured"] is True
    assert json.loads(subs.read_text()) == {"subscriptions": {"claude-code": 20.0, "antigravity": 0.0, "codex": 0.0}}
    ov = c.get("/api/overview", params={"range": "30d"}).json()
    sub = ov["subscription"]
    assert sub["days"] == 30 and sub["monthly_usd"] == 20.0 and sub["basis"] == "tool"
    assert sub["plan_cost_usd"] == pytest.approx(20 * 30 / DAYS_PER_MONTH)
    assert sub["value_usd"] == pytest.approx(18.0) and sub["multiple"] == pytest.approx(18.0 / sub["plan_cost_usd"])
    assert sub["tools"] == [{"tool": "claude-code", "label": "Claude Code", "monthly_usd": 20.0,
                             "paid_days": 30, "plan_cost_usd": pytest.approx(sub["plan_cost_usd"]),
                             "value_usd": pytest.approx(18.0), "unpriced_events": 1}]
    assert (sub["start"], sub["end"]) == ("2026-10-28", "2026-11-26")
    # the model filter does not apply to a plan; the tool filter does
    assert c.get("/api/overview", params={"range": "30d", "model": "claude-opus-4"}).json()["subscription"] == sub
    assert c.get("/api/overview", params={"range": "30d", "tool": "antigravity"}).json()["subscription"] is None
    # all-zero fees hide ROI
    c.post("/api/subscriptions", json={"fees": {"claude-code": 0}})
    assert c.get("/api/overview").json()["subscription"] is None


@pytest.mark.parametrize("payload", [
    {"fees": {"claude-code": -1}},
    {"fees": {"claude-code": MAX_MONTHLY_USD + 1}},
    {"fees": {"claude-code": "20"}},
    {"fees": {"claude-code": True}},
    {"fees": {"claude-code": None}},
    {"fees": {"cursor": 20}},
    {"fees": {"Claude-Code": 20}},
    {"fees": {"_comment": 1}},
    {"fees": ["claude-code"]},
    {"fees": {"claude-code": 20}, "extra": 1},
    {},
    {"fees": {f"tool-{i}": 1 for i in range(33)}},
])
def test_post_subscriptions_rejects_bad_payloads(db, pricing_path, tmp_path, payload):
    subs = _subs_file(tmp_path, {"codex": 7})
    before = subs.read_text()
    r = make_client(db, pricing_path, subs=subs).post("/api/subscriptions", json=payload)
    assert r.status_code == 422, r.text
    assert subs.read_text() == before


@pytest.mark.parametrize("raw", ['{"fees": {"codex": NaN}}', '{"fees": {"codex": Infinity}}'])
def test_post_subscriptions_rejects_non_finite(db, pricing_path, tmp_path, raw):
    subs = _subs_file(tmp_path)
    r = make_client(db, pricing_path, subs=subs).post(
        "/api/subscriptions", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert not subs.exists()


def test_post_subscriptions_disabled_is_503(db, pricing_path):
    r = make_client(db, pricing_path).post("/api/subscriptions", json={"fees": {"codex": 1}})
    assert r.status_code == 503


CSRF_CASES = [
    ({"content": "{}", "headers": {"Content-Type": "text/plain"}}, 415),
    ({"content": "fees=1", "headers": {"Content-Type": "application/x-www-form-urlencoded"}}, 415),
    ({"content": "{}", "headers": {"Content-Type": "multipart/form-data; boundary=x"}}, 415),
    ({"json": {}, "headers": {"Sec-Fetch-Site": "cross-site"}}, 403),
    ({"json": {}, "headers": {"Sec-Fetch-Site": "same-site"}}, 403),
    ({"json": {}, "headers": {"Origin": "http://evil.example"}}, 403),
    ({"json": {}, "headers": {"Origin": "http://127.0.0.1:9999"}}, 403),
    ({"json": {}, "headers": {"Origin": "null"}}, 403),
    ({"content": json.dumps({"fees": {}, "pad": "x" * 5000}), "headers": {"Content-Type": "application/json"}}, 413),
]


@pytest.mark.parametrize("kw, status", CSRF_CASES)
def test_post_subscriptions_csrf_guard(db, pricing_path, tmp_path, kw, status):
    subs = _subs_file(tmp_path, {"codex": 7})
    before = subs.read_text()
    kw = dict(kw)
    if "json" in kw:
        kw["json"] = {"fees": {"codex": 99}}
    r = make_client(db, pricing_path, subs=subs).post("/api/subscriptions", **kw)
    assert r.status_code == status, r.text
    assert subs.read_text() == before


def test_post_subscriptions_rejects_foreign_host(db, pricing_path, tmp_path):
    subs = _subs_file(tmp_path, {"codex": 7})
    r = make_client(db, pricing_path, subs=subs).post(
        "/api/subscriptions", json={"fees": {"codex": 1}}, headers={"Host": "evil.example.com"})
    assert r.status_code in (400, 403)
    assert json.loads(subs.read_text()) == {"codex": 7}


def test_subscriptions_same_origin_post_allowed(db, pricing_path, tmp_path):
    subs = _subs_file(tmp_path)
    r = make_client(db, pricing_path, subs=subs).post(
        "/api/subscriptions", json={"fees": {"codex": 1}},
        headers={"Origin": BASE, "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200 and json.loads(subs.read_text()) == {"subscriptions": {"codex": 1.0}}


# ================================================================== POST /api/sync

@pytest.fixture
def sync_env(tmp_path, db, pricing_path):
    claude, brain = copy_sources(tmp_path)
    cfg = write_sources(tmp_path / "cfg" / "sources.json", [
        {"name": "claude-main", "tool": "claude-code", "path": str(claude)},
        {"name": "ag", "tool": "antigravity", "path": str(brain)},
        {"name": "codex-off", "tool": "codex", "path": str(tmp_path / "nowhere.sqlite"), "enabled": False},
    ])
    return {"db": db, "pricing": pricing_path, "sources": cfg, "claude": claude, "brain": brain}


def test_sync_not_configured(db, pricing_path, tmp_path):
    for sources in (None, tmp_path / "missing" / "sources.json"):
        r = make_client(db, pricing_path, sources=sources).post("/api/sync", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "not_configured"
        assert body["message"].startswith("No sources.json found.") and "sources.example.json" in body["message"]
        assert body["sources"] == [] and body["by_tool"] == []
    assert not db.exists()  # nothing was opened for writing


@pytest.mark.parametrize("text, needle", [
    ("{nope", "not valid JSON"),
    (json.dumps({"sources": [{"tool": "cursor", "path": "x"}]}), "unknown tool"),
    (json.dumps({"sources": [{"tool": "codex"}]}), "'path' is required"),
    (json.dumps({"items": []}), "expected"),
])
def test_sync_invalid_config_is_422(db, pricing_path, tmp_path, text, needle):
    cfg = tmp_path / "cfg" / "sources.json"
    cfg.parent.mkdir()
    cfg.write_text(text)
    r = make_client(db, pricing_path, sources=cfg).post("/api/sync", json={})
    assert r.status_code == 422
    assert needle in r.json()["detail"] and r.json()["detail"].startswith("sources.json is invalid")


def test_sync_success_reports_counts_per_tool(sync_env):
    c = make_client(sync_env["db"], sync_env["pricing"], sources=sync_env["sources"])
    r = c.post("/api/sync", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert [s["name"] for s in body["sources"]] == ["claude-main", "ag", "codex-off"]
    assert [s["status"] for s in body["sources"]] == ["ok", "ok", "disabled"]
    with connect(sync_env["db"]) as conn:
        per_tool = dict(conn.execute("SELECT tool, COUNT(*) FROM events GROUP BY tool").fetchall())
    assert {t["tool"]: t["inserted"] for t in body["by_tool"]} == per_tool
    assert [t["tool"] for t in body["by_tool"]] == ["claude-code", "antigravity"]
    assert body["by_tool"][0]["label"] == "Claude Code"
    assert body["totals"] == {"inserted": sum(per_tool.values()), "updated": 0, "failed": 0, "ok": 2}
    assert isinstance(body["duration_ms"], int) and body["duration_ms"] >= 0
    # idempotent: a second press finds nothing new
    again = c.post("/api/sync", json={}).json()
    assert again["status"] == "ok" and again["totals"]["inserted"] == 0 and again["totals"]["updated"] == 0
    assert all(t["inserted"] == 0 for t in again["by_tool"])
    # the dashboard sees the new rows right away
    assert c.get("/api/overview").json()["totals"]["events"] == sum(per_tool.values())


def test_sync_partial_and_failed(sync_env, tmp_path):
    cfg = write_sources(sync_env["sources"], [
        {"name": "claude-main", "tool": "claude-code", "path": str(sync_env["claude"])},
        {"name": "gone", "tool": "antigravity", "path": str(tmp_path / "does-not-exist")},
    ])
    c = make_client(sync_env["db"], sync_env["pricing"], sources=cfg)
    body = c.post("/api/sync", json={}).json()
    assert body["status"] == "partial"
    gone = body["sources"][1]
    assert gone["status"] == "failed" and "path does not exist" in gone["error"] and gone["inserted"] == 0
    assert body["totals"]["failed"] == 1 and body["totals"]["ok"] == 1 and body["totals"]["inserted"] > 0
    assert [t["tool"] for t in body["by_tool"]] == ["claude-code"]

    write_sources(cfg, [{"name": "gone", "tool": "antigravity", "path": str(tmp_path / "does-not-exist")}])
    assert c.post("/api/sync", json={}).json()["status"] == "failed"


def test_sync_reads_the_configured_file_fresh_each_time(sync_env):
    c = make_client(sync_env["db"], sync_env["pricing"], sources=sync_env["sources"])
    write_sources(sync_env["sources"], [{"name": "ag", "tool": "antigravity", "path": str(sync_env["brain"])}])
    body = c.post("/api/sync", json={}).json()
    assert [s["name"] for s in body["sources"]] == ["ag"]


@pytest.fixture
def recorder(monkeypatch):
    calls = []

    def fake_sync_all(conn, specs, pricing, on_result=None):
        calls.append([s.name for s in specs])
        return []

    monkeypatch.setattr(sources_mod, "sync_all", fake_sync_all)
    return calls


@pytest.mark.parametrize("kw, status", CSRF_CASES)
def test_sync_csrf_guard(sync_env, recorder, kw, status):
    r = make_client(sync_env["db"], sync_env["pricing"], sources=sync_env["sources"]).post("/api/sync", **kw)
    assert r.status_code == status, r.text
    assert recorder == []


def test_sync_rejects_foreign_host_and_get(sync_env, recorder):
    c = make_client(sync_env["db"], sync_env["pricing"], sources=sync_env["sources"])
    assert c.post("/api/sync", json={}, headers={"Host": "evil.example.com"}).status_code in (400, 403)
    assert c.get("/api/sync").status_code == 405
    assert recorder == []
    assert c.post("/api/sync", json={}, headers={"Origin": BASE, "Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert recorder == [["claude-main", "ag", "codex-off"]]


def test_sync_lock_rejects_a_concurrent_run(sync_env, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_sync_all(conn, specs, pricing, on_result=None):
        calls.append(len(specs))
        entered.set()
        assert release.wait(10)
        return []

    monkeypatch.setattr(sources_mod, "sync_all", slow_sync_all)
    app = make_app(sync_env["db"], sync_env["pricing"], sources=sync_env["sources"])
    first = {}

    def run():
        first["r"] = TestClient(app, base_url=BASE).post("/api/sync", json={})

    t = threading.Thread(target=run)
    t.start()
    try:
        assert entered.wait(10), "first sync never started"
        second = TestClient(app, base_url=BASE).post("/api/sync", json={})
        assert second.status_code == 409
        assert second.json()["detail"] == "A sync is already running - try again in a moment."
        # reads are not blocked by a running sync
        assert TestClient(app, base_url=BASE).get("/api/overview").status_code == 200
    finally:
        release.set()
        t.join(10)
    assert first["r"].status_code == 200 and first["r"].json()["status"] == "ok"
    assert calls == [3]
    # the lock is free again
    assert TestClient(app, base_url=BASE).post("/api/sync", json={}).status_code == 200
    assert calls == [3, 3]


def test_sync_lock_released_after_a_crash(sync_env, monkeypatch):
    def boom(conn, specs, pricing, on_result=None):
        raise RuntimeError("simulated crash")

    app = make_app(sync_env["db"], sync_env["pricing"], sources=sync_env["sources"])
    monkeypatch.setattr(sources_mod, "sync_all", boom)
    assert TestClient(app, base_url=BASE, raise_server_exceptions=False).post("/api/sync", json={}).status_code == 500
    monkeypatch.undo()
    r = TestClient(app, base_url=BASE).post("/api/sync", json={})
    assert r.status_code == 200 and r.json()["status"] == "ok"


@pytest.fixture
def fake_uvicorn(monkeypatch):
    apps = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: apps.append(app))
    return apps


def test_serve_wires_sources_and_subscriptions(fake_uvicorn, sync_env, tmp_path):
    subs = _subs_file(tmp_path, {"claude-code": 20})
    r = CliRunner().invoke(cli.app, [
        "serve", "--db", str(sync_env["db"]), "--pricing", str(sync_env["pricing"]),
        "--sources", str(sync_env["sources"]), "--subscriptions", str(subs)])
    assert r.exit_code == 0, r.output
    c = TestClient(fake_uvicorn[0], base_url=BASE)
    assert c.post("/api/sync", json={}).json()["status"] == "ok"
    assert {t["tool"]: t["monthly_usd"] for t in c.get("/api/subscriptions").json()["tools"]}["claude-code"] == 20.0
    help_text = CliRunner().invoke(cli.app, ["serve", "--help"]).output
    assert "--sources" in help_text and "--subscriptions" in help_text


def test_default_subscriptions_path_is_in_the_workspace():
    assert DEFAULT_SUBSCRIPTIONS_PATH == PROJECT_ROOT / "subscriptions.json"


# ================================================================== personal records

def test_records_empty_ledger(db, pricing_path):
    put(db, pricing_path, [])
    with connect(db) as conn:
        assert analytics.records(conn, "lifelong", TODAY) == analytics.Records()


def test_records_peak_day_ties_go_to_earliest_and_cost_peak_is_separate(db, pricing_path):
    put(db, pricing_path, [
        ev("2026-11-01T10:00:00", model="claude-sonnet-4-5", inp=100, session="s1"),  # $0.0003
        ev("2026-11-03T10:00:00", model="claude-opus-4", inp=100, session="s2"),      # $0.0015
        ev("2026-11-05T10:00:00", model="claude-haiku-4-5", inp=50, session="s3"),
    ])
    with connect(db) as conn:
        rec = analytics.records(conn, "lifelong", TODAY)
    assert rec.peak_day.date == "2026-11-01" and rec.peak_day.total_tokens == 100 and rec.peak_day.events == 1
    assert rec.peak_day.cost_usd == pytest.approx(0.0003)
    assert rec.peak_cost_day.date == "2026-11-03" and rec.peak_cost_day.cost_usd == pytest.approx(0.0015)


def test_records_unpriced_only(db, pricing_path):
    put(db, pricing_path, [ev("2026-11-01T10:00:00", model="mystery-model-x", inp=100, out=5)])
    with connect(db) as conn:
        rec = analytics.records(conn, "lifelong", TODAY)
    assert rec.peak_day.total_tokens == 105 and rec.peak_day.cost_usd is None
    assert rec.peak_cost_day is None
    assert rec.heaviest_session.cost_usd is None and rec.heaviest_session.total_tokens == 105


def test_records_heaviest_session_rules(db, pricing_path):
    put(db, pricing_path, [
        # crosses midnight: dated by its first event, tokens summed
        ev("2026-11-10T23:59:00", session="late", inp=60),
        ev("2026-11-11T00:30:00", session="late", inp=60),
        # tie with "late" but starts later
        ev("2026-11-12T09:00:00", session="tie", inp=120),
        # the same session id on another tool is a different session
        ev("2026-11-13T09:00:00", tool="antigravity", model="gemini-4-neon", session="late", inp=100),
        # no session id: never a record, however big
        ev("2026-11-14T09:00:00", session="", inp=10_000),
    ])
    with connect(db) as conn:
        rec = analytics.records(conn, "lifelong", TODAY)
        ag = analytics.records(conn, "lifelong", TODAY, tool="antigravity")
    s = rec.heaviest_session
    assert (s.tool, s.date, s.total_tokens) == ("claude-code", "2026-11-10", 120)
    assert s.cost_usd == pytest.approx(120 * 3 / 1e6)
    assert ag.heaviest_session.tool == "antigravity" and ag.heaviest_session.total_tokens == 100
    assert rec.peak_day.date == "2026-11-14"  # day records still count session-less events


def test_records_without_session_ids(db, pricing_path):
    put(db, pricing_path, [ev("2026-11-14T09:00:00", session="", inp=10)])
    with connect(db) as conn:
        rec = analytics.records(conn, "lifelong", TODAY)
    assert rec.heaviest_session is None and rec.peak_day is not None and rec.longest_streak.days == 1


def test_records_longest_streak(db, pricing_path):
    days = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-10", "2026-11-25", "2026-11-26"]
    put(db, pricing_path, [ev(f"{d}T12:00:00", inp=1, session=f"s-{d}") for d in days])
    with connect(db) as conn:
        rec = analytics.records(conn, "lifelong", TODAY)
        recent = analytics.records(conn, "7d", TODAY)
        single = analytics.records(conn, "today", TODAY)
    assert (rec.longest_streak.days, rec.longest_streak.start, rec.longest_streak.end) == (3, "2026-10-01", "2026-10-03")
    assert (recent.longest_streak.days, recent.longest_streak.start, recent.longest_streak.end) == (2, "2026-11-25", "2026-11-26")
    assert (single.longest_streak.days, single.longest_streak.start, single.longest_streak.end) == (1, "2026-11-26", "2026-11-26")


def test_records_follow_tool_model_and_range(db, pricing_path):
    put(db, pricing_path, [
        ev("2026-11-20T10:00:00", model="claude-sonnet-4-5", inp=500, session="a"),
        ev("2026-11-21T10:00:00", model="claude-haiku-4-5", inp=900, session="b"),
        ev("2026-11-22T10:00:00", tool="antigravity", model="gemini-4-neon", inp=700, session="c"),
        ev("2026-03-01T10:00:00", model="claude-sonnet-4-5", inp=5_000, session="d"),
    ])
    c = make_client(db, pricing_path)
    rec = lambda **p: c.get("/api/overview", params=p).json()["records"]  # noqa: E731
    assert rec(range="lifelong")["peak_day"]["date"] == "2026-03-01"
    assert rec(range="30d")["peak_day"]["date"] == "2026-11-21"
    assert rec(range="30d", tool="antigravity")["peak_day"]["date"] == "2026-11-22"
    assert rec(range="30d", model="claude-sonnet-4-5")["peak_day"]["date"] == "2026-11-20"
    assert rec(range="lifelong", model="claude-sonnet-4-5")["heaviest_session"]["total_tokens"] == 5_000
    assert rec(range="30d", tool="codex") == {"peak_day": None, "peak_cost_day": None,
                                              "heaviest_session": None, "longest_streak": None}
    assert rec(range="30d")["longest_streak"] == {"days": 3, "start": "2026-11-20", "end": "2026-11-22"}


def test_heaviest_session_exposes_no_ids_paths_or_projects(db, pricing_path):
    put(db, pricing_path, [ev("2026-11-20T10:00:00", inp=500, session="sess-secret-123",
                              project="top-secret-project")])
    body = make_client(db, pricing_path).get("/api/overview").json()
    hs = body["records"]["heaviest_session"]
    assert set(hs) == {"tool", "date", "total_tokens", "cost_usd"}
    text = json.dumps(body["records"])
    assert "sess-secret-123" not in text and "top-secret-project" not in text and "/" not in text


# ================================================================== grouped model variants

@pytest.mark.parametrize("model, family", [
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
    ("claude-sonnet-4-5", "claude-sonnet-4-5"),
    ("anthropic/claude-opus-4-5@20251101", "claude-opus-4-5"),
    ("gemini-4-neon-low", "gemini-4-neon"),
    ("gemini-4-neon-xhigh", "gemini-4-neon"),
    ("gemini-4-neon-tiered-high", "gemini-4-neon"),
    ("gpt-6-sol-2026-03-01", "gpt-6-sol"),
    ("gemini-4-neon-high-20260101", "gemini-4-neon"),
    ("gemini-3.8-flash", "gemini-3.8-flash"),
    ("high", "high"),
])
def test_model_family(model, family):
    assert model_family(model) == family


@pytest.fixture
def variant_ledger(db, pricing_path):
    put(db, pricing_path, [
        ev("2026-11-20T10:00:00", model="claude-sonnet-4-5-20250929", inp=100),
        ev("2026-11-20T11:00:00", model="claude-sonnet-4-5", inp=50, cr=10),
        ev("2026-11-21T10:00:00", tool="antigravity", model="gemini-4-neon-low", inp=40),
        ev("2026-11-21T11:00:00", tool="antigravity", model="gemini-4-neon-high", inp=40),
        ev("2026-11-21T12:00:00", tool="antigravity", model="gemini-4-neon", inp=5),
        ev("2026-11-22T10:00:00", model="claude-opus-4-5", inp=60),
        ev("2026-11-22T11:00:00", tool="codex", model="gpt-6-sol-2026-03-01", inp=10),
    ])
    return db


def test_breakdown_default_is_unchanged(variant_ledger, pricing_path):
    c = make_client(variant_ledger, pricing_path)
    default = c.get("/api/breakdown", params={"by": "model"}).json()
    explicit = c.get("/api/breakdown", params={"by": "model", "group_variants": "false"}).json()
    assert default == explicit
    assert default["group_variants"] is False
    assert [r["key"] for r in default["rows"]][:2] == ["claude-sonnet-4-5-20250929", "claude-opus-4-5"]
    assert len(default["rows"]) == 7
    for r in default["rows"]:
        assert "variants" not in r and "variant_count" not in r and "price_key" in r and "base_model" in r
    neon_low = next(r for r in default["rows"] if r["key"] == "gemini-4-neon-low")
    assert neon_low["base_model"] == "gemini-4-neon"


def test_breakdown_group_variants_merges_families(variant_ledger, pricing_path):
    c = make_client(variant_ledger, pricing_path)
    plain = c.get("/api/breakdown", params={"by": "model"}).json()
    body = c.get("/api/breakdown", params={"by": "model", "group_variants": "true"}).json()
    assert body["group_variants"] is True
    rows = {r["key"]: r for r in body["rows"]}
    assert [r["key"] for r in body["rows"]] == ["claude-sonnet-4-5", "gemini-4-neon", "claude-opus-4-5", "gpt-6-sol"]
    assert rows["claude-sonnet-4-5"]["variants"] == ["claude-sonnet-4-5", "claude-sonnet-4-5-20250929"]
    assert rows["gemini-4-neon"]["variants"] == ["gemini-4-neon", "gemini-4-neon-high", "gemini-4-neon-low"]
    assert rows["gemini-4-neon"]["variant_count"] == 3 and rows["claude-opus-4-5"]["variant_count"] == 1
    assert rows["claude-sonnet-4-5"]["total_tokens"] == 160 and rows["claude-sonnet-4-5"]["events"] == 2
    assert rows["claude-sonnet-4-5"]["cache_read_tokens"] == 10
    assert rows["gemini-4-neon"]["base_model"] is None and rows["gemini-4-neon"]["price_key"] == "gemini-4-neon"
    # nothing lost: totals, shares and cost add up to the ungrouped view
    for f in ("total_tokens", "input_tokens", "events"):
        assert sum(r[f] for r in body["rows"]) == sum(r[f] for r in plain["rows"])
    assert sum(r["cost_usd"] for r in body["rows"]) == pytest.approx(sum(r["cost_usd"] for r in plain["rows"]))
    assert sum(r["token_share"] for r in body["rows"]) == pytest.approx(1.0)
    assert body["total"] == plain["total"]


def test_breakdown_group_variants_limits_after_merging(variant_ledger, pricing_path):
    c = make_client(variant_ledger, pricing_path)
    plain = c.get("/api/breakdown", params={"by": "model", "limit": 2}).json()
    grouped = c.get("/api/breakdown", params={"by": "model", "limit": 2, "group_variants": "true"}).json()
    assert [r["key"] for r in plain["rows"]] == ["claude-sonnet-4-5-20250929", "claude-opus-4-5"]
    assert [r["key"] for r in grouped["rows"]] == ["claude-sonnet-4-5", "gemini-4-neon"]  # 85 > 60 once merged


def test_breakdown_group_variants_respects_scope_and_dimension(variant_ledger, pricing_path):
    c = make_client(variant_ledger, pricing_path)
    ag = c.get("/api/breakdown", params={"by": "model", "group_variants": "true", "tool": "antigravity"}).json()
    assert [r["key"] for r in ag["rows"]] == ["gemini-4-neon"] and ag["rows"][0]["total_tokens"] == 85
    for by in ("tool", "project"):
        body = c.get("/api/breakdown", params={"by": by, "group_variants": "true"}).json()
        assert body["group_variants"] is False
        assert body == c.get("/api/breakdown", params={"by": by}).json()
    assert c.get("/api/breakdown", params={"by": "model", "group_variants": "maybe"}).status_code == 422


# ================================================================== front-end wiring

def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def test_index_has_new_controls():
    html = (STATIC_DIR / "index.html").read_text()
    for needle in ['id="sync-now"', 'id="kpi-saved"', 'id="kpi-saved-sub"', 'id="kpi-roi-card" hidden',
                   'id="kpi-roi"', 'id="kpi-roi-spent"', 'id="kpi-roi-unit">spent<', 'Plan Spent &amp; ROI', 'id="rec-peak"', 'id="rec-session"', 'id="rec-streak"',
                   'id="group-variants" role="switch"', 'id="pricing-tabs"', 'data-tab="subs"',
                   'id="subs-form"', 'id="subs-list"', 'id="subs-save"', 'id="foot-keys"',
                   'data-range="custom"', '>Custom</button>', 'id="custom-range"', 'id="custom-start"', 'id="custom-end"',
                   'id="custom-apply"', 'id="kpi-cost-delta"', 'id="kpi-tokens-delta"']:
        assert needle in html, needle
    keys = re.search(r'<span class="keys" id="foot-keys">(.*?)</span>', html).group(1)
    assert _text(keys) == "Keys: 1-8 range · P pricing · S sync · Esc clear"
    assert "Personal Records" in html and "Cache Saved" in html and "Sync Now" in html
    assert not re.search(r"[一-鿿]", html)  # English-only UI


def test_app_js_wiring():
    js = (STATIC_DIR / "app.js").read_text()
    for needle in ['group_variants: "true"', 'p.set("group", "0")', 'group: true,', 'p.get("group") !== "0"', '"/api/sync"', '"/api/subscriptions"',
                   'document.addEventListener("keydown", onShortcut)', '+ "×"', "} value`"]:
        assert needle in js, needle
    assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", js)
    assert not re.search(r"[一-鿿]", js)


def test_csp_still_strict(db, pricing_path):
    r = make_client(db, pricing_path).get("/")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp or "default-src 'self'" in csp
    assert "http" not in csp.replace("'self'", "")


# ================================================================== repo files

def test_subscriptions_example_is_valid_and_ignored():
    example = PROJECT_ROOT / "subscriptions.example.json"
    plans = load_subscriptions(example)
    assert list(plans) == ["claude-code", "antigravity", "codex", "opencode"]
    assert [(p.start.isoformat(), p.end and p.end.isoformat(), p.monthly_usd) for p in plans["claude-code"]] == [
        ("2026-03-15", "2026-07-31", 20.0), ("2026-08-01", None, 100.0)]
    assert plans["opencode"][0].legacy and plans["opencode"][0].monthly_usd == 0.0
    ignored = (PROJECT_ROOT / ".gitignore").read_text().splitlines()
    assert "subscriptions.json" in ignored and "sources.json" in ignored


# ================================================================== JavaScriptCore runtime

def fmt_usd(x: float) -> str:
    if not x:
        return "$0.00"
    if abs(x) >= 1:
        return f"${x:,.2f}"
    return f"${x:.3f}" if abs(x) >= 0.01 else f"${x:.4f}"


def fmt_compact(n: float) -> str:
    a = abs(n)
    if a >= 1e9:
        return f"{n / 1e9:.2f}B"
    if a >= 1e6:
        return f"{n / 1e6:.2f}M"
    if a >= 1e3:
        return f"{n / 1e3:.1f}K"
    return str(round(n))


def fmt_multiple(m: float) -> str:
    return (f"{m:.0f}" if m >= 100 else f"{m:.1f}" if m >= 10 else f"{m:.2f}") + "×"


def fmt_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}, {d.year}"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def sync_toast(body: dict) -> str:
    parts = [f"{t['label']} +{t['inserted']:,}" + (f" / {t['updated']:,} updated" if t["updated"] else "")
             for t in body["by_tool"] if t["inserted"] or t["updated"]]
    failed = [s["name"] for s in body["sources"] if s["status"] == "failed"]
    tot = body["totals"]
    return (f"{'Synced with errors' if failed else 'Synced'} · {tot['inserted']:,} new · {tot['updated']:,} updated"
            + (f" · {', '.join(parts)}" if parts else " · nothing new")
            + (f" · failed: {', '.join(failed)}" if failed else ""))


def roi_title(sub: dict) -> str:
    lines = [f"{t['label']}: {fmt_usd(t['monthly_usd']) + '/mo' if t['monthly_usd'] > 0 else 'no active plan'}"
             f" · {plural(t['paid_days'], 'paid day')} · {fmt_usd(t['plan_cost_usd'])} plan · {fmt_usd(t['value_usd'])} value"
             for t in sub["tools"]]
    if sub["unpriced_events"]:
        lines.append(f"{sub['unpriced_events']:,} unpriced requests are not counted")
    return "\n".join(lines)


# range x tool combinations the smoke test switches through for the Plan Spent card
PLAN_SCOPES = [("30d", None), ("ytd", None), ("lifelong", None),
               ("lifelong", "claude-code"), ("ytd", "claude-code"), ("lifelong", "antigravity")]


def plan_card(sub: dict | None) -> dict:
    """Expected Plan Spent & ROI card texts (mirrors renderRoi in app.js)."""
    if sub is None:
        return {"hidden": True}
    days = plural(sub["days"], "day")
    if sub["multiple"] is None:
        return {"hidden": False, "spent": fmt_usd(sub["plan_cost_usd"]), "roi": "–",
                "sub": f"No paid plan in this range · {days}"}
    net = sub["value_usd"] - sub["plan_cost_usd"]
    net_text = f"Saved {fmt_usd(net)}" if net >= 0 else f"Net loss {fmt_usd(-net)}"
    return {"hidden": False, "spent": fmt_usd(sub["plan_cost_usd"]),
            "roi": f"{fmt_multiple(sub['multiple'])} value",
            "sub": f"{fmt_usd(sub['value_usd'])} API value · {net_text} · {days}"}


def subs_toast(body: dict) -> str:
    active = [t for t in body["tools"] if t["monthly_usd"] > 0]
    if active:
        return "Subscriptions saved · " + " · ".join(f"{t['label']} {fmt_usd(t['monthly_usd'])}/mo" for t in active)
    if any(p["monthly_usd"] > 0 for t in body["tools"] for p in t["periods"]):
        return "Subscriptions saved · no plan active today"
    return "Subscriptions saved · no paid plans, ROI hidden"


# What features_smoke.js edits the Subscriptions tab into, in order (JSON.stringify of each must match).
SMOKE_SUBS_POSTS = [
    {"subscriptions": {
        "claude-code": [{"start": "2026-01-01", "end": "2026-06-30", "monthly_usd": 20},
                        {"start": "2026-07-01", "end": "2026-10-31", "monthly_usd": 100},
                        {"start": "2026-11-01", "end": None, "monthly_usd": 200}],
        "antigravity": [{"start": TODAY.isoformat(), "end": None, "monthly_usd": 20}],
        "codex": [{"start": "2026-08-01", "end": "2026-11-10", "monthly_usd": 20}]}},
    {"subscriptions": {"codex": [{"start": "2026-08-01", "end": "2026-11-10", "monthly_usd": 20}]}},
    {"subscriptions": {}},
]


@pytest.fixture
def recorded(tmp_path, sync_env) -> Path:
    """Real API responses from a fixture ledger that was filled through POST /api/sync."""
    subs = _subs_file(tmp_path, {"subscriptions": {
        "claude-code": [{"start": "2026-01-01", "end": "2026-06-30", "monthly_usd": 20.0},
                        {"start": "2026-07-01", "end": None, "monthly_usd": 100.0}],
        "codex": 20.0}})  # a legacy undated plan next to dated ones
    c = make_client(sync_env["db"], sync_env["pricing"], subs=subs, sources=sync_env["sources"])
    sync = c.post("/api/sync", json={}).json()
    assert sync["status"] == "ok" and sync["totals"]["inserted"] > 0
    q = {"range": "lifelong"}
    R = {
        "sync": sync,
        "tools": c.get("/api/tools").json(),
        "overview": c.get("/api/overview", params=q).json(),
        "timeseries": c.get("/api/timeseries", params=q).json(),
        "breakdown": c.get("/api/breakdown", params={**q, "by": "model"}).json(),
        "breakdown_grouped": c.get("/api/breakdown", params={**q, "by": "model", "group_variants": "true"}).json(),
        "hourly": c.get("/api/hourly", params=q).json(),
        "heatmap": c.get("/api/heatmap").json(),
        "subscriptions": c.get("/api/subscriptions").json(),
        "pricing": c.get("/api/pricing").json(),
        "sync_not_configured": make_client(sync_env["db"], sync_env["pricing"]).post("/api/sync", json={}).json(),
    }
    # Plan Spent & ROI per range x tool, from the real server (before the plans change below)
    R["overview_scoped"] = {
        f"{rng}|{tool or ''}": c.get("/api/overview", params={"range": rng, **({"tool": tool} if tool else {})}).json()
        for rng, tool in PLAN_SCOPES}
    entries = json.loads(sync_env["sources"].read_text())["sources"]
    write_sources(sync_env["sources"], entries + [
        {"name": "missing", "tool": "opencode", "path": str(tmp_path / "missing.db")}])
    R["sync_partial"] = c.post("/api/sync", json={}).json()
    assert R["sync_partial"]["status"] == "partial"
    # the exact bodies features_smoke.js must send, with the real server's replies
    R["subs_posts"] = {}
    for body in SMOKE_SUBS_POSTS:
        text = json.dumps(body, separators=(",", ":"))
        reply = c.post("/api/subscriptions", content=text, headers={"Content-Type": "application/json"})
        assert reply.status_code == 200, reply.text
        R["subs_posts"][text] = reply.json()
        if body is SMOKE_SUBS_POSTS[1]:  # only an ended Codex plan: paid plans exist, none in the last 7 days
            R["overview_no_plan_days"] = c.get("/api/overview", params={"range": "7d"}).json()
    nd = R["overview_no_plan_days"]["subscription"]
    assert nd["multiple"] is None and nd["plan_cost_usd"] == 0 and nd["tools"][0]["paid_days"] == 0

    ov = R["overview"]
    cs, sub, rec = ov["cache_savings"], ov["subscription"], ov["records"]
    assert cs["usd"] > 0 and sub is not None and rec["heaviest_session"] is not None
    labels = {t["tool"]: t["label"] for t in R["subscriptions"]["tools"]}
    st = rec["longest_streak"]
    with connect(sync_env["db"]) as conn:
        forbidden = sorted({v for row in conn.execute("SELECT DISTINCT session_id, project_name FROM events")
                            for v in row if v})
    grouped_rows = R["breakdown_grouped"]["rows"][:15]
    X = {
        "saved": fmt_usd(cs["usd"]),
        "saved_sub": (f"{fmt_compact(cs['cache_read_tokens'])} cache-read tokens"
                      + (f" · {fmt_compact(cs['unpriced_cache_read_tokens'])} unpriced"
                         if cs["unpriced_cache_read_tokens"] else "")),
        "roi": f"{fmt_multiple(sub['multiple'])} value",
        "roi_spent": fmt_usd(sub["plan_cost_usd"]),
        "roi_sub": plan_card(sub)["sub"],
        "plan_scopes": [[rng, tool or ""] for rng, tool in PLAN_SCOPES],
        "plan_cards": {k: plan_card(o["subscription"]) for k, o in R["overview_scoped"].items()},
        "roi_title": roi_title(sub),
        "roi_title_no_plan_days": roi_title(R["overview_no_plan_days"]["subscription"]),
        "roi_days": plural(sub["days"], "day"),
        "peak": f"{fmt_compact(rec['peak_day']['total_tokens'])} tokens",
        "peak_sub": fmt_day(rec["peak_day"]["date"]) + " · ",
        "session": f"{fmt_compact(rec['heaviest_session']['total_tokens'])} tokens",
        "session_sub": f"{labels[rec['heaviest_session']['tool']]} · {fmt_day(rec['heaviest_session']['date'])} · ",
        "streak": plural(st["days"], "day"),
        "streak_sub": fmt_day(st["start"]) if st["start"] == st["end"] else f"{fmt_day(st['start'])} – {fmt_day(st['end'])}",
        "forbidden": forbidden,
        "multi_variant_families": sum(r["variant_count"] > 1 for r in grouped_rows),
        "sync_toast": sync_toast(sync),
        "sync_partial_toast": sync_toast(R["sync_partial"]),
        "subs_posts": list(R["subs_posts"]),
        "subs_toasts": [subs_toast(R["subs_posts"][k]) for k in R["subs_posts"]],
    }
    assert X["multi_variant_families"] >= 1 and X["forbidden"]
    cards = X["plan_cards"]
    assert cards["lifelong|antigravity"] == {"hidden": True}          # no plan for that tool
    assert cards["30d|"]["spent"] != cards["lifelong|"]["spent"]        # follows the range
    assert cards["lifelong|claude-code"]["spent"] != cards["lifelong|"]["spent"]  # follows the tool
    assert all(c["hidden"] or c["spent"].startswith("$") for c in cards.values())
    tool_ids = {t["tool"] for t in R["tools"]["tools"]} if isinstance(R["tools"], dict) else set()
    assert {"claude-code", "antigravity"} <= tool_ids, tool_ids
    assert X["sync_partial_toast"].endswith(" · nothing new · failed: missing")
    out = tmp_path / "recorded.json"
    out.write_text(json.dumps({"responses": R, "expect": X}))
    return out


def _run_jsc(scenario: Path, *args: Path) -> list[str]:
    out = subprocess.run([str(JSC), str(JS / "stub_dom.js"), str(scenario), "--",
                          str(STATIC_DIR / "app.js"), *map(str, args)],
                         capture_output=True, text=True, timeout=60)
    lines = out.stdout.strip().splitlines()
    assert lines and lines[-1] == "SMOKE_OK", out.stdout + out.stderr
    assert not [ln for ln in lines if ln.startswith("FAIL")]
    return lines


@pytest.mark.skipif(not JSC.exists(), reason="macOS JavaScriptCore not available")
def test_app_js_syntax_jsc():
    script = 'try { new Function(readFile(arguments[0])); print("SYNTAX_OK"); } catch (e) { print("SYNTAX_ERROR " + e); }'
    for f in ("app.js",):
        out = subprocess.run([str(JSC), "-e", script, "--", str(STATIC_DIR / f)], capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == "SYNTAX_OK", out.stdout + out.stderr
    for f in ("stub_dom.js", "pricing_smoke.js", "features_smoke.js", "range_smoke.js"):
        out = subprocess.run([str(JSC), "-e", script, "--", str(JS / f)], capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == "SYNTAX_OK", f + out.stdout + out.stderr


@pytest.mark.skipif(not JSC.exists(), reason="macOS JavaScriptCore not available")
def test_features_runtime_jsc(recorded):
    """The real app.js against recorded API responses: KPIs, records, group switch, keys, Sync Now, plans."""
    lines = _run_jsc(JS / "features_smoke.js", recorded)
    passed = [ln for ln in lines if ln.startswith("PASS")]
    assert len(passed) >= 125, len(passed)
    for name in ("period 1 prefilled", "overlapping periods", "impossible calendar date", "an undated plan cannot sit",
                 "a new period starts the day after", "save payload", "no plan days in range", "tooltip for an ended plan",
                 "read-only: no add, no save", "Plan Spent shows the full amount", "sub has API value and net saved",
                 "Plan card for 30d|", "Plan card for lifelong|claude-code", "Plan card hidden for lifelong|antigravity",
                 "back to All Tools", "nothing spent in a range without plan days", "no spent figure when the file is broken"):
        assert any(ln.startswith("PASS " + name) for ln in passed), name
