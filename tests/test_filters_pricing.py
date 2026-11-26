"""Dashboard upgrade: tool / model scope filters, tier-suffix price matching and
`POST /api/pricing` (atomic pricing.json write + automatic reprice).

The ledger is built from the fabricated fixtures only (tests/fixtures/claude_code
and tests/fixtures/antigravity) in pytest's workspace-local tmp dir; pricing.json
is always a tmp copy of tests/fixtures/pricing_test.json. The real data/tracker.db
and the project's pricing.json are never opened.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURES, TEST_PRICING
from lifelong_tracker import fsutil as fsutil_mod
from lifelong_tracker.collectors import AntigravityCollector, ClaudeCodeCollector
from lifelong_tracker.pricing import (
    PricingTable,
    base_model_name,
    set_model_price,
    strip_tier_suffix,
)
from lifelong_tracker.storage import connect, pricing_drift
from lifelong_tracker.sync import sync_source
from lifelong_tracker.web.app import STATIC_DIR, create_app

TODAY = date(2026, 11, 26)
BASE = "http://127.0.0.1:8765"
ENDPOINTS = ("/api/overview", "/api/timeseries", "/api/breakdown", "/api/heatmap",
             "/api/hourly", "/api/export", "/api/card")

# Fixture facts (see tests/test_web.py and tests/test_antigravity_collector.py)
CLAUDE = {"events": 5, "tokens": 126 + 551 + 8000 + 650}
AG_LOW = {"events": 3, "tokens": (27895 + 3410) + (31000 + 120 + 24000 + 800) + (5000 + 700 + 1000)}
AG_HIGH = {"events": 1, "tokens": 1200 + 950 + 30000 + 500}
AG = {"events": 4, "tokens": AG_LOW["tokens"] + AG_HIGH["tokens"]}
AG_SUMS = {"input_tokens": 65095, "output_tokens": 5180, "thinking_tokens": 800,
           "cache_read_tokens": 55000, "cache_write_tokens": 500}
TOKEN_COLS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "cache_write_tokens")
GEMINI_PRICE = {"input": 2.0, "output": 12.0, "cache_read": 0.2, "cache_write": 2.5}


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def pricing_file(tmp_path) -> Path:
    dst = tmp_path / "pricing" / "pricing.json"
    dst.parent.mkdir()
    shutil.copy(TEST_PRICING, dst)
    return dst


@pytest.fixture
def mixed_ledger(db_path, pricing, claude_source, tmp_path) -> Path:
    ag_source = tmp_path / "brain"
    shutil.copytree(FIXTURES / "antigravity" / "brain", ag_source)
    with connect(db_path) as conn:
        sync_source(conn, ClaudeCodeCollector(), claude_source, pricing)
        sync_source(conn, AntigravityCollector(), ag_source, pricing)
    return db_path


def make_client(db_path, pricing_path=TEST_PRICING) -> TestClient:
    return TestClient(create_app(db_path, pricing_path, today=lambda: TODAY), base_url=BASE)


@pytest.fixture
def client(mixed_ledger) -> TestClient:
    return make_client(mixed_ledger)


@pytest.fixture
def wclient(mixed_ledger, pricing_file) -> TestClient:
    """Client whose pricing.json is a writable tmp copy."""
    return make_client(mixed_ledger, pricing_file)


def snapshot(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as c:
        return c.execute(f"SELECT id, tool, model, {', '.join(TOKEN_COLS)}, cost_usd FROM events ORDER BY id").fetchall()


def token_snapshot(db: Path) -> list[tuple]:
    return [row[:-1] for row in snapshot(db)]


def events_in(client: TestClient, path: str, params: dict) -> int:
    """Number of events an endpoint reports under the given scope."""
    params = {"range": "lifelong", **params}
    if path == "/api/heatmap":
        params.pop("range")
        params["year"] = 2026
    if path == "/api/export":
        params["format"] = "json"
    r = client.get(path, params=params)
    assert r.status_code == 200, (path, r.text)
    if path == "/api/card":
        return -1  # SVG; checked separately
    body = r.json()
    return {
        "/api/overview": lambda: body["totals"]["events"],
        "/api/timeseries": lambda: sum(d["events"] for d in body["days"]),
        "/api/breakdown": lambda: sum(x["events"] for x in body["rows"]),
        "/api/heatmap": lambda: sum(d["events"] for d in body["days"]),
        "/api/hourly": lambda: sum(h["events"] for h in body["hours"]),
        "/api/export": lambda: body["meta"]["count"],
    }[path]()


# ---------------------------------------------------------------- /api/tools

def test_tools_lists_tools_with_data_in_registry_order(client):
    tools = client.get("/api/tools").json()["tools"]
    assert [(t["tool"], t["label"]) for t in tools] == [("claude-code", "Claude Code"), ("antigravity", "Antigravity")]
    by = {t["tool"]: t for t in tools}
    assert by["claude-code"]["events"] == CLAUDE["events"] and by["claude-code"]["tokens"] == CLAUDE["tokens"]
    assert by["antigravity"]["events"] == AG["events"] and by["antigravity"]["tokens"] == AG["tokens"]
    assert by["antigravity"]["first_seen"].startswith("2026-11-25")


def test_tools_empty_ledger(db_path):
    assert make_client(db_path).get("/api/tools").json() == {"tools": []}
    assert not db_path.exists()


# ---------------------------------------------------------------- tool filter

@pytest.mark.parametrize("path", [p for p in ENDPOINTS if p != "/api/card"])
@pytest.mark.parametrize("params, expected", [
    ({}, CLAUDE["events"] + AG["events"]),
    ({"tool": "all"}, CLAUDE["events"] + AG["events"]),
    ({"tool": ""}, CLAUDE["events"] + AG["events"]),
    ({"tool": "claude-code"}, CLAUDE["events"]),
    ({"tool": "claude_code"}, CLAUDE["events"]),  # underscore spelling accepted
    ({"tool": "Antigravity"}, AG["events"]),
    ({"tool": "opencode"}, 0),                     # known tool, no data -> empty, not an error
])
def test_tool_filter_applies_to_every_endpoint(client, path, params, expected):
    assert events_in(client, path, params) == expected


def test_tool_scopes_add_up_to_all_tools(client):
    total = client.get("/api/overview").json()["totals"]
    parts = [client.get("/api/overview", params={"tool": t}).json()["totals"] for t in ("claude-code", "antigravity")]
    for key in ("events", *TOKEN_COLS, "total_tokens", "cost_usd", "unpriced_events"):
        assert sum(p[key] for p in parts) == pytest.approx(total[key]), key


def test_tool_filter_details(client):
    ov = client.get("/api/overview", params={"tool": "antigravity"}).json()
    assert ov["filter"] == {"tool": "antigravity", "tool_label": "Antigravity", "model": None}
    assert {k: ov["totals"][k] for k in AG_SUMS} == AG_SUMS
    assert ov["streak"]["active_days"] == 2 and ov["streak"]["current"] == 2  # Nov 25 + Nov 26
    assert {u["model"] for u in ov["unpriced_models"]} == {"gemini-4-neon-low", "gemini-4-neon-high"}
    assert {u["base_model"] for u in ov["unpriced_models"]} == {"gemini-4-neon"}
    assert "fictional-model-x1" not in json.dumps(ov["unpriced_models"])

    cc = client.get("/api/overview", params={"tool": "claude-code"}).json()
    assert [u["model"] for u in cc["unpriced_models"]] == ["fictional-model-x1"]
    assert cc["unpriced_models"][0]["base_model"] is None
    assert cc["streak"]["longest"] == 3 and cc["streak"]["current"] == 0
    assert client.get("/api/overview").json()["filter"]["tool_label"] == "All Tools"

    hours = client.get("/api/hourly", params={"tool": "antigravity"}).json()["hours"]
    assert {h["hour"]: h["events"] for h in hours if h["events"]} == {1: 1, 15: 3}

    hm = client.get("/api/heatmap", params={"tool": "antigravity"}).json()
    assert hm["active_days"] == 2 and hm["total_tokens"] == AG["tokens"]
    assert {d["date"] for d in hm["days"] if d["events"]} == {"2026-11-25", "2026-11-26"}

    series = client.get("/api/timeseries", params={"tool": "antigravity"}).json()["days"]
    assert [d["date"] for d in series] == ["2026-11-25", "2026-11-26"]  # lifelong starts at the tool's first day

    bd = client.get("/api/breakdown", params={"by": "tool"}).json()["rows"]
    assert [(r["key"], r["label"]) for r in bd] == [("antigravity", "Antigravity"), ("claude-code", "Claude Code")]


# ---------------------------------------------------------------- model filter

@pytest.mark.parametrize("path", [p for p in ENDPOINTS if p != "/api/card"])
@pytest.mark.parametrize("params, expected", [
    ({"model": "gemini-4-neon-low"}, AG_LOW["events"]),
    ({"model": "gemini-4-neon-high"}, AG_HIGH["events"]),
    ({"model": "all"}, CLAUDE["events"] + AG["events"]),
    ({"model": "gemini-4-neon-low", "tool": "antigravity"}, AG_LOW["events"]),
    ({"model": "gemini-4-neon-low", "tool": "claude-code"}, 0),  # combined scopes intersect
    ({"model": "gemini-4-neon"}, 0),                             # exact match, never a prefix
    ({"model": "fictional-model-x1", "tool": "claude_code"}, 1),
])
def test_model_filter_applies_to_every_endpoint(client, path, params, expected):
    assert events_in(client, path, params) == expected


def test_model_focus_details(client):
    p = {"model": "gemini-4-neon-low"}
    ov = client.get("/api/overview", params=p).json()
    assert ov["filter"]["model"] == "gemini-4-neon-low"
    assert ov["totals"]["total_tokens"] == AG_LOW["tokens"]
    assert ov["totals"]["thinking_tokens"] == 800 and ov["totals"]["cache_write_tokens"] == 0
    assert sum(c["tokens"] for c in ov["categories"]) == AG_LOW["tokens"]
    assert [u["model"] for u in ov["unpriced_models"]] == ["gemini-4-neon-low"]

    rows = client.get("/api/breakdown", params={**p, "by": "model"}).json()["rows"]
    assert [r["key"] for r in rows] == ["gemini-4-neon-low"] and rows[0]["token_share"] == 1.0
    assert rows[0]["base_model"] == "gemini-4-neon" and rows[0]["price_key"] == "gemini-4-neon-low"

    high = client.get("/api/overview", params={"model": "gemini-4-neon-high"}).json()
    assert high["streak"]["active_days"] == 1 and high["totals"]["cache_write_tokens"] == 500

    hours = client.get("/api/hourly", params=p).json()["hours"]
    assert {h["hour"]: h["events"] for h in hours if h["events"]} == {1: 1, 15: 2}


def test_filtered_export_and_card(client):
    r = client.get("/api/export", params={"format": "csv", "tool": "antigravity"})
    assert r.headers["content-disposition"] == \
        'attachment; filename="lifelong-ai-tracker_lifelong_antigravity_2026-11-26.csv"'
    lines = r.text.strip().splitlines()
    assert len(lines) == 1 + AG["events"] and all(",antigravity," in ln for ln in lines[1:])

    r = client.get("/api/card", params={"tool": "antigravity", "model": "gemini-4-neon-high", "download": True})
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["content-disposition"] == (
        'attachment; filename="lifelong-ai-tracker_card_lifelong_antigravity_gemini-4-neon-high_2026-11-26.svg"')
    svg = r.text
    assert "Antigravity · gemini-4-neon-high" in svg
    assert "claude" not in svg.lower() and "gemini-4-neon-low" not in svg
    assert "1 request" in svg  # one event in scope

    unscoped = client.get("/api/card").text
    assert "All Tools" not in unscoped and "claude-opus-4-5-20251101" in unscoped


def test_filename_slug_cannot_break_headers(client):
    r = client.get("/api/export", params={"format": "json", "model": 'evil"; filename=x.sh'})
    assert r.status_code == 200 and r.json()["meta"]["count"] == 0
    cd = r.headers["content-disposition"]
    assert cd.count('"') == 2 and ";" not in cd.split("filename=", 1)[1]


@pytest.mark.parametrize("params", [
    {"tool": "../etc"}, {"tool": "claude code"}, {"tool": "a" * 65}, {"tool": "x;drop"},
    {"model": "bad\nname"}, {"model": "nul\x00"}, {"model": "m" * 257},
])
@pytest.mark.parametrize("path", ENDPOINTS)
def test_invalid_scope_rejected(client, path, params):
    assert client.get(path, params=params).status_code == 422


def test_scope_values_are_bound_not_interpolated(client):
    # SQL-looking input is just an unknown model: empty result, ledger untouched.
    r = client.get("/api/overview", params={"model": "x' OR '1'='1"})
    assert r.status_code == 200 and r.json()["totals"]["events"] == 0


# ---------------------------------------------------------------- tier suffix normalisation

@pytest.mark.parametrize("raw, base", [
    ("gemini-4-neon-xhigh", "gemini-4-neon"),
    ("gemini-4-neon-high", "gemini-4-neon"),
    ("gemini-4-neon-medium", "gemini-4-neon"),
    ("gemini-4-neon-low", "gemini-4-neon"),
    ("gemini-4-neon-tiered-high", "gemini-4-neon"),
    ("gemini-4-neon-tiered-low", "gemini-4-neon"),
    ("gemini-4-neon", "gemini-4-neon"),
    ("gemini-4-neon-thigh", "gemini-4-neon-thigh"),   # whole segment only
    ("gemini-4-neon-highway", "gemini-4-neon-highway"),
    ("high", "high"),
    ("-high", "-high"),
])
def test_strip_tier_suffix(raw, base):
    assert strip_tier_suffix(raw) == base


def test_base_model_name():
    assert base_model_name("gemini-4-neon-low") == "gemini-4-neon"
    assert base_model_name("  Anthropic/Claude-Opus-4-5-High ") == "claude-opus-4-5"
    assert base_model_name("claude-opus-4-5") is None


@pytest.fixture
def gemini_table(tmp_path) -> PricingTable:
    spec = json.loads(TEST_PRICING.read_text(encoding="utf-8"))
    spec["models"]["gemini-4-neon"] = {"input": 2, "output": 12, "cache_read": 0.2, "cache_write": 2.5}
    spec["models"]["gemini-4-pulse-high"] = {"input": 9, "output": 9, "cache_read": 9, "cache_write": 9}
    spec["models"]["gemini-4-pulse"] = {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1}
    return PricingTable.from_dict(spec)


@pytest.mark.parametrize("model, key", [
    ("gemini-4-neon-xhigh", "gemini-4-neon"),
    ("gemini-4-neon-high", "gemini-4-neon"),
    ("gemini-4-neon-low", "gemini-4-neon"),
    ("gemini-4-neon-tiered-high", "gemini-4-neon"),
    ("gemini-4-neon-tiered-low", "gemini-4-neon"),
    ("google/gemini-4-neon-medium", "gemini-4-neon"),
    ("gemini-4-pulse-high", "gemini-4-pulse-high"),  # an explicit variant row wins
    ("gemini-4-pulse-low", "gemini-4-pulse"),
    ("claude-opus-4-5-high", "claude-opus-4-5"),     # not the shorter 'claude-opus-4'
    ("claude-opus-4-high", "claude-opus-4"),
    ("claude-opus-4-5-20251101-xhigh", "claude-opus-4-5"),
])
def test_tier_variants_share_base_price(gemini_table, model, key):
    assert gemini_table.lookup(model)[0] == key


@pytest.mark.parametrize("model", [
    "gemini-5-neon-high",     # different major version
    "gemini-3-neon-low",
    "gemini-4-5-neon-high",   # different minor version
    "gemini-4-neon-2-high",
    "claude-opus-4-9-high",   # stripping the tier must not unlock a minor-version borrow
    "claude-opus-5-xhigh",
    "gemini-4-neonx-high",    # not a segment boundary
    "high",
])
def test_tier_stripping_does_not_cross_versions(gemini_table, model):
    assert gemini_table.lookup(model) is None


# ---------------------------------------------------------------- set_model_price (atomic file write)

def test_set_model_price_adds_entry_and_preserves_file(pricing_file):
    before = json.loads(pricing_file.read_text(encoding="utf-8"))
    os.chmod(pricing_file, 0o640)
    upd = set_model_price(pricing_file, "gemini-4-neon", dict(GEMINI_PRICE))
    assert upd.key == "gemini-4-neon" and not upd.replaced
    after = json.loads(pricing_file.read_text(encoding="utf-8"))
    assert after["_meta"] == before["_meta"]
    assert list(after["models"])[:-1] == list(before["models"])  # order kept, new entry appended
    for name, spec in before["models"].items():
        assert {k: float(v) for k, v in spec.items()} == after["models"][name]
    assert after["models"]["gemini-4-neon"] == GEMINI_PRICE  # thinking omitted -> defaults to output on load
    assert PricingTable.load(pricing_file).lookup("gemini-4-neon-low")[1].thinking == 12.0
    assert upd.table.lookup("gemini-4-neon-high")[0] == "gemini-4-neon"
    assert (pricing_file.stat().st_mode & 0o777) == 0o640
    assert sorted(p.name for p in pricing_file.parent.iterdir()) == ["pricing.json"]  # no temp files left
    text = pricing_file.read_text(encoding="utf-8")
    assert '    "gemini-4-neon":' in text and text.endswith("}\n")  # one line per model


def test_set_model_price_replaces_and_normalises_name(pricing_file):
    upd = set_model_price(pricing_file, "Anthropic/Claude-Opus-4-5", {**GEMINI_PRICE, "thinking": 30})
    assert upd.key == "claude-opus-4-5" and upd.replaced
    models = json.loads(pricing_file.read_text(encoding="utf-8"))["models"]
    assert models["claude-opus-4-5"]["thinking"] == 30.0
    assert list(models).count("claude-opus-4-5") == 1


def test_set_model_price_is_atomic_on_failure(pricing_file, monkeypatch):
    original = pricing_file.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(fsutil_mod.os, "replace", boom)
    with pytest.raises(OSError):
        set_model_price(pricing_file, "gemini-4-neon", dict(GEMINI_PRICE))
    assert pricing_file.read_bytes() == original
    assert sorted(p.name for p in pricing_file.parent.iterdir()) == ["pricing.json"]


@pytest.mark.parametrize("model, prices", [
    ("gemini-4-neon", {**GEMINI_PRICE, "input": -1}),
    ("gemini-4-neon", {**GEMINI_PRICE, "output": float("nan")}),
    ("gemini-4-neon", {**GEMINI_PRICE, "cache_write": float("inf")}),
    ("gemini-4-neon", {k: v for k, v in GEMINI_PRICE.items() if k != "cache_read"}),
    ("gemini-4-neon", {**GEMINI_PRICE, "surprise": 1}),
    ("_meta", GEMINI_PRICE),
    ("bad name", GEMINI_PRICE),
    ("", GEMINI_PRICE),
])
def test_set_model_price_rejects_bad_input(pricing_file, model, prices):
    original = pricing_file.read_bytes()
    with pytest.raises(ValueError):
        set_model_price(pricing_file, model, prices)
    assert pricing_file.read_bytes() == original


# ---------------------------------------------------------------- POST /api/pricing

def test_post_pricing_base_model_writes_and_reprices(wclient, mixed_ledger, pricing_file):
    tokens_before = token_snapshot(mixed_ledger)
    assert {u["model"] for u in wclient.get("/api/overview", params={"tool": "antigravity"}).json()["unpriced_models"]} \
        == {"gemini-4-neon-low", "gemini-4-neon-high"}

    r = wclient.post("/api/pricing", json={"model": "gemini-4-neon", **GEMINI_PRICE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "gemini-4-neon" and body["replaced"] is False
    assert body["price"] == {**GEMINI_PRICE, "thinking": 12.0}
    assert body["matched_models"] == ["gemini-4-neon-high", "gemini-4-neon-low"]
    assert body["unpriced_models"] == ["fictional-model-x1"]
    assert body["reprice"]["events_unpriced"] == 1 and body["reprice"]["events_priced"] == 8

    # pricing.json on disk
    assert json.loads(pricing_file.read_text(encoding="utf-8"))["models"]["gemini-4-neon"] == GEMINI_PRICE

    # ledger: only cost_usd changed, and it is exactly right
    assert token_snapshot(mixed_ledger) == tokens_before
    exp = (AG_SUMS["input_tokens"] * 2 + AG_SUMS["output_tokens"] * 12 + AG_SUMS["thinking_tokens"] * 12
           + AG_SUMS["cache_read_tokens"] * 0.2 + AG_SUMS["cache_write_tokens"] * 2.5) / 1e6
    with sqlite3.connect(mixed_ledger) as c:
        cost, nulls = c.execute(
            "SELECT SUM(cost_usd), SUM(cost_usd IS NULL) FROM events WHERE tool = 'antigravity'").fetchone()
        assert cost == pytest.approx(exp) and nulls == 0

    # the dashboard sees it immediately (no restart, no mtime race) and nothing is stale
    ov = wclient.get("/api/overview", params={"tool": "antigravity"}).json()
    assert ov["unpriced_models"] == [] and not ov["alerts"]["needs_reprice"]
    assert ov["totals"]["cost_usd"] == pytest.approx(exp) and ov["totals"]["unpriced_events"] == 0
    with connect(mixed_ledger) as conn:
        assert pricing_drift(conn, PricingTable.load(pricing_file)) == []


def test_post_pricing_single_variant_only(wclient, mixed_ledger):
    r = wclient.post("/api/pricing", json={"model": "gemini-4-neon-high", **GEMINI_PRICE, "thinking": 3.5})
    assert r.status_code == 200, r.text
    assert r.json()["matched_models"] == ["gemini-4-neon-high"]
    assert "gemini-4-neon-low" in r.json()["unpriced_models"]
    left = wclient.get("/api/overview", params={"tool": "antigravity"}).json()["unpriced_models"]
    assert [u["model"] for u in left] == ["gemini-4-neon-low"]


def test_post_pricing_replaces_existing_price(wclient, mixed_ledger):
    before = wclient.get("/api/overview", params={"tool": "claude-code"}).json()["totals"]["cost_usd"]
    r = wclient.post("/api/pricing", json={"model": "claude-opus-4-5", "input": 0, "output": 0,
                                           "cache_read": 0, "cache_write": 0})
    assert r.status_code == 200 and r.json()["replaced"] is True
    after = wclient.get("/api/overview", params={"tool": "claude-code"}).json()["totals"]["cost_usd"]
    assert after < before


def test_post_pricing_without_ledger_does_not_create_db(db_path, pricing_file):
    c = make_client(db_path, pricing_file)
    r = c.post("/api/pricing", json={"model": "gemini-4-neon", **GEMINI_PRICE})
    assert r.status_code == 200 and r.json()["reprice"] is None
    assert not db_path.exists()
    assert "gemini-4-neon" in json.loads(pricing_file.read_text(encoding="utf-8"))["models"]


@pytest.mark.parametrize("payload", [
    {"model": "gemini-4-neon", **GEMINI_PRICE, "input": -1},
    {"model": "gemini-4-neon", **GEMINI_PRICE, "thinking": -0.01},
    {"model": "gemini-4-neon", **GEMINI_PRICE, "output": 1e9},
    {"model": "gemini-4-neon", **GEMINI_PRICE, "input": "2.0"},     # strings are not numbers
    {"model": "gemini-4-neon", **GEMINI_PRICE, "input": True},
    {"model": "gemini-4-neon", **GEMINI_PRICE, "input": None},
    {"model": "gemini-4-neon", **GEMINI_PRICE, "path": "../../etc/passwd"},  # unknown field
    {"model": "gemini-4-neon", **GEMINI_PRICE, "apply_to_base": True},
    {"model": "gemini-4-neon", "input": 1, "output": 1, "cache_read": 1},     # missing cache_write
    {"model": "../pricing", **GEMINI_PRICE},
    {"model": "a/../../b", **GEMINI_PRICE},
    {"model": "/etc/passwd/", **GEMINI_PRICE},
    {"model": "..\\pricing", **GEMINI_PRICE},
    {"model": "_meta", **GEMINI_PRICE},
    {"model": "gemini 4 neon", **GEMINI_PRICE},
    {"model": "gemini\n4", **GEMINI_PRICE},
    {"model": "", **GEMINI_PRICE},
    {"model": "m" * 201, **GEMINI_PRICE},
    {"model": 42, **GEMINI_PRICE},
    {**GEMINI_PRICE},
])
def test_post_pricing_rejects_invalid_payloads(wclient, mixed_ledger, pricing_file, payload):
    disk, ledger = pricing_file.read_bytes(), snapshot(mixed_ledger)
    r = wclient.post("/api/pricing", json=payload)
    assert r.status_code == 422, (payload, r.text)
    assert pricing_file.read_bytes() == disk and snapshot(mixed_ledger) == ledger


@pytest.mark.parametrize("raw", [
    b'{"model": "gemini-4-neon", "input": NaN, "output": 1, "cache_read": 1, "cache_write": 1}',
    b'{"model": "gemini-4-neon", "input": Infinity, "output": 1, "cache_read": 1, "cache_write": 1}',
    b'[1, 2, 3]',
    b'{not json',
])
def test_post_pricing_rejects_non_finite_and_malformed_json(wclient, pricing_file, raw):
    disk = pricing_file.read_bytes()
    r = wclient.post("/api/pricing", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert pricing_file.read_bytes() == disk


@pytest.mark.parametrize("kwargs, status", [
    ({"data": {"model": "gemini-4-neon", "input": "1"}}, 415),                        # form post
    ({"content": b"model=x", "headers": {"content-type": "text/plain"}}, 415),         # simple CORS request
    ({"json": {"model": "gemini-4-neon", **GEMINI_PRICE},
      "headers": {"origin": "http://evil.example.com"}}, 403),
    ({"json": {"model": "gemini-4-neon", **GEMINI_PRICE},
      "headers": {"origin": "http://127.0.0.1:9999"}}, 403),                            # other local port
    ({"json": {"model": "gemini-4-neon", **GEMINI_PRICE},
      "headers": {"sec-fetch-site": "cross-site"}}, 403),
    ({"json": {"model": "gemini-4-neon", **GEMINI_PRICE},
      "headers": {"host": "evil.example.com"}}, 403),                                   # DNS rebinding
    ({"content": b'{"model": "' + b"m" * 5000 + b'"}',
      "headers": {"content-type": "application/json"}}, 413),
])
def test_post_pricing_cross_site_guards(wclient, mixed_ledger, pricing_file, kwargs, status):
    disk, ledger = pricing_file.read_bytes(), snapshot(mixed_ledger)
    r = wclient.post("/api/pricing", **kwargs)
    assert r.status_code == status, r.text
    assert pricing_file.read_bytes() == disk and snapshot(mixed_ledger) == ledger


def test_post_pricing_accepts_same_origin_browser_headers(wclient):
    r = wclient.post("/api/pricing", json={"model": "gemini-4-neon", **GEMINI_PRICE},
                     headers={"origin": BASE, "sec-fetch-site": "same-origin"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store" and "default-src 'none'" in r.headers["content-security-policy"]


def test_other_methods_on_pricing_are_not_allowed(wclient):
    assert wclient.get("/api/pricing").status_code == 200  # read-only listing (test_pricing_table.py)
    assert wclient.put("/api/pricing", json={}).status_code == 405
    assert wclient.delete("/api/pricing").status_code in (405, 415)  # body-less: stopped by the write guard
    assert wclient.patch("/api/pricing", json={}).status_code == 405
    # writes exist nowhere else
    assert wclient.post("/api/overview", json={}).status_code == 405


# ---------------------------------------------------------------- frontend wiring

def test_dashboard_has_filter_and_pricing_ui():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for needle in ('id="tool-control"', ">All Tools<", 'id="filterbar"', 'id="model-chip-clear"',
                   'id="price-dialog"', "Apply to base model", "Save &amp; Reprice", 'id="alert-actions"'):
        assert needle in html, needle
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for needle in ('fetch("/api/pricing"', '"Content-Type": "application/json"', "api(\"/api/tools\")",
                   'p.set("tool", state.tool)', 'p.set("model", state.model)', "history.replaceState",
                   "openPriceDialog(", "Set Price", "e.preventDefault()"):
        assert needle in js, needle
