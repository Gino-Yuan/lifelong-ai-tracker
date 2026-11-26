"""Codex per-thread totals (tool == "codex", message_id == "thread_total").

Codex only logs one cumulative `tokens_used` per thread, stored raw in
input_tokens. Two rules keep it from distorting the dashboard:

* cost: priced at a blended rate, 0.90 cache read + 0.08 input + 0.02 output,
  on sync and on reprice (CLI and web), while raw token columns stay untouched;
* cache hit rate: such rows (with cache_read_tokens == 0) are left out; when
  nothing else is in scope the rate is None (JSON null, shown as an em dash).

Every other tool's cost must come out exactly as before. All data is built in
pytest temp directories; no real ledger or config is read.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import TEST_PRICING
from lifelong_tracker import analytics, cli
from lifelong_tracker.models import UsageEvent
from lifelong_tracker.pricing import CODEX_BLEND, PricingTable, is_codex_thread_total
from lifelong_tracker.storage import connect
from lifelong_tracker.storage.db import pricing_drift, reprice, transaction, upsert_events
from lifelong_tracker.web.app import STATIC_DIR, create_app

TODAY = date(2026, 11, 26)
BASE = "http://127.0.0.1:8765"
CODEX_MODEL = "gpt-5-codex"
CODEX_PRICE = {"input": 1.25, "output": 10.0, "cache_read": 0.125, "cache_write": 0.0}
BLENDED = 0.90 * 0.125 + 0.08 * 1.25 + 0.02 * 10.0          # 0.4125 USD / 1M
_ids = itertools.count(1)


@pytest.fixture
def pricing_path(tmp_path) -> Path:
    p = tmp_path / "prices" / "pricing.json"
    p.parent.mkdir()
    raw = json.loads(TEST_PRICING.read_text(encoding="utf-8"))
    raw["models"][CODEX_MODEL] = dict(CODEX_PRICE)
    p.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return p


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "ledger" / "tracker.db"


def ev(ts, *, tool="claude-code", model="claude-sonnet-4-5", msg=None,
       inp=0, out=0, think=0, cr=0, cw=0) -> UsageEvent:
    n = next(_ids)
    return UsageEvent(
        timestamp=datetime.fromisoformat(ts).replace(tzinfo=timezone.utc),
        tool=tool, model=model, project_name="proj", session_id=f"s-{n}",
        request_id=f"req-{n}", message_id=msg or f"msg-{n}",
        input_tokens=inp, output_tokens=out, thinking_tokens=think,
        cache_read_tokens=cr, cache_write_tokens=cw,
    )


def thread(ts, tokens, **kw) -> UsageEvent:
    return ev(ts, tool="codex", model=CODEX_MODEL, msg="thread_total", inp=tokens, **kw)


def put(db, pricing_path, events) -> None:
    with connect(db) as conn:
        with transaction(conn):
            upsert_events(conn, events, PricingTable.load(pricing_path))


def rows(db) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    finally:
        conn.close()


def client(db, pricing_path) -> TestClient:
    return TestClient(create_app(db, pricing_path, today=lambda: TODAY), base_url=BASE)


# Other tools, each with every token category populated where the model allows.
OTHERS = [
    dict(tool="claude-code", model="claude-sonnet-4-5", inp=1_234, out=5_678, cr=90_001, cw=7_007),
    dict(tool="claude-code", model="claude-opus-4-5", inp=321, out=4_321, think=999, cr=12_345, cw=50),
    dict(tool="antigravity", model="claude-opus-4", inp=77_777, out=1_111, cr=3, cw=19),
    dict(tool="opencode", model="claude-haiku-4-5", inp=10, out=20, cr=30, cw=40),
    # Codex rows that are NOT bare thread totals keep the normal formula.
    dict(tool="codex", model=CODEX_MODEL, inp=4_000, out=600, msg="thread_total"),
    dict(tool="codex", model=CODEX_MODEL, inp=8_000, msg="turn-7"),
]


def other_events():
    return [ev(f"2026-11-{10 + i}T09:00:00", **{**spec, "think": spec.get("think", 0)})
            for i, spec in enumerate(OTHERS)]


def full_cost(table: PricingTable, r) -> float:
    return table.lookup(r["model"])[1].cost(
        input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
        thinking_tokens=r["thinking_tokens"], cache_read_tokens=r["cache_read_tokens"],
        cache_write_tokens=r["cache_write_tokens"])


# --------------------------------------------------------------------------- pricing rule

def test_blend_weights_and_rate():
    assert CODEX_BLEND == {"cache_read": 0.90, "input": 0.08, "output": 0.02}
    assert sum(CODEX_BLEND.values()) == pytest.approx(1.0)
    table = PricingTable.from_dict({"models": {CODEX_MODEL: CODEX_PRICE}})
    assert table.lookup(CODEX_MODEL)[1].codex_blended_rate == pytest.approx(BLENDED)


@pytest.mark.parametrize("tool,msg,extra,blended", [
    ("codex", "thread_total", {}, True),
    ("codex", "thread_total", {"output_tokens": 1}, False),
    ("codex", "thread_total", {"thinking_tokens": 1}, False),
    ("codex", "thread_total", {"cache_read_tokens": 1}, False),
    ("codex", "thread_total", {"cache_write_tokens": 1}, False),
    ("codex", "turn-1", {}, False),
    ("claude-code", "thread_total", {}, False),
    ("antigravity", "thread_total", {}, False),
    ("opencode", "thread_total", {}, False),
])
def test_cost_for_applies_blend_only_to_bare_codex_thread_totals(tool, msg, extra, blended):
    table = PricingTable.from_dict({"models": {CODEX_MODEL: CODEX_PRICE}})
    tokens = {"input_tokens": 2_000_000, **extra}
    assert is_codex_thread_total(tool, msg, **tokens) is blended
    got = table.cost_for(CODEX_MODEL, tool=tool, message_id=msg, **tokens)
    want = (2.0 * BLENDED if blended else table.lookup(CODEX_MODEL)[1].cost(**tokens))
    assert got == pytest.approx(want, rel=1e-12)
    if blended:
        assert got == pytest.approx(0.825)        # vs 2.50 at the full input rate


def test_cost_for_without_tool_is_unchanged():
    table = PricingTable.load(TEST_PRICING)
    tokens = dict(input_tokens=11, output_tokens=22, cache_read_tokens=33, cache_write_tokens=44)
    assert table.cost_for("claude-sonnet-4-5", **tokens) == table.lookup("claude-sonnet-4-5")[1].cost(**tokens)
    assert table.cost_for("not-a-model", tool="codex", message_id="thread_total", input_tokens=5) is None


# --------------------------------------------------------------------------- sync

def test_sync_prices_thread_total_at_blend_and_keeps_raw_tokens(db, pricing_path):
    put(db, pricing_path, [thread("2026-11-20T08:00:00", 3_000_000)])
    (r,) = rows(db)
    assert (r["tool"], r["message_id"]) == ("codex", "thread_total")
    assert r["input_tokens"] == 3_000_000
    assert (r["output_tokens"], r["thinking_tokens"], r["cache_read_tokens"], r["cache_write_tokens"]) == (0, 0, 0, 0)
    assert r["cost_usd"] == pytest.approx(3.0 * BLENDED, rel=1e-12)        # 1.2375, not 3.75


def test_sync_reprices_a_growing_thread_total(db, pricing_path):
    def snap(tokens):
        return UsageEvent(
            timestamp=datetime(2026, 11, 20, 8, tzinfo=timezone.utc), tool="codex",
            model=CODEX_MODEL, session_id="thr-1", request_id="thr-1",
            message_id="thread_total", input_tokens=tokens)
    put(db, pricing_path, [snap(1_000_000)])
    put(db, pricing_path, [snap(5_000_000)])
    (r,) = rows(db)
    assert r["input_tokens"] == 5_000_000
    assert r["cost_usd"] == pytest.approx(5.0 * BLENDED, rel=1e-12)


def test_sync_other_tools_cost_exactly_as_before(db, pricing_path):
    put(db, pricing_path, other_events())
    table = PricingTable.load(pricing_path)
    got = rows(db)
    assert len(got) == len(OTHERS)
    for r in got:
        assert r["cost_usd"] == full_cost(table, r), dict(r)


# --------------------------------------------------------------------------- reprice

def _bump_prices(pricing_path: Path) -> dict:
    raw = json.loads(pricing_path.read_text(encoding="utf-8"))
    raw["models"][CODEX_MODEL] = {"input": 2.0, "output": 16.0, "cache_read": 0.2, "cache_write": 0.0}
    raw["models"]["claude-sonnet-4-5"]["input"] = 4.0
    pricing_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return raw


def test_reprice_uses_blend_and_leaves_others_exact(db, pricing_path):
    put(db, pricing_path, other_events() + [thread("2026-11-21T08:00:00", 4_000_000),
                                            thread("2026-11-22T08:00:00", 123_457)])
    assert pricing_drift_now(db, pricing_path) == []
    _bump_prices(pricing_path)
    assert CODEX_MODEL in pricing_drift_now(db, pricing_path)
    table = PricingTable.load(pricing_path)
    with connect(db) as conn:
        res = reprice(conn, table)
    blended = 0.90 * 0.2 + 0.08 * 2.0 + 0.02 * 16.0          # 0.66
    for r in rows(db):
        if r["message_id"] == "thread_total" and is_codex_thread_total(
                r["tool"], r["message_id"], output_tokens=r["output_tokens"]):
            assert r["cost_usd"] == pytest.approx(r["input_tokens"] * blended / 1e6, rel=1e-12)
        else:
            assert r["cost_usd"] == pytest.approx(full_cost(table, r), rel=1e-12, abs=1e-15), dict(r)
    assert res.cost_after == pytest.approx(sum(r["cost_usd"] for r in rows(db)), rel=1e-12)
    assert pricing_drift_now(db, pricing_path) == []


def test_reprice_matches_sync_row_for_row(db, pricing_path, tmp_path):
    events = other_events() + [thread("2026-11-21T08:00:00", 987_654)]
    put(db, pricing_path, events)
    synced = {r["id"]: r["cost_usd"] for r in rows(db)}
    with connect(db) as conn:
        conn.execute("UPDATE events SET cost_usd = NULL")
        conn.commit()
        reprice(conn, PricingTable.load(pricing_path))
    for r in rows(db):
        assert r["cost_usd"] == pytest.approx(synced[r["id"]], rel=1e-12, abs=1e-15)


def test_cli_reprice_applies_blend(db, pricing_path):
    put(db, pricing_path, [thread("2026-11-21T08:00:00", 2_000_000)])
    _bump_prices(pricing_path)
    res = CliRunner().invoke(cli.app, ["reprice", "--db", str(db), "--pricing", str(pricing_path)])
    assert res.exit_code == 0, res.output
    assert rows(db)[0]["cost_usd"] == pytest.approx(2.0 * 0.66, rel=1e-12)


def test_web_save_and_reprice_applies_blend(db, pricing_path):
    put(db, pricing_path, [thread("2026-11-21T08:00:00", 2_000_000)])
    c = client(db, pricing_path)
    body = {"model": CODEX_MODEL, "input": 2.0, "output": 16.0, "cache_read": 0.2,
            "cache_write": 0.0, "thinking": 16.0}
    r = c.post("/api/pricing", json=body, headers={"Origin": BASE})
    assert r.status_code == 200, r.text
    assert rows(db)[0]["cost_usd"] == pytest.approx(2.0 * 0.66, rel=1e-12)


def pricing_drift_now(db, pricing_path) -> list[str]:
    with connect(db) as conn:
        return pricing_drift(conn, PricingTable.load(pricing_path))


# --------------------------------------------------------------------------- cache hit rate

def mixed_ledger(db, pricing_path):
    put(db, pricing_path, [
        ev("2026-11-20T09:00:00", inp=1_000, cr=8_000, cw=1_000),       # 80% hit
        ev("2026-11-21T09:00:00", tool="opencode", model="claude-haiku-4-5", inp=500, cr=1_500),
        thread("2026-11-22T09:00:00", 50_000_000),                       # would drag the blend down
        thread("2026-11-23T09:00:00", 7_000_000),
    ])


def test_hit_rate_excludes_thread_totals(db, pricing_path):
    mixed_ledger(db, pricing_path)
    with connect(db) as conn:
        t = analytics.summarize(conn, "lifelong", TODAY)
        cc = analytics.summarize(conn, "lifelong", TODAY, tool="claude-code")
    assert t.cache_hit_rate == pytest.approx(9_500 / (1_500 + 9_500 + 1_000))
    assert cc.cache_hit_rate == pytest.approx(0.8)
    # The thread totals still count everywhere else.
    assert t.input_tokens == 1_500 + 57_000_000
    assert t.total_tokens == 1_500 + 9_500 + 1_000 + 57_000_000
    assert t.events == 4


def test_hit_rate_is_none_when_only_thread_totals(db, pricing_path):
    mixed_ledger(db, pricing_path)
    with connect(db) as conn:
        assert analytics.summarize(conn, "lifelong", TODAY, tool="codex").cache_hit_rate is None
        assert analytics.summarize(conn, "lifelong", TODAY, model=CODEX_MODEL).cache_hit_rate is None
        # A range that holds only the thread totals is just as empty of breakdown.
        assert analytics.summarize_span(conn, date(2026, 11, 22), date(2026, 11, 23)).cache_hit_rate is None
        # No rows at all keeps the old 0.0.
        assert analytics.summarize(conn, "today", TODAY).cache_hit_rate == 0.0


def test_codex_rows_with_breakdown_still_count(db, pricing_path):
    put(db, pricing_path, [
        thread("2026-11-22T09:00:00", 9_000_000),
        ev("2026-11-23T09:00:00", tool="codex", model=CODEX_MODEL, msg="thread_total", inp=100, cr=300),
        ev("2026-11-24T09:00:00", tool="codex", model=CODEX_MODEL, msg="turn-3", inp=200, cr=200, cw=100),
    ])
    with connect(db) as conn:
        t = analytics.summarize(conn, "lifelong", TODAY, tool="codex")
    assert t.cache_hit_rate == pytest.approx(500 / (300 + 500 + 100))


def test_totals_addition_keeps_exclusion():
    a = analytics.Totals(events=1, input_tokens=100, cache_read_tokens=300)
    b = analytics.Totals(events=1, input_tokens=9_000, no_breakdown_events=1, no_breakdown_input_tokens=9_000)
    assert (a + b).cache_hit_rate == pytest.approx(0.75)
    assert b.cache_hit_rate is None
    assert analytics.Totals().cache_hit_rate == 0.0


def test_api_overview_and_breakdown(db, pricing_path):
    mixed_ledger(db, pricing_path)
    c = client(db, pricing_path)
    all_tools = c.get("/api/overview").json()["totals"]
    assert all_tools["cache_hit_rate"] == pytest.approx(9_500 / 12_000)
    codex = c.get("/api/overview", params={"tool": "codex"}).json()["totals"]
    assert codex["cache_hit_rate"] is None
    assert codex["events"] == 2 and codex["input_tokens"] == 57_000_000
    by_tool = {r["key"] if "key" in r else r["name"]: r
               for r in c.get("/api/breakdown", params={"by": "tool"}).json()["rows"]}
    assert by_tool["codex"]["cache_hit_rate"] is None
    assert by_tool["claude-code"]["cache_hit_rate"] == pytest.approx(0.8)


def test_card_and_cli_handle_missing_breakdown(db, pricing_path):
    mixed_ledger(db, pricing_path)
    svg = client(db, pricing_path).get("/api/card", params={"tool": "codex"}).text
    assert "No cache breakdown" in svg and "—" in svg and "nan" not in svg.lower()
    svg_all = client(db, pricing_path).get("/api/card").text
    assert f"{9_500 / 12_000 * 100:.1f}%" in svg_all
    res = CliRunner().invoke(cli.app, ["stats", "--db", str(db), "--pricing", str(pricing_path)])
    assert res.exit_code == 0, res.output


def test_dashboard_shows_dash_and_subtitle():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'noBreakdown ? "—" : fmtPct(t.cache_hit_rate)' in js
    assert '"No cache breakdown"' in js
    assert 'const HIT_SUB = "Cache read ÷ (input + cache read + cache write)";' in js
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="kpi-hit-sub">Cache read ÷ (input + cache read + cache write)<' in html


# --------------------------------------------------------------------------- shipped estimates

def test_shipped_pricing_has_estimated_fallback_entries():
    from lifelong_tracker.config import DEFAULT_PRICING_PATH
    raw = json.loads(Path(DEFAULT_PRICING_PATH).read_text(encoding="utf-8"))
    want = {"input": 2.0, "output": 10.0, "thinking": 10.0, "cache_read": 0.20, "cache_write": 0.0}
    table = PricingTable.load(DEFAULT_PRICING_PATH)
    assert raw["models"]["codex-auto-review"] == want
    assert table.lookup("codex-auto-review")[0] == "codex-auto-review"
    assert any("codex-auto-review" in n and "Estimate" in n for n in raw["_meta"]["notes"])
    # no personal guess for events whose log names no model: they stay unpriced
    assert "unknown" not in raw["models"] and table.lookup("unknown") is None
    assert not any("'unknown'" in n for n in raw["_meta"]["notes"])
