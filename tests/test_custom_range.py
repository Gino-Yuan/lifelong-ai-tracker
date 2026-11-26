"""Custom date ranges (`range=custom&start=&end=`) and period-over-period comparison.

Fabricated ledger only (events built in code under pytest's workspace-local tmp dir);
every app gets explicit tmp paths, so no real data/, sources.json or subscriptions.json
is read.

Ledger (TZ pinned to UTC by conftest; TODAY = 2026-11-26). Prices: claude-haiku-4-5 is
$1 / 1M input tokens, so cost_usd == input tokens / 1e6 exactly.

    2024-06-01  haiku       4,000,000   last_year's previous year (2024)
    2025-03-10  haiku       5,000,000   prior YTD / last_year (2025)
    2025-12-01  haiku       1,000,000   last_year only (after Nov 26)
    2026-09-05  haiku      10,000,000   prev of custom Oct; prev of custom Sep 10-19
    2026-09-25  sonnet      1,000,000   other model ($3) - only unscoped totals see it
    2026-09-15  haiku      10,000,000   custom Sep 10-19 -> flat vs Aug 31 - Sep 9
    2026-10-10  haiku      23,680,000   custom Oct: +18.4% vs Aug 31 - Sep 30 (haiku only)
    2026-10-12  antigravity 1,000,000   another tool - only unscoped totals see it
    2026-11-01  haiku      18,000,000   } 30d = 20,838,400 = 23,680,000 x 0.88 -> -12.0%
    2026-11-21  haiku       2,838,400   } and 7d is "New" (nothing in Nov 13-19)
"""

from __future__ import annotations

import itertools
import json
import math
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURES, TEST_PRICING, JSC, requires_jsc
from lifelong_tracker import analytics
from lifelong_tracker.analytics import CustomRange, previous_period
from lifelong_tracker.card import build_card_data
from lifelong_tracker.models import UsageEvent
from lifelong_tracker.pricing import PricingTable
from lifelong_tracker.storage import connect
from lifelong_tracker.storage.db import transaction, upsert_events
from lifelong_tracker.subscriptions import DAYS_PER_MONTH
from lifelong_tracker.web.app import STATIC_DIR, create_app

TODAY = date(2026, 11, 26)
BASE = "http://127.0.0.1:8765"
JS = FIXTURES / "js"
HAIKU, SONNET = "claude-haiku-4-5", "claude-sonnet-4-5"
OCT = {"range": "custom", "start": "2026-10-01", "end": "2026-10-31"}
_ids = itertools.count(1)


def ev(day: str, inp: int, *, tool="claude-code", model=HAIKU, session="sess-r") -> UsageEvent:
    return UsageEvent(
        timestamp=datetime.fromisoformat(f"{day}T12:00:00").replace(tzinfo=timezone.utc),
        tool=tool, model=model, project_name="proj-r", session_id=session,
        message_id=f"msg-range-{next(_ids)}", input_tokens=inp,
    )


LEDGER = [
    ev("2024-06-01", 4_000_000),
    ev("2025-03-10", 5_000_000),
    ev("2025-12-01", 1_000_000),
    ev("2026-09-05", 10_000_000),
    ev("2026-09-25", 1_000_000, model=SONNET),
    ev("2026-09-15", 10_000_000),
    ev("2026-10-10", 23_680_000, session="sess-oct"),
    ev("2026-10-12", 1_000_000, tool="antigravity", session="sess-ag"),
    ev("2026-11-01", 18_000_000),
    ev("2026-11-21", 2_838_400),
]


@pytest.fixture
def pricing_path(tmp_path) -> Path:
    p = tmp_path / "prices" / "pricing.json"
    p.parent.mkdir()
    shutil.copy(TEST_PRICING, p)
    return p


@pytest.fixture
def db(tmp_path, pricing_path) -> Path:
    path = tmp_path / "ledger" / "tracker.db"
    with connect(path) as conn:
        with transaction(conn):
            upsert_events(conn, LEDGER, PricingTable.load(pricing_path))
    return path


def client(db, pricing_path, *, today=TODAY, subs=None) -> TestClient:
    app = create_app(db, pricing_path, today=lambda: today, subscriptions_path=subs)
    return TestClient(app, base_url=BASE)


@pytest.fixture
def c(db, pricing_path) -> TestClient:
    return client(db, pricing_path)


def ro(db: Path):
    import sqlite3
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


# ================================================================== CustomRange

@pytest.mark.parametrize("start, end, days", [
    ("2026-10-01", "2026-10-31", 31),
    ("2026-10-01", "2026-10-01", 1),
    ("2024-02-29", "2024-03-01", 2),     # a real leap day
    ("2025-12-15", "2026-01-14", 31),
])
def test_custom_range_parses_real_dates(start, end, days):
    r = CustomRange.parse(start, end)
    assert (r.start.isoformat(), r.end.isoformat(), r.days) == (start, end, days)
    assert analytics.range_bounds(r, TODAY) == (r.start, r.end)
    assert analytics.range_start(r, TODAY) == r.start
    assert analytics.range_name_of(r) == "custom"
    assert analytics.range_slug(r) == f"custom_{start}_{end}"


@pytest.mark.parametrize("start, end, needle", [
    (None, "2026-10-31", "needs both start and end"),
    ("2026-10-01", None, "needs both start and end"),
    ("", "", "needs both start and end"),
    ("2026-1-01", "2026-10-31", "YYYY-MM-DD"),
    ("20261001", "2026-10-31", "YYYY-MM-DD"),
    ("2026/10/01", "2026-10-31", "YYYY-MM-DD"),
    ("2026-10-01T00", "2026-10-31", "YYYY-MM-DD"),
    ("+026-10-01", "2026-10-31", "YYYY-MM-DD"),
    ("2026-02-30", "2026-03-01", "not a real calendar date"),
    ("2025-02-29", "2025-03-01", "not a real calendar date"),   # 2025 is not a leap year
    ("2026-13-01", "2026-12-31", "not a real calendar date"),
    ("2026-10-01", "2026-04-31", "not a real calendar date"),
    ("2026-10-31", "2026-10-01", "on or after start"),
    ("1969-12-31", "1970-01-05", "1970-9999"),
    ("1970-01-01", "2200-01-01", "at most"),
])
def test_custom_range_rejects_bad_input(start, end, needle):
    with pytest.raises(ValueError, match=needle):
        CustomRange.parse(start, end)


@pytest.mark.parametrize("start, end, label", [
    ("2026-10-01", "2026-10-31", "Oct 1 – Oct 31, 2026"),
    ("2026-10-01", "2026-10-01", "Oct 1, 2026"),
    ("2025-12-15", "2026-01-14", "Dec 15, 2025 – Jan 14, 2026"),
])
def test_custom_range_label(start, end, label):
    assert analytics.range_label(CustomRange.parse(start, end)) == label


# ================================================================== previous period

def _pp(rng, today=TODAY):
    p = previous_period(rng, today)
    return None if p is None else (p.start.isoformat(), p.end.isoformat(), p.label)


@pytest.mark.parametrize("rng, expected", [
    ("today", ("2026-11-25", "2026-11-25", "yesterday")),
    ("7d", ("2026-11-13", "2026-11-19", "prev 7d")),        # range Nov 20-26
    ("30d", ("2026-09-28", "2026-10-27", "prev 30d")),      # range Oct 28 - Nov 26
    ("90d", ("2026-05-31", "2026-08-28", "prev 90d")),      # range Aug 29 - Nov 26
    ("ytd", ("2025-01-01", "2025-11-26", "prior YTD")),
    ("year", ("2025-01-01", "2025-11-26", "prior YTD")),    # CLI alias
    ("last_year", ("2024-01-01", "2024-12-31", "2024")),
    ("lifelong", None),
    ("all", None),
])
def test_previous_period_presets(rng, expected):
    assert _pp(rng) == expected


def test_previous_period_is_adjacent_and_same_length():
    for rng in ("today", "7d", "30d", "90d"):
        start, end = analytics.range_bounds(rng, TODAY)
        p = previous_period(rng, TODAY)
        assert p.end == start - timedelta(days=1)
        assert (p.end - p.start).days == (end - start).days


@pytest.mark.parametrize("start, end, expected", [
    ("2026-10-01", "2026-10-31", ("2026-08-31", "2026-09-30", "prev 31d")),
    ("2026-09-10", "2026-09-19", ("2026-08-31", "2026-09-09", "prev 10d")),
    ("2026-10-01", "2026-10-01", ("2026-09-30", "2026-09-30", "prev day")),
    ("2024-03-01", "2024-03-31", ("2024-01-30", "2024-02-29", "prev 31d")),  # spans the leap day
    ("2026-01-01", "2026-01-07", ("2025-12-25", "2025-12-31", "prev 7d")),   # across New Year
])
def test_previous_period_custom(start, end, expected):
    assert _pp(CustomRange.parse(start, end)) == expected


@pytest.mark.parametrize("today, expected", [
    (date(2028, 2, 29), ("2027-01-01", "2027-02-28", "prior YTD")),   # leap day -> Feb 28
    (date(2025, 2, 28), ("2024-01-01", "2024-02-28", "prior YTD")),   # Feb 29 2024 not included
    (date(2025, 3, 1), ("2024-01-01", "2024-03-01", "prior YTD")),    # same month/day, leap day inside
    (date(2026, 1, 1), ("2025-01-01", "2025-01-01", "prior YTD")),
    (date(2026, 12, 31), ("2025-01-01", "2025-12-31", "prior YTD")),
])
def test_previous_period_ytd_leap_years(today, expected):
    assert _pp("ytd", today) == expected


def test_previous_period_seven_days_over_a_leap_day():
    assert _pp("7d", date(2024, 3, 6)) == ("2024-02-22", "2024-02-28", "prev 7d")  # range Feb 29 - Mar 6


def test_previous_period_unknown_range():
    with pytest.raises(ValueError):
        previous_period("fortnight", TODAY)


@pytest.mark.parametrize("cur, prev, expected", [
    (118.4, 100, 18.4), (88, 100, -12.0), (100, 100, 0.0), (0, 10, -100.0),
    (11.84, 10, 18.4), (3, 1, 200.0),
    (5, 0, None), (0, 0, None), (0.0, 0.0, None),
])
def test_pct_change(cur, prev, expected):
    got = analytics.pct_change(cur, prev)
    assert got is None if expected is None else got == pytest.approx(expected, abs=1e-9)


# ================================================================== comparison on the ledger

def test_comparison_custom_follows_tool_and_model(db):
    oct_ = CustomRange.parse("2026-10-01", "2026-10-31")
    with ro(db) as conn:
        scoped = analytics.comparison(conn, oct_, TODAY, tool="claude-code", model=HAIKU)
        unscoped = analytics.comparison(conn, oct_, TODAY)
        by_tool = analytics.comparison(conn, oct_, TODAY, tool="claude-code")
        ag = analytics.comparison(conn, oct_, TODAY, tool="antigravity")
    # haiku / claude-code only: 23.68M vs 20M = +18.4% (cost and tokens alike)
    assert (scoped.prev_start, scoped.prev_end) == (date(2026, 8, 31), date(2026, 9, 30))
    assert scoped.prev_total_tokens == 20_000_000 and scoped.prev_cost_usd == pytest.approx(20.0)
    assert scoped.tokens_pct_change == pytest.approx(18.4) and scoped.cost_pct_change == pytest.approx(18.4)
    # everything: 24.68M tokens vs 21M; $24.68 vs $23 (the sonnet day costs $3)
    assert unscoped.prev_total_tokens == 21_000_000 and unscoped.prev_cost_usd == pytest.approx(23.0)
    assert unscoped.tokens_pct_change == pytest.approx((24.68 - 21) / 21 * 100)
    assert unscoped.cost_pct_change == pytest.approx((24.68 - 23) / 23 * 100)
    # claude-code, any model: 23.68M vs 21M
    assert by_tool.tokens_pct_change == pytest.approx((23.68 - 21) / 21 * 100)
    # antigravity has nothing before October: no base, no percentage
    assert ag.prev_total_tokens == 0 and ag.tokens_pct_change is None and ag.cost_pct_change is None


@pytest.mark.parametrize("rng, prev_tokens, tok_pct", [
    ("today", 0, None),                      # 0 -> 0
    ("7d", 0, None),                         # 0 -> 2,838,400 ("New" in the UI)
    ("30d", 23_680_000, -12.0),
    ("ytd", 5_000_000, None),                # computed below
    ("last_year", 4_000_000, (6_000_000 - 4_000_000) / 4_000_000 * 100),
    ("lifelong", None, None),
])
def test_comparison_presets(db, rng, prev_tokens, tok_pct):
    with ro(db) as conn:
        cmp = analytics.comparison(conn, rng, TODAY, tool="claude-code", model=HAIKU)
        cur = analytics.summarize(conn, rng, TODAY, tool="claude-code", model=HAIKU)
    if prev_tokens is None:
        assert cmp is None
        return
    assert cmp.prev_total_tokens == prev_tokens
    if rng == "ytd":
        tok_pct = (cur.total_tokens - 5_000_000) / 5_000_000 * 100
    if tok_pct is None:
        assert cmp.tokens_pct_change is None and cmp.cost_pct_change is None
    else:
        assert cmp.tokens_pct_change == pytest.approx(tok_pct)
        assert cmp.cost_pct_change == pytest.approx(tok_pct)  # haiku: cost == tokens / 1e6


def test_comparison_flat(db):
    with ro(db) as conn:
        cmp = analytics.comparison(conn, CustomRange.parse("2026-09-10", "2026-09-19"), TODAY, model=HAIKU)
    assert cmp.prev_total_tokens == 10_000_000 and cmp.tokens_pct_change == 0.0 and cmp.cost_pct_change == 0.0


def test_comparison_ytd_on_a_leap_day(db, pricing_path):
    with connect(db) as conn:
        with transaction(conn):
            upsert_events(conn, [ev("2027-02-28", 1_000_000), ev("2027-03-01", 7_000_000),
                                 ev("2028-02-29", 3_000_000)], PricingTable.load(pricing_path))
    body = client(db, pricing_path, today=date(2028, 2, 29)).get("/api/overview", params={"range": "ytd"}).json()
    cmp = body["comparison"]
    assert (cmp["prev_start"], cmp["prev_end"], cmp["prev_label"]) == ("2027-01-01", "2027-02-28", "prior YTD")
    assert cmp["prev_total_tokens"] == 1_000_000            # Mar 1 2027 is outside
    assert cmp["tokens_pct_change"] == pytest.approx(200.0)  # 3M vs 1M


# ================================================================== API: custom range

ENDPOINTS = ["/api/overview", "/api/timeseries", "/api/breakdown", "/api/hourly", "/api/export", "/api/card"]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_every_range_endpoint_accepts_custom(c, path):
    r = c.get(path, params=OCT)
    assert r.status_code == 200, r.text
    if path in ("/api/export", "/api/card"):
        disp = r.headers["content-disposition"]
        assert "custom_2026-10-01_2026-10-31" in disp and "/" not in disp.split("filename=")[1]
        return
    body = r.json()
    assert body["range"] == "custom" and body["range_label"] == "Oct 1 – Oct 31, 2026"
    assert (body["range_start"], body["range_end"]) == ("2026-10-01", "2026-10-31")


BAD = [
    ({"range": "custom"}, "needs both"),
    ({"range": "custom", "start": "2026-10-01"}, "needs both"),
    ({"range": "custom", "end": "2026-10-31"}, "needs both"),
    ({"range": "custom", "start": "", "end": ""}, "needs both"),
    ({"range": "custom", "start": "2026-02-30", "end": "2026-03-31"}, "not a real calendar date"),
    ({"range": "custom", "start": "2026-10-01", "end": "2026-13-01"}, "not a real calendar date"),
    ({"range": "custom", "start": "Oct 1", "end": "2026-10-31"}, "YYYY-MM-DD"),
    ({"range": "custom", "start": "2026-10-31", "end": "2026-10-01"}, "on or after start"),
    ({"range": "custom", "start": "1900-01-01", "end": "1900-01-02"}, "1970-9999"),
    ({"range": "30d", "start": "2026-10-01"}, "only accepted with range=custom"),
    ({"range": "lifelong", "start": "2026-10-01", "end": "2026-10-31"}, "only accepted with range=custom"),
    ({"start": "2026-10-01", "end": "2026-10-31"}, "only accepted with range=custom"),  # default range
]


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("params, needle", BAD)
def test_every_range_endpoint_rejects_bad_custom(c, path, params, needle):
    r = c.get(path, params=params)
    assert r.status_code == 422, (path, params, r.text)
    assert needle in json.dumps(r.json())


@pytest.mark.parametrize("path", ENDPOINTS)
def test_overlong_or_unknown_range_is_422(c, path):
    assert c.get(path, params={"range": "custom", "start": "2026-10-01" * 2, "end": "2026-10-31"}).status_code == 422
    assert c.get(path, params={"range": "fortnight"}).status_code == 422


def test_custom_overview_totals_records_and_ranges(c, db):
    body = c.get("/api/overview", params=OCT).json()
    assert (body["start"], body["end"]) == ("2026-10-01", "2026-10-31")
    with ro(db) as conn:
        want = analytics.summarize_span(conn, date(2026, 10, 1), date(2026, 10, 31))
    assert body["totals"]["total_tokens"] == want.total_tokens == 24_680_000
    assert body["totals"]["cost_usd"] == pytest.approx(24.68)
    rec = body["records"]
    assert rec["peak_day"]["date"] == "2026-10-10" and rec["peak_day"]["total_tokens"] == 23_680_000
    assert rec["heaviest_session"]["date"] == "2026-10-10"
    assert rec["longest_streak"]["days"] == 1
    # comparison block
    cmp = body["comparison"]
    assert set(cmp) == {"prev_label", "prev_start", "prev_end", "prev_cost_usd", "prev_total_tokens",
                        "cost_pct_change", "tokens_pct_change"}
    assert (cmp["prev_label"], cmp["prev_start"], cmp["prev_end"]) == ("prev 31d", "2026-08-31", "2026-09-30")


def test_overview_comparison_precision_and_scope(c):
    scoped = c.get("/api/overview", params={**OCT, "tool": "claude-code", "model": HAIKU}).json()["comparison"]
    assert scoped["prev_cost_usd"] == pytest.approx(20.0) and scoped["prev_total_tokens"] == 20_000_000
    assert round(scoped["cost_pct_change"], 1) == 18.4 and round(scoped["tokens_pct_change"], 1) == 18.4
    assert scoped["cost_pct_change"] == pytest.approx(18.4, abs=1e-9)
    down = c.get("/api/overview", params={"range": "30d", "tool": "claude-code", "model": HAIKU}).json()["comparison"]
    assert down["prev_label"] == "prev 30d" and down["tokens_pct_change"] == pytest.approx(-12.0, abs=1e-9)
    assert down["cost_pct_change"] == pytest.approx(-12.0, abs=1e-9)


def test_overview_comparison_zero_base_and_lifelong(c):
    new = c.get("/api/overview", params={"range": "7d"}).json()
    assert new["totals"]["total_tokens"] > 0
    assert new["comparison"]["prev_total_tokens"] == 0 and new["comparison"]["prev_cost_usd"] == 0
    assert new["comparison"]["tokens_pct_change"] is None and new["comparison"]["cost_pct_change"] is None
    empty = c.get("/api/overview", params={"range": "today"}).json()
    assert empty["comparison"]["prev_label"] == "yesterday" and empty["comparison"]["tokens_pct_change"] is None
    assert c.get("/api/overview", params={"range": "lifelong"}).json()["comparison"] is None
    assert c.get("/api/overview").json()["comparison"] is None  # default range is lifelong


@pytest.mark.parametrize("rng, label", [("today", "yesterday"), ("7d", "prev 7d"), ("30d", "prev 30d"),
                                        ("90d", "prev 90d"), ("ytd", "prior YTD"), ("last_year", "2024")])
def test_overview_comparison_labels(c, rng, label):
    assert c.get("/api/overview", params={"range": rng}).json()["comparison"]["prev_label"] == label


def test_custom_timeseries_spans_exactly_the_window(c):
    body = c.get("/api/timeseries", params={**OCT, "granularity": "day"}).json()
    days = [d["date"] for d in body["days"]]
    assert days[0] == "2026-10-01" and days[-1] == "2026-10-31" and len(days) == 31
    assert sum(d["total_tokens"] for d in body["days"]) == 24_680_000
    split = c.get("/api/timeseries", params={**OCT, "split_by": "tool"}).json()
    assert {s["tool"] for s in split["series"]} == {"claude-code", "antigravity"}
    future = c.get("/api/timeseries", params={"range": "custom", "start": "2026-11-20", "end": "2026-12-05"}).json()
    assert len(future["days"]) == 16 and future["days"][-1]["date"] == "2026-12-05"


def test_custom_breakdown_hourly_and_export(c):
    bd = c.get("/api/breakdown", params={**OCT, "by": "tool"}).json()
    assert {r["key"]: r["total_tokens"] for r in bd["rows"]} == {"claude-code": 23_680_000, "antigravity": 1_000_000}
    hr = c.get("/api/hourly", params=OCT).json()
    assert hr["hours"][12]["tokens"] == 24_680_000 and sum(h["events"] for h in hr["hours"]) == 2
    csv = c.get("/api/export", params={**OCT, "format": "csv"})
    lines = csv.text.strip().splitlines()
    assert len(lines) == 3 and all(",2026-10-1" in ln for ln in lines[1:])
    assert 'filename="lifelong-ai-tracker_custom_2026-10-01_2026-10-31_2026-11-26.csv"' in csv.headers["content-disposition"]
    js = c.get("/api/export", params={**OCT, "format": "json", "tool": "claude-code"})
    meta = js.json()["meta"]
    assert meta["range"] == "custom" and meta["range_label"] == "Oct 1 – Oct 31, 2026" and meta["count"] == 1
    assert "custom_2026-10-01_2026-10-31_claude-code_2026-11-26.json" in js.headers["content-disposition"]
    # the export never carries subscription dates or fees
    assert "monthly_usd" not in js.text and "subscription" not in js.text


def test_custom_card(c, db):
    r = c.get("/api/card", params={**OCT, "download": "1"})
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["content-disposition"] == (
        'attachment; filename="lifelong-ai-tracker_card_custom_2026-10-01_2026-10-31_2026-11-26.svg"')
    svg = r.text
    assert '<title id="card-title">Oct 1 – Oct 31, 2026</title>' in svg
    assert "Custom range · 31 days" in svg
    assert "proj-r" not in svg and "sess-" not in svg and "/mo" not in svg
    with ro(db) as conn:
        data = build_card_data(conn, CustomRange.parse("2025-12-15", "2026-01-14"), TODAY)
    assert data.range_name == "custom" and data.title == "Dec 15, 2025 – Jan 14, 2026"
    assert data.period == "Custom range · 31 days"


def test_custom_roi(db, pricing_path, tmp_path):
    subs = tmp_path / "subs" / "subscriptions.json"
    subs.parent.mkdir()
    subs.write_text(json.dumps({"subscriptions": {
        "claude-code": [{"start": "2026-10-15", "end": None, "monthly_usd": 30.0}]}}))
    c = client(db, pricing_path, subs=subs)
    sub = c.get("/api/overview", params=OCT).json()["subscription"]
    assert (sub["start"], sub["end"], sub["days"]) == ("2026-10-01", "2026-10-31", 31)
    assert sub["tools"][0]["paid_days"] == 17                     # Oct 15-31
    assert sub["plan_cost_usd"] == pytest.approx(17 / DAYS_PER_MONTH * 30.0)
    assert sub["value_usd"] == pytest.approx(0.0)                 # the Oct 10 usage predates the plan
    before = c.get("/api/overview", params={"range": "custom", "start": "2026-09-01", "end": "2026-09-30"}).json()
    assert before["subscription"]["multiple"] is None and before["subscription"]["plan_cost_usd"] == 0


def test_today_is_unchanged_for_presets(c):
    """Presets keep their old shape: no range_start / range_end keys, same labels."""
    body = c.get("/api/timeseries", params={"range": "30d"}).json()
    assert body["range_label"] == "Last 30 days" and "range_start" not in body


# ================================================================== JavaScriptCore runtime

def fmt_usd(x: float) -> str:
    if not x:
        return "$0.00"
    if abs(x) >= 1:
        return f"${x:,.2f}"
    return f"${x:.3f}" if abs(x) >= 0.01 else f"${x:.4f}"


def fmt_span(a: str, b: str) -> str:
    return analytics.fmt_span(date.fromisoformat(a), date.fromisoformat(b))


def badge(cmp: dict | None, pct_key: str, current: float, prev_text: str | None) -> dict:
    """Expected delta badge (mirrors renderDelta in app.js)."""
    if cmp is None:
        return {"hidden": True}
    pct = cmp[pct_key]
    title = f"{cmp['prev_label']} ({fmt_span(cmp['prev_start'], cmp['prev_end'])}): {prev_text}"
    if pct is not None:
        mag = math.floor(abs(pct) * 10 + 0.5) / 10    # JS Math.round, one decimal
        arrow = "→" if mag == 0 else "↑" if pct > 0 else "↓"
        return {"hidden": False, "new": False, "text": f"{arrow} {mag:,.1f}% vs {cmp['prev_label']}", "title": title}
    if current > 0:
        return {"hidden": False, "new": True, "text": f"New vs {cmp['prev_label']}", "title": title}
    return {"hidden": True}


def badges(ov: dict) -> dict:
    cmp, t = ov["comparison"], ov["totals"]
    return {
        "cost": badge(cmp, "cost_pct_change", t["cost_usd"], cmp and fmt_usd(cmp["prev_cost_usd"])),
        "tokens": badge(cmp, "tokens_pct_change", t["total_tokens"], cmp and f"{cmp['prev_total_tokens']:,} tokens"),
    }


# Every scope range_smoke.js visits: (range, start, end, tool)
SMOKE_SCOPES = [
    ("custom", "2026-10-01", "2026-10-31", ""),
    ("30d", "", "", ""), ("7d", "", "", ""), ("today", "", "", ""), ("lifelong", "", "", ""),
    ("custom", "2026-10-28", "2026-11-26", ""),     # Custom's default: the last 30 days
    ("custom", "2026-09-10", "2026-09-19", ""),     # flat
    ("ytd", "", "", ""), ("last_year", "", "", ""),
    ("custom", "2026-10-01", "2026-10-31", "claude-code"),
]


def scope_key(rng, start, end, tool) -> str:
    return f"{rng}|{start}|{end}|{tool}"


@pytest.fixture
def recorded_ranges(tmp_path, db, pricing_path) -> Path:
    c = client(db, pricing_path)
    R = {"tools": c.get("/api/tools").json(), "heatmap": c.get("/api/heatmap").json(),
         "pricing": c.get("/api/pricing").json(), "overviews": {}, "breakdowns": {}}
    for rng, start, end, tool in SMOKE_SCOPES:
        q = {"range": rng, **({"start": start, "end": end} if rng == "custom" else {}),
             **({"tool": tool} if tool else {})}
        k = scope_key(rng, start, end, tool)
        R["overviews"][k] = c.get("/api/overview", params=q).json()
        R["breakdowns"][k] = c.get("/api/breakdown", params={**q, "by": "model", "group_variants": "true"}).json()
    R["timeseries"] = c.get("/api/timeseries", params=OCT).json()
    R["hourly"] = c.get("/api/hourly", params=OCT).json()
    X = {"badges": {k: badges(o) for k, o in R["overviews"].items()},
         "labels": {k: o["range_label"] for k, o in R["overviews"].items()},
         "today": TODAY.isoformat()}
    b = X["badges"]
    # the fixture covers every badge state
    assert b["custom|2026-10-01|2026-10-31|claude-code"]["tokens"]["text"] == "↑ 12.8% vs prev 31d"
    assert b["30d|||"]["cost"]["text"].startswith("↓ ")
    assert b["7d|||"]["tokens"] == {"hidden": False, "new": True, "text": "New vs prev 7d",
                                    "title": "prev 7d (Nov 13 – Nov 19, 2026): 0 tokens"}
    assert b["today|||"]["cost"] == {"hidden": True} and b["lifelong|||"]["tokens"] == {"hidden": True}
    assert b["custom|2026-09-10|2026-09-19|"]["tokens"]["text"] == "→ 0.0% vs prev 10d"
    out = tmp_path / "ranges.json"
    out.write_text(json.dumps({"responses": R, "expect": X}))
    return out


def _run_jsc(*args: str) -> list[str]:
    out = subprocess.run([str(JSC), str(JS / "stub_dom.js"), str(JS / "range_smoke.js"), "--",
                          str(STATIC_DIR / "app.js"), *args], capture_output=True, text=True, timeout=60)
    lines = out.stdout.strip().splitlines()
    assert lines and lines[-1] == "SMOKE_OK", out.stdout + out.stderr
    assert not [ln for ln in lines if ln.startswith("FAIL")]
    return [ln for ln in lines if ln.startswith("PASS")]


@requires_jsc
def test_custom_range_and_deltas_runtime_jsc(recorded_ranges):
    passed = _run_jsc(str(recorded_ranges), "main")
    assert len(passed) >= 60, len(passed)
    for name in ("boot from a custom hash", "Custom button pressed", "dates shown", "cost badge for custom|2026-10-01",
                 "tokens badge for 7d", "New badge", "badges hidden for lifelong", "badges hidden for today",
                 "Custom remembers the window", "Apply reloads", "Enter reloads", "end before start",
                 "impossible date", "key 8", "custom follows the tool", "export links carry the dates",
                 "preset drops start/end", "flat badge"):
        assert any(ln.startswith("PASS " + name) for ln in passed), name


@requires_jsc
def test_custom_range_defaults_jsc(recorded_ranges):
    passed = _run_jsc(str(recorded_ranges), "fresh")
    assert any(ln.startswith("PASS Custom prefills the last 30 days ending on the server's today") for ln in passed)
    assert any(ln.startswith("PASS cost badge for custom|2026-10-28|2026-11-26|") for ln in passed)


@requires_jsc
@pytest.mark.parametrize("mode", ["badhash", "reversedhash"])
def test_custom_range_bad_hash_falls_back_jsc(recorded_ranges, mode):
    passed = _run_jsc(str(recorded_ranges), mode)
    assert any(ln.startswith("PASS falls back to the last 30 days") for ln in passed)
