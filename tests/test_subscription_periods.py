"""Dated subscription periods: format, validation, exact overlap-day ROI, API, privacy.

All data is fabricated: events built in code, configs written to pytest's workspace-local
tmp dir, and every app gets explicit tmp paths. No real subscriptions.json, sources.json,
data/ or home directory is read.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lifelong_tracker import analytics, cli
from lifelong_tracker import fsutil as fsutil_mod
from lifelong_tracker.storage import connect
from lifelong_tracker.subscriptions import (
    DAYS_PER_MONTH,
    MAX_PERIODS,
    Period,
    SubscriptionsError,
    fee_on,
    load_subscriptions,
    save_subscriptions,
    subscription_roi,
    validate_file,
    validate_plans,
)
from lifelong_tracker.web.app import STATIC_DIR
from test_enhancements import (  # shared fabricated-data helpers + fixtures
    BASE,
    CSRF_CASES,
    TODAY,
    db,  # noqa: F401  (pytest fixture)
    ev,
    make_client,
    pricing_path,  # noqa: F401  (pytest fixture)
    put,
)

D = date.fromisoformat

# The example from the spec: an upgrade mid-way and a cancelled plan.
SPEC = {
    "claude-code": [
        {"start": "2026-03-15", "end": "2026-07-31", "monthly_usd": 20.0},
        {"start": "2026-08-01", "end": None, "monthly_usd": 100.0},
    ],
    "codex": [{"start": "2026-08-01", "end": "2026-11-10", "monthly_usd": 20.0}],
}
SONNET_PER_M = 3.0  # claude-sonnet-4-5 input USD / 1M in tests/fixtures/pricing_test.json


def usd(inp: int) -> float:
    return inp / 1_000_000 * SONNET_PER_M


# (tool, day, input tokens): distinct amounts so any wrongly counted day shows in the sum
EVENTS = [
    ("claude-code", "2026-03-01", 100_000),   # before the first plan starts: no value
    ("claude-code", "2026-04-01", 200_000),
    ("claude-code", "2026-07-31", 400_000),   # last day of the $20 plan
    ("claude-code", "2026-08-01", 800_000),   # first day of the $100 plan
    ("claude-code", "2026-11-20", 1_600_000),
    ("claude-code", "2026-11-26", 3_200_000),
    ("codex", "2026-08-01", 110_000),
    ("codex", "2026-11-10", 220_000),         # last covered codex day
    ("codex", "2026-11-11", 440_000),         # after cancelling: no value
    ("codex", "2026-11-25", 880_000),
    ("antigravity", "2025-12-15", 1_000_000),  # no plan at all
]


@pytest.fixture
def ledger(db, pricing_path):  # noqa: F811
    put(db, pricing_path, [
        ev(f"{day}T12:00:00", tool=tool, model="claude-sonnet-4-5", inp=inp, session=f"s-{tool}-{day}")
        for tool, day, inp in EVENTS
    ])
    return db


def oracle(plans: dict, start: date, end: date, tools=None) -> tuple[float, float, dict[str, int]]:
    """Brute force, day by day: (plan cost, value, paid days per tool)."""
    parsed = validate_plans(plans)
    cost = value = 0.0
    paid_days: dict[str, int] = {}
    d = start
    while d <= min(end, TODAY):
        for tool, periods in parsed.items():
            if tools is not None and tool not in tools:
                continue
            fee = sum(p.monthly_usd for p in periods if p.monthly_usd > 0 and p.covers(d))
            if fee > 0:
                cost += fee / DAYS_PER_MONTH
                paid_days[tool] = paid_days.get(tool, 0) + 1
                value += sum(usd(i) for t, day, i in EVENTS if t == tool and day == d.isoformat())
        d += timedelta(days=1)
    return cost, value, paid_days


def subs_file(tmp_path: Path, data=None) -> Path:
    p = tmp_path / "cfg" / "subscriptions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return p


# ================================================================== format

def test_current_format_loads_and_omitted_end_is_ongoing(tmp_path):
    p = subs_file(tmp_path, {"_comment": "x", "subscriptions": {
        "claude-code": [{"start": "2026-08-01", "monthly_usd": 100},
                        {"start": "2026-03-15", "end": "2026-07-31", "monthly_usd": 20}]}})
    plans = load_subscriptions(p)
    assert plans == {"claude-code": [Period(D("2026-03-15"), D("2026-07-31"), 20.0),
                                     Period(D("2026-08-01"), None, 100.0)]}  # sorted by start


def test_legacy_flat_file_and_legacy_number_inside_subscriptions(tmp_path):
    flat = load_subscriptions(subs_file(tmp_path, {"_c": "old", "claude-code": 20, "opencode": 0}))
    assert flat == {"claude-code": [Period(None, None, 20.0)], "opencode": [Period(None, None, 0.0)]}
    assert flat["claude-code"][0].legacy and flat["claude-code"][0].covers(date(1999, 1, 1))


def test_mixed_shapes_per_tool():
    plans = validate_file({"subscriptions": {"antigravity": 20.5, "claude-code": SPEC["claude-code"]}})
    assert plans["antigravity"] == [Period(None, None, 20.5)]
    assert [p.monthly_usd for p in plans["claude-code"]] == [20.0, 100.0]


def test_unexpected_top_level_keys_next_to_subscriptions_are_rejected():
    with pytest.raises(SubscriptionsError, match="unexpected top-level"):
        validate_file({"subscriptions": {}, "claude-code": 20})
    assert validate_file({"subscriptions": {}, "_comment": "fine"}) == {}


def test_save_writes_current_format_keeps_comments_and_round_trips(tmp_path):
    p = subs_file(tmp_path, {"_comment": "keep me", "codex": 1})
    stored = save_subscriptions(p, {"codex": SPEC["codex"], "claude-code": list(reversed(SPEC["claude-code"])),
                                    "opencode": 0})
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw == {"_comment": "keep me", "subscriptions": {
        "claude-code": SPEC["claude-code"], "opencode": 0.0, "codex": SPEC["codex"]}}
    assert load_subscriptions(p) == stored
    assert sorted(x.name for x in p.parent.iterdir()) == ["subscriptions.json"]  # no temp files left


def test_failed_atomic_replace_leaves_the_file_and_no_temp(tmp_path, monkeypatch):
    p = subs_file(tmp_path, {"subscriptions": {"codex": 7}})
    before = p.read_text(encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fsutil_mod.os, "replace", boom)
    with pytest.raises(OSError):
        save_subscriptions(p, SPEC)
    assert p.read_text(encoding="utf-8") == before
    assert sorted(x.name for x in p.parent.iterdir()) == ["subscriptions.json"]


def test_fee_on():
    plans = validate_plans(SPEC)
    assert fee_on(plans["claude-code"], D("2026-03-14")) == 0.0
    assert fee_on(plans["claude-code"], D("2026-03-15")) == 20.0
    assert fee_on(plans["claude-code"], D("2026-07-31")) == 20.0
    assert fee_on(plans["claude-code"], D("2026-08-01")) == 100.0
    assert fee_on(plans["claude-code"], D("2030-01-01")) == 100.0  # ongoing
    assert fee_on(plans["codex"], D("2026-11-10")) == 20.0 and fee_on(plans["codex"], D("2026-11-11")) == 0.0


# ================================================================== validation

OK_PERIOD = {"start": "2026-03-15", "end": "2026-07-31", "monthly_usd": 20}


@pytest.mark.parametrize("periods, needle", [
    ([{**OK_PERIOD, "start": "2026-3-15"}], "YYYY-MM-DD"),
    ([{**OK_PERIOD, "start": "2026/03/15"}], "YYYY-MM-DD"),
    ([{**OK_PERIOD, "start": "2026-02-30"}], "real calendar date"),
    ([{**OK_PERIOD, "start": "2026-13-01"}], "real calendar date"),
    ([{**OK_PERIOD, "start": 20260315}], "YYYY-MM-DD"),
    ([{**OK_PERIOD, "start": None}], "YYYY-MM-DD"),
    ([{"end": "2026-07-31", "monthly_usd": 20}], "needs a start"),
    ([{**OK_PERIOD, "end": "2026-02-29"}], "real calendar date"),  # 2026 is not a leap year
    ([{**OK_PERIOD, "end": "2026-03-14"}], "before start"),
    ([{**OK_PERIOD, "end": ""}], "YYYY-MM-DD"),
    ([{**OK_PERIOD, "monthly_usd": -0.01}], "between 0 and"),
    ([{**OK_PERIOD, "monthly_usd": 10_000.01}], "between 0 and"),
    ([{**OK_PERIOD, "monthly_usd": float("nan")}], "between 0 and"),
    ([{**OK_PERIOD, "monthly_usd": float("inf")}], "between 0 and"),
    ([{**OK_PERIOD, "monthly_usd": "20"}], "must be a number"),
    ([{**OK_PERIOD, "monthly_usd": True}], "must be a number"),
    ([{"start": "2026-03-15"}], "needs monthly_usd"),
    ([{**OK_PERIOD, "note": "x"}], "unknown period field"),
    (["2026-03-15"], "must be an object"),
    ([OK_PERIOD, {"start": "2026-07-31", "end": None, "monthly_usd": 100}], "overlap"),  # same day
    ([OK_PERIOD, {"start": "2026-06-01", "end": "2026-06-30", "monthly_usd": 5}], "overlap"),  # nested
    ([{"start": "2026-01-01", "end": None, "monthly_usd": 5}, {"start": "2026-09-01", "monthly_usd": 5}], "ongoing"),
    ([dict(OK_PERIOD)] * 2, "overlap"),
    ([{"start": f"2026-01-{i + 1:02d}", "end": f"2026-01-{i + 1:02d}", "monthly_usd": 1} for i in range(31)]
     + [{"start": f"2026-02-{i + 1:02d}", "end": f"2026-02-{i + 1:02d}", "monthly_usd": 1} for i in range(18)],
     f"at most {MAX_PERIODS}"),
    ("monthly", "must be a number, or a list"),
    ({"start": "2026-01-01"}, "must be a number, or a list"),
])
def test_invalid_periods_are_rejected(periods, needle):
    with pytest.raises(SubscriptionsError, match=needle):
        validate_plans({"claude-code": periods})


def test_valid_edge_periods():
    plans = validate_plans({
        "claude-code": [OK_PERIOD, {"start": "2026-08-01", "end": None, "monthly_usd": 0}],  # adjacent, free
        "codex": [{"start": "2028-02-29", "end": "2028-02-29", "monthly_usd": 10_000}],      # 1 day, leap day
        "t3": [{"start": f"2026-01-{i + 1:02d}", "end": f"2026-01-{i + 1:02d}", "monthly_usd": 1}
               for i in range(MAX_PERIODS - 17)] + [
              {"start": f"2026-02-{i + 1:02d}", "end": f"2026-02-{i + 1:02d}", "monthly_usd": 1} for i in range(17)],
    })
    assert len(plans["t3"]) == MAX_PERIODS
    assert plans["codex"] == [Period(D("2028-02-29"), D("2028-02-29"), 10_000.0)]


# ================================================================== ROI: exact overlap days

@pytest.mark.parametrize("range_name", ["today", "7d", "30d", "90d", "ytd", "last_year", "lifelong"])
def test_roi_matches_a_day_by_day_oracle(ledger, range_name):
    with connect(ledger) as conn:
        roi = subscription_roi(conn, SPEC, range_name, TODAY)
    start, end = analytics.range_bounds(range_name, TODAY)
    if start is None:
        start = D("2026-03-01")  # first active day of a paid tool, before the earliest plan start
    cost, value, paid_days = oracle(SPEC, start, end)
    assert (roi.start, roi.end, roi.days) == (start, end, (end - start).days + 1)
    assert roi.plan_cost_usd == pytest.approx(cost, rel=1e-12, abs=1e-12)
    assert roi.value_usd == pytest.approx(value, rel=1e-12, abs=1e-12)
    assert [t.tool for t in roi.tools] == ["claude-code", "codex"]
    assert {t.tool: t.paid_days for t in roi.tools if t.paid_days} == paid_days
    if cost:
        assert roi.multiple == pytest.approx(value / cost, rel=1e-12)
    else:
        assert roi.multiple is None


def test_roi_30d_by_hand(ledger):
    """Oct 28 - Nov 26: Claude $100 every day; Codex Oct 28 - Nov 10 = 14 days at $20."""
    with connect(ledger) as conn:
        roi = subscription_roi(conn, SPEC, "30d", TODAY)
    cc, cx = roi.tools
    assert (cc.paid_days, cx.paid_days) == (30, 14)
    assert cc.plan_cost_usd == pytest.approx(30 / 30.436875 * 100.0, rel=1e-12)
    assert cx.plan_cost_usd == pytest.approx(14 / 30.436875 * 20.0, rel=1e-12)
    assert cc.value_usd == pytest.approx(usd(1_600_000) + usd(3_200_000), rel=1e-12)
    assert cx.value_usd == pytest.approx(usd(220_000), rel=1e-12)  # Nov 11 / Nov 25 are after the plan ended
    assert (cc.monthly_usd, cx.monthly_usd, roi.monthly_usd) == (100.0, 0.0, 100.0)  # in force on Nov 26


def test_roi_lifelong_by_hand(ledger):
    with connect(ledger) as conn:
        roi = subscription_roi(conn, SPEC, "lifelong", TODAY)
    cc, cx = roi.tools
    assert roi.start == D("2026-03-01") and roi.days == 271
    assert cc.paid_days == 139 + 118 and cx.paid_days == 102
    assert roi.plan_cost_usd == pytest.approx((139 * 20 + 118 * 100 + 102 * 20) / DAYS_PER_MONTH, rel=1e-12)
    assert cc.value_usd == pytest.approx(usd(200_000 + 400_000 + 800_000 + 1_600_000 + 3_200_000), rel=1e-12)
    assert cx.value_usd == pytest.approx(usd(110_000 + 220_000), rel=1e-12)


def test_roi_lifelong_starts_at_the_earliest_plan_start_when_it_precedes_activity(ledger):
    plans = {"t3": [{"start": "2026-01-10", "end": None, "monthly_usd": DAYS_PER_MONTH}]}  # $1 / day, no t3 usage
    with connect(ledger) as conn:
        roi = subscription_roi(conn, {**SPEC, **plans}, "lifelong", TODAY)
        t3 = subscription_roi(conn, plans, "lifelong", TODAY, tool="t3")
    assert roi.start == D("2026-01-10")
    assert t3.start == D("2026-01-10") and t3.days == 321 and t3.plan_cost_usd == pytest.approx(321.0)
    assert t3.value_usd == 0.0 and t3.multiple == 0.0


def test_roi_follows_the_tool_filter(ledger):
    with connect(ledger) as conn:
        cx = subscription_roi(conn, SPEC, "30d", TODAY, tool="codex")
        ag = subscription_roi(conn, SPEC, "30d", TODAY, tool="antigravity")
    cost, value, _ = oracle(SPEC, D("2026-10-28"), TODAY, tools={"codex"})
    assert [t.tool for t in cx.tools] == ["codex"]
    assert cx.plan_cost_usd == pytest.approx(cost, rel=1e-12) and cx.value_usd == pytest.approx(value, rel=1e-12)
    assert ag is None  # no plan for that tool


def test_cancelled_plan_costs_nothing_after_it_ends(ledger):
    with connect(ledger) as conn:
        roi = subscription_roi(conn, SPEC, "7d", TODAY, tool="codex")  # Nov 20 - 26, plan ended Nov 10
    assert roi is not None and roi.plan_cost_usd == 0.0 and roi.value_usd == 0.0
    assert roi.multiple is None and roi.tools[0].paid_days == 0 and roi.monthly_usd == 0.0


def test_future_starts_and_future_ends(ledger):
    plans = {"claude-code": [{"start": "2026-11-01", "end": "2027-01-31", "monthly_usd": 30},
                             {"start": "2027-02-01", "end": None, "monthly_usd": 60}]}
    with connect(ledger) as conn:
        roi = subscription_roi(conn, plans, "30d", TODAY)
        later = subscription_roi(conn, {"codex": [{"start": "2026-12-01", "monthly_usd": 20}]}, "30d", TODAY)
    assert roi.tools[0].paid_days == 26  # Nov 1 - Nov 26: never past today
    assert roi.plan_cost_usd == pytest.approx(26 / DAYS_PER_MONTH * 30, rel=1e-12)
    assert later.plan_cost_usd == 0.0 and later.multiple is None


def test_last_year_overlap(ledger):
    plans = {"antigravity": [{"start": "2025-06-01", "monthly_usd": 10}]}
    with connect(ledger) as conn:
        roi = subscription_roi(conn, plans, "last_year", TODAY)
    assert (roi.start, roi.end) == (D("2025-01-01"), D("2025-12-31"))
    assert roi.tools[0].paid_days == 214  # Jun 1 - Dec 31 2025
    assert roi.plan_cost_usd == pytest.approx(214 / DAYS_PER_MONTH * 10, rel=1e-12)
    assert roi.value_usd == pytest.approx(usd(1_000_000), rel=1e-12)


def test_single_day_and_zero_fee_periods(ledger):
    plans = {"claude-code": [{"start": "2026-11-20", "end": "2026-11-20", "monthly_usd": 50},
                             {"start": "2026-11-21", "monthly_usd": 0}]}  # free afterwards
    with connect(ledger) as conn:
        roi = subscription_roi(conn, plans, "7d", TODAY)
        free = subscription_roi(conn, {"claude-code": [{"start": "2026-01-01", "monthly_usd": 0}]}, "7d", TODAY)
    assert roi.tools[0].paid_days == 1
    assert roi.plan_cost_usd == pytest.approx(50 / DAYS_PER_MONTH, rel=1e-12)
    assert roi.value_usd == pytest.approx(usd(1_600_000), rel=1e-12)  # Nov 26 falls in the free period
    assert free is None  # only $0 plans: no ROI


def test_legacy_numbers_keep_the_old_results(ledger):
    """An undated fee covers every day: fee x range days / DAYS_PER_MONTH, value = all usage."""
    with connect(ledger) as conn:
        for rng in ("7d", "30d", "ytd", "last_year", "lifelong"):
            roi = subscription_roi(conn, {"claude-code": 20.0, "codex": 0.0}, rng, TODAY)
            tot = analytics.summarize(conn, rng, TODAY, tool="claude-code")
            assert roi.plan_cost_usd == pytest.approx(20.0 * roi.days / DAYS_PER_MONTH, rel=1e-12)
            assert roi.value_usd == pytest.approx(tot.cost_usd, rel=1e-12, abs=1e-12)
            assert roi.tools[0].paid_days == roi.days
        assert subscription_roi(conn, {"claude-code": 20.0}, "lifelong", TODAY).start == D("2026-03-01")


def test_summarize_span_is_inclusive(ledger):
    with connect(ledger) as conn:
        tot = analytics.summarize_span(conn, D("2026-07-31"), D("2026-08-01"), tool="claude-code")
    assert tot.cost_usd == pytest.approx(usd(400_000 + 800_000), rel=1e-12)


# ================================================================== API

def test_get_reports_periods_legacy_and_fee_in_force_today(db, pricing_path, tmp_path):  # noqa: F811
    p = subs_file(tmp_path, {"subscriptions": {**SPEC, "antigravity": 15}})
    body = make_client(db, pricing_path, subs=p).get("/api/subscriptions").json()
    tools = {t["tool"]: t for t in body["tools"]}
    assert body["today"] == TODAY.isoformat() and body["max_periods"] == MAX_PERIODS
    assert tools["claude-code"]["monthly_usd"] == 100.0 and tools["claude-code"]["legacy"] is False
    assert tools["claude-code"]["periods"] == [
        {"start": "2026-03-15", "end": "2026-07-31", "monthly_usd": 20.0, "ongoing": False},
        {"start": "2026-08-01", "end": None, "monthly_usd": 100.0, "ongoing": True}]
    assert tools["codex"]["monthly_usd"] == 0.0  # cancelled on Nov 10
    assert tools["antigravity"]["legacy"] is True and tools["antigravity"]["periods"] == [
        {"start": None, "end": None, "monthly_usd": 15.0, "ongoing": True}]
    assert tools["t3"]["periods"] == [] and tools["t3"]["monthly_usd"] == 0.0


def test_post_periods_writes_file_and_drives_overview(ledger, pricing_path, tmp_path):  # noqa: F811
    p = subs_file(tmp_path, {"_comment": "mine", "codex": 7})
    c = make_client(ledger, pricing_path, subs=p)
    body = {"subscriptions": {"codex": SPEC["codex"], "claude-code": [
        {"start": "2026-08-01", "monthly_usd": 100}, {"start": "2026-03-15", "end": "2026-07-31", "monthly_usd": 20}]}}
    r = c.post("/api/subscriptions", json=body)
    assert r.status_code == 200, r.text
    assert json.loads(p.read_text(encoding="utf-8")) == {"_comment": "mine", "subscriptions": SPEC}
    assert r.json()["status"] == "ok" and [x["monthly_usd"] for x in r.json()["tools"]][:1] == [100.0]

    sub = c.get("/api/overview", params={"range": "30d"}).json()["subscription"]
    cost, value, _ = oracle(SPEC, D("2026-10-28"), TODAY)
    assert sub["plan_cost_usd"] == pytest.approx(cost, rel=1e-12)
    assert sub["value_usd"] == pytest.approx(value, rel=1e-12)
    assert sub["multiple"] == pytest.approx(value / cost, rel=1e-12)
    assert (sub["start"], sub["end"], sub["days"]) == ("2026-10-28", "2026-11-26", 30)
    assert [(t["tool"], t["paid_days"]) for t in sub["tools"]] == [("claude-code", 30), ("codex", 14)]
    # the model filter is ignored, the tool filter is not
    assert c.get("/api/overview", params={"range": "30d", "model": "claude-opus-4"}).json()["subscription"] == sub
    cx = c.get("/api/overview", params={"range": "7d", "tool": "codex"}).json()["subscription"]
    assert cx["multiple"] is None and cx["plan_cost_usd"] == 0 and cx["tools"][0]["paid_days"] == 0


def test_post_legacy_fees_body_still_works(db, pricing_path, tmp_path):  # noqa: F811
    p = subs_file(tmp_path)
    r = make_client(db, pricing_path, subs=p).post("/api/subscriptions", json={"fees": {"codex": 20}})
    assert r.status_code == 200 and json.loads(p.read_text(encoding="utf-8")) == {"subscriptions": {"codex": 20.0}}
    r = make_client(db, pricing_path, subs=p).post("/api/subscriptions", json={"subscriptions": {"codex": 25}})
    assert r.status_code == 200 and json.loads(p.read_text(encoding="utf-8")) == {"subscriptions": {"codex": 25.0}}


@pytest.mark.parametrize("payload", [
    {},
    {"subscriptions": {}, "fees": {}},
    {"subscriptions": {"claude-code": [{**OK_PERIOD, "start": "2026-02-30"}]}},
    {"subscriptions": {"claude-code": [{**OK_PERIOD, "start": "15/03/2026"}]}},
    {"subscriptions": {"claude-code": [{**OK_PERIOD, "start": None}]}},
    {"subscriptions": {"claude-code": [{"end": "2026-07-31", "monthly_usd": 20}]}},
    {"subscriptions": {"claude-code": [{**OK_PERIOD, "end": "2026-03-01"}]}},
    {"subscriptions": {"claude-code": [{**OK_PERIOD, "end": "2026-02-31"}]}},
    {"subscriptions": {"claude-code": [{**OK_PERIOD, "monthly_usd": -1}]}},
    {"subscriptions": {"claude-code": [{**OK_PERIOD, "monthly_usd": 10_001}]}},
    {"subscriptions": {"claude-code": [{**OK_PERIOD, "monthly_usd": "20"}]}},
    {"subscriptions": {"claude-code": [{**OK_PERIOD, "monthly_usd": True}]}},
    {"subscriptions": {"claude-code": [{"start": "2026-03-15", "end": None}]}},
    {"subscriptions": {"claude-code": [{**OK_PERIOD, "note": "x"}]}},
    {"subscriptions": {"claude-code": [OK_PERIOD, {"start": "2026-07-31", "monthly_usd": 1}]}},
    {"subscriptions": {"claude-code": [{"start": "2026-01-01", "monthly_usd": 1}, {"start": "2026-09-01", "monthly_usd": 1}]}},
    {"subscriptions": {"claude-code": ["2026-03-15"]}},
    {"subscriptions": {"claude-code": None}},
    {"subscriptions": {"cursor": [OK_PERIOD]}},
    {"subscriptions": {"claude-code": [{"start": f"2026-01-{i % 28 + 1:02d}", "monthly_usd": 1} for i in range(MAX_PERIODS + 1)]}},
    {"subscriptions": [OK_PERIOD]},
])
def test_post_rejects_bad_periods_and_leaves_the_file(db, pricing_path, tmp_path, payload):  # noqa: F811
    p = subs_file(tmp_path, {"subscriptions": {"codex": 7}})
    before = p.read_text(encoding="utf-8")
    r = make_client(db, pricing_path, subs=p).post("/api/subscriptions", json=payload)
    assert r.status_code == 422, r.text
    assert p.read_text(encoding="utf-8") == before


def test_post_rejects_non_finite_fee_in_a_period(db, pricing_path, tmp_path):  # noqa: F811
    p = subs_file(tmp_path)
    raw = '{"subscriptions": {"codex": [{"start": "2026-01-01", "end": null, "monthly_usd": NaN}]}}'
    r = make_client(db, pricing_path, subs=p).post(
        "/api/subscriptions", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and not p.exists()


def test_post_over_4_kib_is_413(db, pricing_path, tmp_path):  # noqa: F811
    p = subs_file(tmp_path, {"subscriptions": {"codex": 7}})
    before = p.read_text(encoding="utf-8")
    periods = [{"start": f"20{y:02d}-01-01", "end": f"20{y:02d}-12-31", "monthly_usd": 12345.678901}
               for y in range(10, 99)]
    body = json.dumps({"subscriptions": {"claude-code": periods}})
    assert len(body) > 4096
    r = make_client(db, pricing_path, subs=p).post(
        "/api/subscriptions", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and p.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("kw, status", CSRF_CASES)
def test_post_periods_csrf_guard(db, pricing_path, tmp_path, kw, status):  # noqa: F811
    p = subs_file(tmp_path, {"subscriptions": {"codex": 7}})
    before = p.read_text(encoding="utf-8")
    kw = dict(kw)
    if "json" in kw:
        kw["json"] = {"subscriptions": SPEC}
    r = make_client(db, pricing_path, subs=p).post("/api/subscriptions", **kw)
    assert r.status_code == status, r.text
    assert p.read_text(encoding="utf-8") == before


def test_post_periods_rejects_foreign_host_and_allows_same_origin(db, pricing_path, tmp_path):  # noqa: F811
    p = subs_file(tmp_path, {"subscriptions": {"codex": 7}})
    c = make_client(db, pricing_path, subs=p)
    r = c.post("/api/subscriptions", json={"subscriptions": SPEC}, headers={"Host": "evil.example.com"})
    assert r.status_code in (400, 403) and json.loads(p.read_text(encoding="utf-8")) == {"subscriptions": {"codex": 7}}
    r = c.post("/api/subscriptions", json={"subscriptions": SPEC},
               headers={"Origin": BASE, "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200 and json.loads(p.read_text(encoding="utf-8")) == {"subscriptions": SPEC}


def test_write_failure_is_500_and_keeps_the_file(db, pricing_path, tmp_path, monkeypatch):  # noqa: F811
    p = subs_file(tmp_path, {"subscriptions": {"codex": 7}})
    before = p.read_text(encoding="utf-8")
    monkeypatch.setattr(fsutil_mod.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError(13, "Permission denied")))
    r = make_client(db, pricing_path, subs=p).post("/api/subscriptions", json={"subscriptions": SPEC})
    assert r.status_code == 500 and "Cannot write" in r.json()["detail"]
    assert p.read_text(encoding="utf-8") == before and sorted(x.name for x in p.parent.iterdir()) == ["subscriptions.json"]


def test_invalid_file_on_disk_is_reported(db, pricing_path, tmp_path):  # noqa: F811
    p = subs_file(tmp_path, {"subscriptions": {"codex": [{"start": "2026-05-01", "end": "2026-04-01", "monthly_usd": 5}]}})
    c = make_client(db, pricing_path, subs=p)
    assert "before start" in c.get("/api/subscriptions").json()["error"]
    ov = c.get("/api/overview").json()
    assert ov["subscription"] is None and "before start" in ov["subscription_error"]


# ================================================================== privacy: card and exports

SECRET_PLANS = {"subscriptions": {
    "claude-code": [{"start": "2019-02-03", "end": "2019-04-05", "monthly_usd": 37.77},
                    {"start": "2026-10-07", "end": None, "monthly_usd": 41.13}],
    "codex": 29.19,
}}
SECRET_NEEDLES = ["37.77", "41.13", "29.19", "2019-02-03", "2019-04-05", "2026-10-07", "Feb 3, 2019",
                  "Oct 7, 2026", "monthly_usd", "subscri", "Subscri", "plan_cost", "ROI"]


def _assert_no_secrets(text: str, where: str) -> None:
    hits = [n for n in SECRET_NEEDLES if n in text]
    assert not hits, f"{where} leaks subscription data: {hits}"


def test_card_and_exports_never_include_subscription_data(ledger, pricing_path, tmp_path):  # noqa: F811
    p = subs_file(tmp_path, SECRET_PLANS)
    c = make_client(ledger, pricing_path, subs=p)
    assert c.get("/api/overview").json()["subscription"] is not None  # the plans are loaded and in effect
    for rng in ("lifelong", "30d", "ytd", "last_year"):
        card = c.get("/api/card", params={"range": rng})
        assert card.status_code == 200 and "<svg" in card.text
        _assert_no_secrets(card.text, f"card {rng}")
        for fmt in ("csv", "json"):
            out = c.get("/api/export", params={"range": rng, "format": fmt})
            assert out.status_code == 200
            _assert_no_secrets(out.text, f"export {fmt} {rng}")
    for tool in ("claude-code", "codex"):
        _assert_no_secrets(c.get("/api/card", params={"tool": tool}).text, f"card tool={tool}")
        _assert_no_secrets(c.get("/api/export", params={"tool": tool, "format": "json"}).text, f"export tool={tool}")


def test_cli_card_never_includes_subscription_data(ledger, tmp_path):
    subs_file(tmp_path, SECRET_PLANS)
    out = tmp_path / "out" / "card.svg"
    res = CliRunner().invoke(cli.app, ["card", "--db", str(ledger), "--out", str(out)])
    assert res.exit_code == 0, res.output
    _assert_no_secrets(out.read_text(encoding="utf-8"), "tracker card")


def test_card_and_export_code_do_not_touch_subscriptions():
    src = Path(cli.__file__).parent
    assert "subscri" not in (src / "card.py").read_text(encoding="utf-8")
    assert "subscri" not in (src / "analytics.py").read_text(encoding="utf-8").lower()


# ================================================================== UI wiring

def test_subscriptions_tab_markup_and_script():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'id="subs-list"' in html and 'id="subs-grid"' not in html
    assert "still running" in html
    for needle in ['type: "date"', "JSON.stringify({ subscriptions: plan.subscriptions })", '"+ Add period"',
                   '"empty = Ongoing"', "No paid plan in this range", "paid day"]:
        assert needle in js, needle
    assert "{ fees }" not in js  # the UI sends periods, not the legacy body
