"""Phase 2: FastAPI dashboard API, static assets, loopback-only protections.

All data comes from tests/fixtures via a pytest tmp ledger; the real data/tracker.db
is never opened. TestClient runs in-process: no socket is bound.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient
from rich.console import Console
from typer.testing import CliRunner

from conftest import CANARY, TEST_PRICING
from lifelong_tracker import analytics, cli
from lifelong_tracker.storage import connect, connect_readonly
from lifelong_tracker.sync import sync_source
from lifelong_tracker.web.app import STATIC_DIR, create_app
from lifelong_tracker.web.security import host_header_name, is_loopback_host

LAST_DAY = date(2026, 3, 5)
BASE = "http://127.0.0.1:8765"
TOTAL_TOKENS = 126 + 551 + 8000 + 650
EXP_COST = (5211 + 10540 + 292.5 + 18) / 1e6


def make_client(db_path, pricing_path=TEST_PRICING, today=LAST_DAY, base_url=BASE) -> TestClient:
    return TestClient(create_app(db_path, pricing_path, today=lambda: today), base_url=base_url)


@pytest.fixture
def ledger(db_path, pricing, claude_source, collector):
    with connect(db_path) as conn:
        sync_source(conn, collector, claude_source, pricing)
    return db_path


@pytest.fixture
def client(ledger) -> TestClient:
    return make_client(ledger)


# ---------------------------------------------------------------- empty ledger

def test_empty_ledger_responses_do_not_create_db(db_path):
    assert not db_path.exists()
    c = make_client(db_path)

    ov = c.get("/api/overview").json()
    assert ov["totals"]["events"] == 0 and ov["totals"]["total_tokens"] == 0
    assert ov["totals"]["cache_hit_rate"] == 0 and ov["totals"]["cost_per_mtok"] == 0
    assert [x["key"] for x in ov["categories"]] == ["input", "output", "thinking", "cache_read", "cache_write"]
    assert all(x["share"] == 0 for x in ov["categories"])
    assert ov["streak"] == {"current": 0, "longest": 0, "longest_start": None, "longest_end": None, "active_days": 0}
    assert ov["unpriced_models"] == [] and ov["alerts"]["messages"] == []

    days = c.get("/api/timeseries", params={"range": "7d"}).json()["days"]
    assert len(days) == 7 and all(d["total_tokens"] == 0 for d in days)
    assert len(c.get("/api/timeseries").json()["days"]) == 1  # lifelong, nothing yet -> just today
    assert c.get("/api/breakdown").json()["rows"] == []
    hm = c.get("/api/heatmap").json()
    assert hm["year"] == 2026 and len(hm["days"]) == 365 and hm["max_tokens"] == 0 and hm["years"] == [2026]
    assert all(h["tokens"] == 0 for h in c.get("/api/hourly").json()["hours"])

    rows = list(csv.reader(io.StringIO(c.get("/api/export", params={"format": "csv"}).text)))
    assert rows == [list(analytics.EXPORT_COLUMNS)]
    assert c.get("/api/export", params={"format": "json"}).json()["meta"]["count"] == 0
    assert not db_path.exists()


def test_zero_byte_db_file_is_treated_as_empty(db_path):
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"")
    assert make_client(db_path).get("/api/overview").json()["totals"]["events"] == 0
    assert db_path.read_bytes() == b""


# ---------------------------------------------------------------- overview & ranges

def test_overview_lifelong(client):
    ov = client.get("/api/overview", params={"range": "lifelong"}).json()
    t = ov["totals"]
    assert (t["events"], t["input_tokens"], t["output_tokens"], t["thinking_tokens"],
            t["cache_read_tokens"], t["cache_write_tokens"]) == (5, 126, 551, 0, 8000, 650)
    assert t["total_tokens"] == TOTAL_TOKENS
    assert t["cost_usd"] == pytest.approx(EXP_COST)
    assert t["cache_hit_rate"] == pytest.approx(8000 / (126 + 8000 + 650))
    assert t["cost_per_mtok"] == pytest.approx(EXP_COST * 1e6 / (TOTAL_TOKENS - 150))
    assert sum(c["share"] for c in ov["categories"]) == pytest.approx(1.0)
    assert ov["streak"]["longest"] == 3 and ov["streak"]["current"] == 1
    assert ov["range_label"] == "Lifelong" and ov["start"] is None and ov["today"] == "2026-03-05"
    assert [u["model"] for u in ov["unpriced_models"]] == ["fictional-model-x1"]
    assert ov["alerts"]["needs_pricing"] and not ov["alerts"]["needs_reprice"]
    assert "tracker reprice" in ov["alerts"]["messages"][0]


@pytest.mark.parametrize(
    "range_name, today, events",
    [
        ("today", LAST_DAY, 2),
        ("today", date(2026, 3, 4), 0),
        ("7d", LAST_DAY, 5),
        ("7d", date(2026, 3, 8), 4),
        ("30d", date(2026, 4, 2), 2),
        ("90d", date(2026, 5, 29), 5),   # window starts 2026-03-01
        ("90d", date(2026, 5, 30), 4),
        ("ytd", date(2026, 12, 31), 5),
        ("ytd", date(2027, 1, 1), 0),
        ("lifelong", date(2030, 1, 1), 5),
    ],
)
def test_range_filters(ledger, range_name, today, events):
    c = make_client(ledger, today=today)
    assert c.get("/api/overview", params={"range": range_name}).json()["totals"]["events"] == events
    rows = c.get("/api/export", params={"format": "json", "range": range_name}).json()["events"]
    assert len(rows) == events
    series = c.get("/api/timeseries", params={"range": range_name}).json()["days"]
    assert sum(d["events"] for d in series) == events
    hours = c.get("/api/hourly", params={"range": range_name}).json()["hours"]
    assert sum(h["events"] for h in hours) == events


@pytest.mark.parametrize(
    "path, params",
    [
        ("/api/overview", {"range": "decade"}),
        ("/api/overview", {"range": "all"}),  # CLI alias, not part of the web contract
        ("/api/timeseries", {"range": ""}),
        ("/api/breakdown", {"by": "session"}),
        ("/api/breakdown", {"limit": 0}),
        ("/api/heatmap", {"year": 1500}),
        ("/api/hourly", {"range": "1y"}),
        ("/api/export", {"format": "xml"}),
    ],
)
def test_invalid_params_rejected(client, path, params):
    assert client.get(path, params=params).status_code == 422


def test_pricing_drift_banner_and_live_reload(ledger, tmp_path):
    pricing_copy = tmp_path / "pricing.json"
    shutil.copy(TEST_PRICING, pricing_copy)
    c = make_client(ledger, pricing_path=pricing_copy)
    assert not c.get("/api/overview").json()["alerts"]["needs_reprice"]

    spec = json.loads(pricing_copy.read_text(encoding="utf-8"))
    spec["models"]["claude-opus-4-5"]["output"] = 99.0
    spec["models"]["fictional-model-x1"] = {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0}
    pricing_copy.write_text(json.dumps(spec), encoding="utf-8")
    import os
    st = pricing_copy.stat()
    os.utime(pricing_copy, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # make mtime change visible

    a = c.get("/api/overview").json()["alerts"]
    assert a["needs_reprice"] and not a["needs_pricing"]
    assert set(c.get("/api/overview").json()["pricing_drift"]) == {"claude-opus-4-5-20251101", "fictional-model-x1"}


def test_broken_pricing_file_is_a_clear_500(ledger, tmp_path):
    bad = tmp_path / "pricing.json"
    bad.write_text("{not json", encoding="utf-8")
    r = make_client(ledger, pricing_path=bad).get("/api/overview")
    assert r.status_code == 500 and "pricing" in r.json()["detail"].lower()


# ---------------------------------------------------------------- series, breakdown, heatmap, hourly

def test_timeseries_is_zero_filled(client):
    days = client.get("/api/timeseries", params={"range": "7d"}).json()["days"]
    assert [d["date"] for d in days] == [f"2026-0{m}-{d:02d}" for m, d in
                                         [(2, 27), (2, 28), (3, 1), (3, 2), (3, 3), (3, 4), (3, 5)]]
    by_day = {d["date"]: d for d in days}
    assert by_day["2026-03-04"]["total_tokens"] == 0
    assert by_day["2026-03-01"]["output_tokens"] == 180
    assert sum(d["total_tokens"] for d in days) == TOTAL_TOKENS
    assert sum(d["cost_usd"] for d in days) == pytest.approx(EXP_COST)
    lifelong = client.get("/api/timeseries").json()["days"]
    assert lifelong[0]["date"] == "2026-03-01" and lifelong[-1]["date"] == "2026-03-05"


def test_breakdown_shares(client):
    bd = client.get("/api/breakdown", params={"by": "model"}).json()
    assert bd["total"]["total_tokens"] == TOTAL_TOKENS
    assert sum(r["token_share"] for r in bd["rows"]) == pytest.approx(1.0)
    assert sum(r["cost_share"] for r in bd["rows"]) == pytest.approx(1.0)
    assert bd["rows"][0]["key"] == "claude-opus-4-5-20251101"
    unpriced = next(r for r in bd["rows"] if r["key"] == "fictional-model-x1")
    assert unpriced["unpriced_events"] == 1 and unpriced["cost_usd"] == 0

    proj = client.get("/api/breakdown", params={"by": "project"}).json()["rows"]
    assert {r["key"]: r["events"] for r in proj} == {"alpha": 3, "beta": 2}
    tool = client.get("/api/breakdown", params={"by": "tool"}).json()["rows"]
    assert [r["key"] for r in tool] == ["claude-code"] and tool[0]["token_share"] == 1.0
    assert len(client.get("/api/breakdown", params={"limit": 1}).json()["rows"]) == 1


def test_heatmap(client):
    hm = client.get("/api/heatmap", params={"year": 2026}).json()
    assert len(hm["days"]) == 365 and hm["days"][0]["date"] == "2026-01-01"
    assert hm["active_days"] == 4 and hm["total_tokens"] == TOTAL_TOKENS
    cells = {d["date"]: d for d in hm["days"]}
    assert cells["2026-03-05"]["events"] == 2 and cells["2026-03-04"]["events"] == 0
    assert hm["max_tokens"] == max(d["tokens"] for d in hm["days"])
    assert hm["years"] == [2026]
    leap = client.get("/api/heatmap", params={"year": 2028}).json()
    assert len(leap["days"]) == 366 and leap["active_days"] == 0


def test_hourly(client):
    hours = client.get("/api/hourly").json()["hours"]
    assert [h["hour"] for h in hours] == list(range(24))
    assert {h["hour"]: h["events"] for h in hours if h["events"]} == {2: 1, 3: 1, 9: 1, 14: 1, 23: 1}


# ---------------------------------------------------------------- export

def test_export_csv(client):
    r = client.get("/api/export", params={"format": "csv", "range": "lifelong"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="lifelong-ai-tracker_lifelong_2026-03-05.csv"'
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert list(rows[0].keys()) == list(analytics.EXPORT_COLUMNS)
    assert len(rows) == 5
    assert [r_["timestamp_utc"] for r_ in rows] == sorted(r_["timestamp_utc"] for r_ in rows)
    by_msg = {r_["message_id"]: r_ for r_ in rows}
    assert by_msg["msg_fx_A001"]["output_tokens"] == "180" and by_msg["msg_fx_A001"]["local_date"] == "2026-03-01"
    assert by_msg["msg_fx_B001"]["cost_usd"] == ""  # unpriced
    assert sum(int(r_["total_tokens"]) for r_ in rows) == TOTAL_TOKENS
    assert CANARY not in r.text


def test_export_json(client):
    r = client.get("/api/export", params={"format": "json", "range": "today"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert 'filename="lifelong-ai-tracker_today_2026-03-05.json"' in r.headers["content-disposition"]
    body = r.json()
    assert body["meta"]["count"] == 2 and body["meta"]["columns"] == list(analytics.EXPORT_COLUMNS)
    assert body["meta"]["totals"]["events"] == 2
    assert {e["message_id"] for e in body["events"]} == {"msg_fx_B001", "msg_fx_B002"}
    assert next(e for e in body["events"] if e["message_id"] == "msg_fx_B001")["cost_usd"] is None
    assert CANARY not in r.text


# ---------------------------------------------------------------- static & offline guarantees

def test_index_and_static_assets(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Lifelong AI Tracker" in r.text
    for label in ("Today", "7D", "30D", "90D", "YTD", "Lifelong", "Export CSV", "Export JSON", "Total Cost",
                  "Total Tokens", "Cache Hit %", "Avg $ / 1M Tokens", "Active Streak", "Token Breakdown",
                  "Daily Trend", "Activity Heatmap", "24-Hour Activity"):
        assert label in r.text, label
    for asset, ctype in (("/static/app.js", "javascript"), ("/static/app.css", "text/css")):
        a = client.get(asset)
        assert a.status_code == 200 and ctype in a.headers["content-type"]
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "script-src 'self'" in csp


def test_frontend_has_no_external_references():
    allowed = {"http://www.w3.org/2000/svg"}  # SVG XML namespace identifier, never fetched
    for path in STATIC_DIR.iterdir():
        text = path.read_text(encoding="utf-8")
        urls = set(re.findall(r"(?:https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s\"'`)]*", text))
        assert urls <= allowed, (path.name, urls - allowed)
        assert "@import" not in text and "<script>" not in text  # no inline/imported code
        assert " style=" not in text  # inline style attributes would violate the CSP


def test_swagger_and_openapi_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_api_responses_are_not_cached(client):
    assert client.get("/api/overview").headers["cache-control"] == "no-store"


@pytest.mark.parametrize("host, ok", [
    ("127.0.0.1:8765", True), ("localhost:8765", True), ("[::1]:8765", True), ("127.0.0.1", True),
    ("evil.example.com", False), ("192.168.1.20:8765", False), ("0.0.0.0:8765", False), ("", False),
])
def test_host_header_guard(client, host, ok):
    r = client.get("/api/overview", headers={"host": host})
    assert (r.status_code == 200) is ok
    if not ok:
        assert r.status_code == 403


def test_server_connection_is_read_only(ledger):
    with connect_readonly(ledger) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM events")
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 5


def test_requests_do_not_modify_ledger(ledger):
    with connect(ledger) as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]
    c = make_client(ledger)
    for path in ("/api/overview", "/api/timeseries", "/api/breakdown", "/api/heatmap", "/api/hourly",
                 "/api/export?format=csv", "/api/export?format=json"):
        assert c.get(path).status_code == 200
    with connect(ledger) as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY id")] == before


# ---------------------------------------------------------------- loopback helpers & `tracker serve`

@pytest.mark.parametrize("host, ok", [
    ("127.0.0.1", True), ("127.0.0.53", True), ("::1", True), ("[::1]", True), ("localhost", True),
    ("LOCALHOST", True), ("0.0.0.0", False), ("::", False), ("192.168.0.2", False),
    ("10.0.0.1", False), ("example.com", False), ("localhost.example.com", False), ("", False),
])
def test_is_loopback_host(host, ok):
    assert is_loopback_host(host) is ok


@pytest.mark.parametrize("header, name", [
    ("127.0.0.1:8765", "127.0.0.1"), ("localhost", "localhost"), ("[::1]:8765", "::1"), ("::1", "::1"),
])
def test_host_header_name(header, name):
    assert host_header_name(header) == name


@pytest.fixture
def fake_uvicorn(monkeypatch):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr(cli, "console", Console(width=200, no_color=True, highlight=False))
    return calls


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
def test_serve_refuses_non_loopback(fake_uvicorn, db_path, host):
    r = CliRunner().invoke(cli.app, ["serve", "--host", host, "--db", str(db_path)])
    assert r.exit_code == 2
    assert "Refusing to bind" in r.output
    assert fake_uvicorn == []


@pytest.mark.parametrize("host, bind", [("127.0.0.1", "127.0.0.1"), ("localhost", "127.0.0.1"), ("::1", "::1")])
def test_serve_binds_loopback_only(fake_uvicorn, ledger, host, bind):
    r = CliRunner().invoke(cli.app, ["serve", "--host", host, "--port", "8799", "--db", str(ledger),
                                     "--pricing", str(TEST_PRICING)])
    assert r.exit_code == 0, r.output
    assert fake_uvicorn == [{"host": bind, "port": 8799, "log_level": "warning", "access_log": False,
                             "server_header": False, "proxy_headers": False}]
    assert ":8799" in r.output


def test_serve_default_host_is_loopback(fake_uvicorn, ledger):
    r = CliRunner().invoke(cli.app, ["serve", "--db", str(ledger), "--pricing", str(TEST_PRICING)])
    assert r.exit_code == 0, r.output
    assert fake_uvicorn[0]["host"] == "127.0.0.1" and fake_uvicorn[0]["port"] == 8765
