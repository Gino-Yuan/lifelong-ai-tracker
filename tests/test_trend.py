"""Trend chart upgrade: `GET /api/timeseries?granularity=…&split_by=…` and the
`last_year` range.

Ledger = fabricated Claude Code fixtures (2026-03-01 … 03-05) + fabricated
Antigravity fixtures (2026-11-25 … 11-26), built in pytest's workspace-local tmp
dir. The real data/tracker.db is never opened.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FIXTURES, TEST_PRICING
from lifelong_tracker import analytics, cli
from lifelong_tracker.analytics import bucket_end, bucket_start, resolve_granularity
from lifelong_tracker.collectors import AntigravityCollector, ClaudeCodeCollector
from lifelong_tracker.storage import connect
from lifelong_tracker.sync import sync_source
from lifelong_tracker.web.app import STATIC_DIR, create_app

BASE = "http://127.0.0.1:8765"
NOV = date(2026, 11, 26)
METRICS = ("events", "input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens",
           "cache_write_tokens", "total_tokens", "cost_usd")
CLAUDE = {"events": 5, "total_tokens": 126 + 551 + 8000 + 650}
AG = {"events": 4, "total_tokens": 65095 + 5180 + 800 + 55000 + 500}
JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")


@pytest.fixture
def ledger(db_path, pricing, claude_source, tmp_path) -> Path:
    ag_source = tmp_path / "brain"
    shutil.copytree(FIXTURES / "antigravity" / "brain", ag_source)
    with connect(db_path) as conn:
        sync_source(conn, ClaudeCodeCollector(), claude_source, pricing)
        sync_source(conn, AntigravityCollector(), ag_source, pricing)
    return db_path


def client_for(db_path, today: date = NOV) -> TestClient:
    return TestClient(create_app(db_path, TEST_PRICING, today=lambda: today), base_url=BASE)


@pytest.fixture
def client(ledger) -> TestClient:
    return client_for(ledger)


def ts(client: TestClient, **params) -> dict:
    r = client.get("/api/timeseries", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def axis(points: list[dict]) -> list[tuple[str, str]]:
    return [(p["start"], p["end"]) for p in points]


def assert_contiguous(points: list[dict]) -> None:
    for a, b in zip(points, points[1:]):
        assert date.fromisoformat(b["start"]) == date.fromisoformat(a["end"]) + timedelta(days=1)


def sums(points: list[dict]) -> dict:
    return {k: sum(p[k] for p in points) for k in METRICS}


# ---------------------------------------------------------------- bucket helpers

@pytest.mark.parametrize("n_days, gran", [
    (1, "day"), (120, "day"), (121, "week"), (730, "week"), (731, "month"), (5000, "month"),
])
def test_auto_granularity_thresholds(n_days, gran):
    assert resolve_granularity("auto", n_days) == gran
    for explicit in ("day", "week", "month"):
        assert resolve_granularity(explicit, n_days) == explicit


def test_unknown_granularity_raises():
    with pytest.raises(ValueError):
        resolve_granularity("year", 10)


@pytest.mark.parametrize("d, gran, start, end", [
    ("2026-11-26", "day", "2026-11-26", "2026-11-26"),
    ("2026-11-26", "week", "2026-11-23", "2026-11-29"),   # Thursday -> Monday..Sunday
    ("2026-11-23", "week", "2026-11-23", "2026-11-29"),   # a Monday is its own start
    ("2026-11-29", "week", "2026-11-23", "2026-11-29"),   # a Sunday closes the week
    ("2026-12-31", "week", "2026-12-28", "2027-01-03"),   # weeks cross the year boundary
    ("2026-11-26", "month", "2026-11-01", "2026-11-30"),
    ("2026-12-15", "month", "2026-12-01", "2026-12-31"),
    ("2028-02-10", "month", "2028-02-01", "2028-02-29"),  # leap year
    ("2027-02-10", "month", "2027-02-01", "2027-02-28"),
])
def test_bucket_bounds(d, gran, start, end):
    s = bucket_start(date.fromisoformat(d), gran)
    assert s.isoformat() == start and bucket_end(s, gran).isoformat() == end


# ---------------------------------------------------------------- granularity=day|week|month

def test_default_response_is_backward_compatible(client):
    body = ts(client, range="lifelong")
    assert body["granularity_requested"] == "auto" and body["split_by"] == "none"
    assert "series" not in body
    days = body["days"]
    assert days[0]["date"] == "2026-03-01" and days[-1]["date"] == "2026-11-26"
    assert len(days) == (NOV - date(2026, 3, 1)).days + 1
    assert set(days[0]) == {"date", *METRICS}
    # explicit granularity never changes the daily `days` list
    assert ts(client, range="lifelong", granularity="month")["days"] == days


def test_day_granularity_is_one_zero_filled_point_per_day(client):
    body = ts(client, range="30d", granularity="day")
    assert body["granularity"] == "day"
    pts = body["points"]
    assert len(pts) == 30 and pts[0]["start"] == "2026-10-28" and pts[-1]["start"] == "2026-11-26"
    assert all(p["start"] == p["end"] for p in pts)
    assert_contiguous(pts)
    assert [p["start"] for p in pts] == [d["date"] for d in body["days"]]
    nonzero = {p["start"]: p["events"] for p in pts if p["events"]}
    assert nonzero == {"2026-11-25": 3, "2026-11-26": 1}
    assert sum(1 for p in pts if p["events"] == 0) == 28  # zero-filled


def test_week_granularity_calendar_weeks_zero_filled(client):
    body = ts(client, range="lifelong", granularity="week")
    assert body["granularity"] == "week"
    pts = body["points"]
    for p in pts:
        s, e = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
        assert s.weekday() == 0 and e - s == timedelta(days=6)
    assert_contiguous(pts)
    # first bucket holds the first active day, last bucket holds today
    assert pts[0]["start"] <= "2026-03-01" <= pts[0]["end"]
    assert pts[-1]["start"] <= NOV.isoformat() <= pts[-1]["end"]
    assert pts[0]["start"] == "2026-02-23"  # Mon before Sun 2026-03-01
    # Sun 03-01 is alone in its week; Mon 03-02 .. Thu 03-05 share the next one
    assert pts[0]["events"] == 1 and pts[1]["events"] == 4
    assert pts[-1]["events"] == AG["events"]
    assert sum(1 for p in pts if p["events"] == 0) == len(pts) - 3
    assert sums(pts) == pytest.approx(sums(body["days"]))


def test_month_granularity_calendar_months_zero_filled(client):
    body = ts(client, range="lifelong", granularity="month")
    pts = body["points"]
    assert axis(pts) == [
        ("2026-03-01", "2026-03-31"), ("2026-04-01", "2026-04-30"), ("2026-05-01", "2026-05-31"),
        ("2026-06-01", "2026-06-30"), ("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31"),
        ("2026-09-01", "2026-09-30"), ("2026-10-01", "2026-10-31"), ("2026-11-01", "2026-11-30"),
    ]
    assert pts[0]["events"] == CLAUDE["events"] and pts[0]["total_tokens"] == CLAUDE["total_tokens"]
    assert pts[-1]["events"] == AG["events"] and pts[-1]["total_tokens"] == AG["total_tokens"]
    assert all(p["events"] == 0 and p["total_tokens"] == 0 and p["cost_usd"] == 0 for p in pts[1:-1])
    assert sums(pts) == pytest.approx(sums(body["days"]))


@pytest.mark.parametrize("range_name, today, gran", [
    ("7d", NOV, "day"),
    ("90d", NOV, "day"),
    ("ytd", NOV, "week"),        # 330 days
    ("lifelong", NOV, "week"),   # 271 days
    ("lifelong", date(2029, 1, 1), "month"),  # > 730 days since the first event
])
def test_auto_granularity_follows_span(ledger, range_name, today, gran):
    body = ts(client_for(ledger, today), range=range_name)
    assert body["granularity"] == gran and body["granularity_requested"] == "auto"


def test_granularity_respects_scope_filters(client):
    # lifelong starts at that tool's first active day and, like every range, runs through today
    pts = ts(client, range="lifelong", granularity="month", tool="claude-code")["points"]
    assert pts[0]["start"] == "2026-03-01" and pts[-1]["start"] == "2026-11-01" and len(pts) == 9
    assert pts[0]["events"] == CLAUDE["events"] and all(p["events"] == 0 for p in pts[1:])
    pts = ts(client, range="lifelong", granularity="week", tool="antigravity")["points"]
    assert axis(pts) == [("2026-11-23", "2026-11-29")]
    pts = ts(client, range="ytd", granularity="month", model="gemini-4-neon-high")["points"]
    assert len(pts) == 11 and sum(p["events"] for p in pts) == 1


# ---------------------------------------------------------------- split_by=tool

@pytest.mark.parametrize("gran", ["day", "week", "month", "auto"])
@pytest.mark.parametrize("range_name", ["lifelong", "ytd", "30d"])
def test_split_series_share_the_axis_and_sum_to_combined(client, gran, range_name):
    body = ts(client, range=range_name, granularity=gran, split_by="tool")
    series = body["series"]
    for s in series:
        assert axis(s["points"]) == axis(body["points"])  # identical buckets, same order
        assert s["totals"] == pytest.approx(sums(s["points"]))
    for i, p in enumerate(body["points"]):
        for k in METRICS:
            assert sum(s["points"][i][k] for s in series) == pytest.approx(p[k]), (i, k)


def test_split_by_tool_lifelong_month(client):
    body = ts(client, range="lifelong", granularity="month", split_by="tool")
    assert body["split_by"] == "tool"
    assert [(s["tool"], s["label"]) for s in body["series"]] == [
        ("claude-code", "Claude Code"), ("antigravity", "Antigravity")]
    cc, ag = body["series"]
    assert len(cc["points"]) == len(ag["points"]) == 9
    assert cc["totals"]["events"] == CLAUDE["events"] and ag["totals"]["events"] == AG["events"]
    # each agent is zero-filled where only the other one was active
    assert cc["points"][0]["events"] == 5 and cc["points"][-1]["events"] == 0
    assert ag["points"][0]["events"] == 0 and ag["points"][-1]["events"] == 4
    assert all(p["events"] == 0 for s in body["series"] for p in s["points"][1:-1])


def test_split_by_tool_daily_alignment_covers_every_day(client):
    body = ts(client, range="lifelong", granularity="day", split_by="tool")
    n = (NOV - date(2026, 3, 1)).days + 1
    assert len(body["points"]) == n
    assert all(len(s["points"]) == n for s in body["series"])
    cc = {p["start"]: p["events"] for p in body["series"][0]["points"] if p["events"]}
    assert set(cc) == {"2026-03-01", "2026-03-02", "2026-03-03", "2026-03-05"}


def test_split_only_lists_agents_active_in_scope(client):
    assert [s["tool"] for s in ts(client, range="30d", split_by="tool")["series"]] == ["antigravity"]
    one = ts(client, range="lifelong", split_by="tool", tool="antigravity")
    assert [s["tool"] for s in one["series"]] == ["antigravity"]
    assert axis(one["series"][0]["points"]) == axis(one["points"])
    by_model = ts(client, range="lifelong", split_by="tool", model="fictional-model-x1")
    assert [s["tool"] for s in by_model["series"]] == ["claude-code"]


def test_split_on_empty_ledger(db_path):
    body = ts(client_for(db_path), range="lifelong", split_by="tool", granularity="week")
    assert body["series"] == [] and len(body["points"]) == 1 and body["points"][0]["events"] == 0
    assert not db_path.exists()


def test_split_none_has_no_series(client):
    assert "series" not in ts(client, split_by="none", granularity="week")


# ---------------------------------------------------------------- last_year range

def test_last_year_range_is_previous_calendar_year(ledger):
    c = client_for(ledger, date(2027, 2, 1))
    ov = c.get("/api/overview", params={"range": "last_year"}).json()
    assert ov["range_label"] == "Last year" and ov["start"] == "2026-01-01" and ov["end"] == "2026-12-31"
    assert ov["totals"]["events"] == CLAUDE["events"] + AG["events"]
    assert c.get("/api/overview", params={"range": "ytd"}).json()["totals"]["events"] == 0

    body = ts(c, range="last_year", granularity="month", split_by="tool")
    assert [p["start"][:7] for p in body["points"]] == [f"2026-{m:02d}" for m in range(1, 13)]
    assert len(body["days"]) == 365 and body["days"][-1]["date"] == "2026-12-31"
    assert all(axis(s["points"]) == axis(body["points"]) for s in body["series"])
    assert sum(h["events"] for h in c.get("/api/hourly", params={"range": "last_year"}).json()["hours"]) == 9
    assert c.get("/api/export", params={"range": "last_year", "format": "json"}).json()["meta"]["count"] == 9
    assert sum(r["events"] for r in c.get("/api/breakdown", params={"range": "last_year"}).json()["rows"]) == 9

    svg = c.get("/api/card", params={"range": "last_year"}).text
    assert "2026 AI Usage" in svg and "Jan 1 – Dec 31, 2026" in svg


def test_last_year_excludes_this_year(ledger):
    # in 2026 itself, "last year" is 2025: nothing there
    c = client_for(ledger, NOV)
    assert c.get("/api/overview", params={"range": "last_year"}).json()["totals"]["events"] == 0
    assert len(ts(c, range="last_year", granularity="day")["points"]) == 365


def test_cli_card_accepts_last_year(ledger, tmp_path):
    out = tmp_path / "card.svg"
    r = CliRunner().invoke(cli.app, ["card", "--range", "last_year", "--db", str(ledger), "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert f"{date.today().year - 1} AI Usage" in out.read_text(encoding="utf-8")


def test_analytics_last_year_bounds():
    assert analytics.range_bounds("last_year", NOV) == (date(2025, 1, 1), date(2025, 12, 31))
    assert analytics.range_bounds("ytd", NOV) == (date(2026, 1, 1), NOV)
    assert analytics.range_bounds("lifelong", NOV) == (None, NOV)


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize("params", [
    {"granularity": "year"}, {"granularity": "hour"}, {"granularity": "DAY"}, {"granularity": ""},
    {"granularity": "day;drop"}, {"split_by": "model"}, {"split_by": "project"}, {"split_by": "tools"},
    {"split_by": ""}, {"range": "last-year"}, {"range": "lastyear"},
])
def test_invalid_trend_params_rejected(client, params):
    assert client.get("/api/timeseries", params=params).status_code == 422


def test_analytics_trend_rejects_bad_split(ledger):
    with connect(ledger) as conn, pytest.raises(ValueError):
        analytics.trend(conn, "lifelong", NOV, "day", "model")


# ---------------------------------------------------------------- frontend wiring

def test_trend_controls_in_markup():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for needle in ('id="gran-toggle"', ">Daily<", ">Weekly<", ">Monthly<", 'id="view-toggle"',
                   ">Combined<", ">By Agent<", 'data-range="last_year"', ">Last Year<", 'id="trend-legend"'):
        assert needle in html, needle
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for needle in ("granularity: state.gran", 'split_by: splitByAgent() ? "tool" : "none"',
                   'p.set("gran", state.gran)', 'p.set("view", state.view)', '"trend-line"',
                   "linearGradient", '"last_year"', "Available when All Tools is selected"):
        assert needle in js, needle
    assert "bucketDays" not in js  # bucketing now happens server-side


@pytest.mark.skipif(not JSC.exists(), reason="macOS JavaScriptCore not available")
def test_app_js_parses_with_javascriptcore():
    script = 'try { new Function(readFile(arguments[0])); print("SYNTAX_OK"); } catch (e) { print("SYNTAX_ERROR: " + e); }'
    out = subprocess.run([str(JSC), "-e", script, "--", str(STATIC_DIR / "app.js")],
                         capture_output=True, text=True, timeout=30, encoding="utf-8")
    assert out.stdout.strip() == "SYNTAX_OK", out.stdout + out.stderr
