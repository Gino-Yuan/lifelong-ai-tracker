"""Model Pricing table: GET /api/pricing, provider grouping, edit -> reprice sync, UI wiring.

Everything runs on tmp copies of tests/fixtures/pricing_test.json and the
fabricated Claude Code / Antigravity ledgers; the real pricing.json and
data/tracker.db are never opened.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURES, TEST_PRICING, JSC, requires_jsc
from lifelong_tracker.collectors import AntigravityCollector, ClaudeCodeCollector
from lifelong_tracker.pricing import PROVIDERS, PricingTable, provider_of, set_model_price
from lifelong_tracker.storage import connect
from lifelong_tracker.sync import sync_source
from lifelong_tracker.web.app import STATIC_DIR, create_app

TODAY = date(2026, 11, 26)
BASE = "http://127.0.0.1:8765"
SMOKE = FIXTURES / "js" / "pricing_smoke.js"
STUB_DOM = FIXTURES / "js" / "stub_dom.js"
FIELDS = ("input", "output", "thinking", "cache_read", "cache_write")
EXTRA = {
    "gemini-4-neon": {"input": 3.0, "output": 15.0, "thinking": 15.0, "cache_read": 0.3, "cache_write": 0.0},
    "gpt-6.1-sol": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 0.0},
    "gpt-6-sol": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 0.0},
    "gpt-10-mini": {"input": 0.5, "output": 2.0, "cache_read": 0.05, "cache_write": 0.0},
    "deepseek-v4": {"input": 0.27, "output": 1.1, "cache_read": 0.07, "cache_write": 0.0},
}


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def pricing_file(tmp_path) -> Path:
    dst = tmp_path / "prices" / "pricing.json"
    dst.parent.mkdir()
    raw = json.loads(TEST_PRICING.read_text())
    raw["_meta"].update({"currency": "USD", "unit": "per_million_tokens", "updated": "2026-11-01"})
    raw["models"].update(EXTRA)
    dst.write_text(json.dumps(raw, indent=2))
    return dst


@pytest.fixture
def ledger(db_path, pricing, claude_source, tmp_path) -> Path:
    ag = tmp_path / "brain"
    shutil.copytree(FIXTURES / "antigravity" / "brain", ag)
    with connect(db_path) as conn:
        sync_source(conn, ClaudeCodeCollector(), claude_source, pricing)
        sync_source(conn, AntigravityCollector(), ag, pricing)
    return db_path


def client_for(db: Path, pricing_path: Path) -> TestClient:
    return TestClient(create_app(db, pricing_path, today=lambda: TODAY), base_url=BASE)


@pytest.fixture
def client(ledger, pricing_file) -> TestClient:
    return client_for(ledger, pricing_file)


# ------------------------------------------------------------------ provider inference

@pytest.mark.parametrize("model, provider", [
    ("claude-sonnet-4-5", "Claude"),
    ("anthropic/claude-opus-4-5", "Claude"),
    ("us.anthropic.claude-haiku-4-5", "Claude"),
    ("Claude-Fable-5", "Claude"),
    ("gemini-4-neon", "Gemini"),
    ("gemini-3.8-flash", "Gemini"),
    ("gpt-6-luna", "GPT"),
    ("GPT-6.1-sol", "GPT"),
    ("chatgpt-4o-latest", "GPT"),
    ("o3-mini", "GPT"),
    ("codex-mini-latest", "GPT"),
    ("opus", "Other"),          # 'o' + letter is not the o-series
    ("deepseek-v4", "Other"),
    ("fictional-model-x1", "Other"),
    ("my-claude-finetune", "Other"),  # prefix, not substring
])
def test_provider_of(model, provider):
    assert provider_of(model) == provider


def test_entries_grouped_then_natural_order(pricing_file):
    entries = PricingTable.load(pricing_file).entries()
    assert [p for p, _, _ in entries] == sorted((p for p, _, _ in entries), key=PROVIDERS.index)
    gpt = [k for p, k, _ in entries if p == "GPT"]
    assert gpt == ["gpt-6-sol", "gpt-6.1-sol", "gpt-10-mini"]  # digit runs compare as numbers


# ------------------------------------------------------------------ GET /api/pricing

def test_get_pricing_shape(client, pricing_file):
    r = client.get("/api/pricing")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"currency", "unit", "unit_label", "updated", "count", "used_count", "providers", "models"}
    assert (body["currency"], body["unit"], body["unit_label"]) == ("USD", "per_million_tokens", "USD per 1M tokens")
    assert body["updated"] == "2026-11-01"
    assert body["count"] == len(body["models"]) == 4 + len(EXTRA)
    assert body["providers"] == [
        {"provider": "Claude", "count": 4}, {"provider": "Gemini", "count": 1},
        {"provider": "GPT", "count": 3}, {"provider": "Other", "count": 1},
    ]
    for m in body["models"]:
        assert set(m) == {"model", "provider", *FIELDS, "used", "effective_from", "history"}
        assert isinstance(m["used"], bool) and m["effective_from"] is None and m["history"] == []
        assert all(isinstance(m[f], float) and m[f] >= 0 for f in FIELDS)
        assert m["provider"] == provider_of(m["model"])


def test_get_pricing_order_and_values(client):
    models = client.get("/api/pricing").json()["models"]
    assert [m["model"] for m in models] == [
        "claude-haiku-4-5", "claude-opus-4", "claude-opus-4-5", "claude-sonnet-4-5",
        "gemini-4-neon",
        "gpt-6-sol", "gpt-6.1-sol", "gpt-10-mini",
        "deepseek-v4",
    ]
    by = {m["model"]: m for m in models}
    assert by["claude-sonnet-4-5"] == {"model": "claude-sonnet-4-5", "provider": "Claude", "input": 3.0,
                                       "output": 15.0, "thinking": 15.0,  # omitted -> output rate
                                       "cache_read": 0.3, "cache_write": 3.75,
                                       "used": True, "effective_from": None, "history": []}
    assert by["gemini-4-neon"]["cache_write"] == 0.0


def test_get_pricing_is_read_only_and_leaks_no_paths(ledger, pricing_file, tmp_path):
    before = (_digest(pricing_file), pricing_file.stat().st_mtime_ns, _digest(ledger))
    c = client_for(ledger, pricing_file)
    text = c.get("/api/pricing").text
    assert (_digest(pricing_file), pricing_file.stat().st_mtime_ns, _digest(ledger)) == before
    assert str(tmp_path) not in text and "pricing.json" not in text and "/Users" not in text


def test_get_pricing_works_without_a_ledger(pricing_file, tmp_path):
    missing = tmp_path / "nope" / "tracker.db"
    r = client_for(missing, pricing_file).get("/api/pricing")
    assert r.status_code == 200 and r.json()["count"] == 9
    assert not missing.exists()


@pytest.mark.parametrize("meta", [{}, {"updated": "yesterday"}, {"updated": 20261101}, None])
def test_updated_falls_back_to_file_date(tmp_path, meta):
    p = tmp_path / "pricing.json"
    raw = {"models": {"claude-sonnet-4-5": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}}}
    if meta is not None:
        raw["_meta"] = meta
    p.write_text(json.dumps(raw))
    stamp = date(2026, 10, 3)
    ts = int(__import__("datetime").datetime(2026, 10, 3, 12).timestamp())
    os.utime(p, (ts, ts))
    body = client_for(tmp_path / "db.sqlite", p).get("/api/pricing").json()
    assert body["updated"] == stamp.isoformat()
    assert body["currency"] == "USD" and body["count"] == 1


def test_external_edit_is_picked_up(client, pricing_file):
    assert client.get("/api/pricing").json()["count"] == 9
    raw = json.loads(pricing_file.read_text())
    raw["models"]["gemini-3-flash"] = {"input": 0.5, "output": 3.0, "cache_read": 0.05, "cache_write": 0.0}
    pricing_file.write_text(json.dumps(raw))
    st = pricing_file.stat()
    os.utime(pricing_file, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    body = client.get("/api/pricing").json()
    assert body["count"] == 10 and "gemini-3-flash" in {m["model"] for m in body["models"]}


def test_broken_pricing_file_is_a_500_not_a_crash(ledger, tmp_path):
    p = tmp_path / "pricing.json"
    p.write_text("{ not json")
    r = client_for(ledger, p).get("/api/pricing")
    assert r.status_code == 500 and "Cannot load pricing file" in r.json()["detail"]


# ------------------------------------------------------------------ edit -> table + reprice stay in sync

def _expected_cost(row: sqlite3.Row, price: dict) -> float:
    return (row["input_tokens"] * price["input"] + row["output_tokens"] * price["output"]
            + row["thinking_tokens"] * price["thinking"] + row["cache_read_tokens"] * price["cache_read"]
            + row["cache_write_tokens"] * price["cache_write"]) / 1e6


def _events(db: Path, like: str) -> list[sqlite3.Row]:
    with connect(db) as conn:
        return conn.execute("SELECT * FROM events WHERE model LIKE ? ORDER BY id", (like,)).fetchall()


def test_edit_existing_model_updates_table_file_and_costs(client, ledger, pricing_file):
    old_total = client.get("/api/overview", params={"range": "lifelong"}).json()["totals"]["cost_usd"]
    before = {m["model"]: m for m in client.get("/api/pricing").json()["models"]}
    assert before["claude-sonnet-4-5"]["input"] == 3.0

    new = {"input": 1.25, "output": 6.5, "thinking": 7.0, "cache_read": 0.125, "cache_write": 1.5}
    r = client.post("/api/pricing", json={"model": "claude-sonnet-4-5", **new})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replaced"] is True and body["model"] == "claude-sonnet-4-5"
    assert set(body["matched_models"]) == {"claude-sonnet-4-5", "claude-sonnet-4-5-20250929"}

    # the table reflects the edit, nothing else moved, and the date is stamped
    after = client.get("/api/pricing").json()
    assert after["updated"] == TODAY.isoformat()
    rows = {m["model"]: m for m in after["models"]}
    assert {f: rows["claude-sonnet-4-5"][f] for f in FIELDS} == new
    assert [m["model"] for m in after["models"]] == list(before)  # same rows, same order
    for name in before:
        if name != "claude-sonnet-4-5":
            assert rows[name] == before[name]

    # pricing.json on disk: entry replaced in place, _meta kept + updated stamped
    raw = json.loads(pricing_file.read_text())
    assert raw["models"]["claude-sonnet-4-5"] == new
    assert raw["_meta"]["note"].startswith("Frozen test prices") and raw["_meta"]["updated"] == "2026-11-26"
    assert list(raw["models"]).index("claude-sonnet-4-5") == 0  # original position

    # every sonnet event is repriced at the new rate; other models are untouched
    sonnet = _events(ledger, "claude-sonnet-4-5%")
    assert len(sonnet) == 2  # fixture ledger: one bare + one dated sonnet response after dedup
    for row in sonnet:
        assert row["cost_usd"] == pytest.approx(_expected_cost(row, new))
    opus_price = {**before["claude-opus-4-5"]}
    for row in _events(ledger, "claude-opus-4-5%"):
        assert row["cost_usd"] == pytest.approx(_expected_cost(row, opus_price))

    new_total = client.get("/api/overview", params={"range": "lifelong"}).json()["totals"]["cost_usd"]
    assert new_total == pytest.approx(body["reprice"]["cost_after"])
    assert old_total == pytest.approx(body["reprice"]["cost_before"])
    # the full reprice also prices the Antigravity gemini-4-neon events for the first time
    gemini = sum(r["cost_usd"] or 0 for r in _events(ledger, "gemini-4-neon%"))
    assert gemini > 0
    sonnet_new = sum(r["cost_usd"] for r in sonnet)
    assert sonnet_new < sum(_expected_cost(r, before["claude-sonnet-4-5"]) for r in sonnet)


def test_add_model_appears_in_its_group_and_prices_ledger(client, ledger):
    r = client.post("/api/pricing", json={"model": "fictional-model-x1", "input": 9.0, "output": 9.0,
                                          "cache_read": 0.0, "cache_write": 0.0})
    assert r.status_code == 200 and r.json()["replaced"] is False
    body = client.get("/api/pricing").json()
    assert body["count"] == 10
    assert body["providers"][-1] == {"provider": "Other", "count": 2}
    assert [m["model"] for m in body["models"] if m["provider"] == "Other"] == ["deepseek-v4", "fictional-model-x1"]
    (row,) = _events(ledger, "fictional-model-x1")
    assert row["cost_usd"] == pytest.approx(_expected_cost(row, {**dict.fromkeys(FIELDS, 0.0), "input": 9.0,
                                                                  "output": 9.0, "thinking": 9.0}))


def test_edit_via_provider_prefixed_name_replaces_the_same_row(client):
    r = client.post("/api/pricing", json={"model": "anthropic/claude-haiku-4-5", "input": 0.5, "output": 2.5,
                                          "cache_read": 0.05, "cache_write": 0.6})
    assert r.status_code == 200 and r.json()["replaced"] is True
    body = client.get("/api/pricing").json()
    assert body["count"] == 9
    haiku = [m for m in body["models"] if m["model"] == "claude-haiku-4-5"]
    assert len(haiku) == 1 and haiku[0]["input"] == 0.5


def test_rejected_edit_leaves_table_unchanged(client, pricing_file):
    before = (client.get("/api/pricing").json(), _digest(pricing_file))
    r = client.post("/api/pricing", json={"model": "claude-sonnet-4-5", "input": -1, "output": 1,
                                          "cache_read": 0, "cache_write": 0})
    assert r.status_code == 422
    assert (client.get("/api/pricing").json(), _digest(pricing_file)) == before


# ------------------------------------------------------------------ set_model_price(updated=...)

def test_set_model_price_stamps_updated(pricing_file):
    set_model_price(pricing_file, "gpt-6-sol", {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0},
                    updated="2026-12-01")
    raw = json.loads(pricing_file.read_text())
    assert raw["_meta"]["updated"] == "2026-12-01" and raw["_meta"]["currency"] == "USD"


def test_set_model_price_creates_meta_first_when_missing(tmp_path):
    p = tmp_path / "pricing.json"
    p.write_text(json.dumps({"models": {}}))
    set_model_price(p, "gpt-6-sol", {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0},
                    updated="2026-12-01")
    raw = json.loads(p.read_text())
    assert list(raw) == ["_meta", "models"] and raw["_meta"] == {"updated": "2026-12-01"}


@pytest.mark.parametrize("bad", ["12/01/2026", "2026-12-1", "", "2026-12-01T00:00"])
def test_set_model_price_rejects_bad_updated(pricing_file, bad):
    before = _digest(pricing_file)
    with pytest.raises(ValueError, match="ISO date"):
        set_model_price(pricing_file, "gpt-6-sol",
                        {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0}, updated=bad)
    assert _digest(pricing_file) == before


def test_set_model_price_without_updated_keeps_meta(pricing_file):
    meta = json.loads(pricing_file.read_text())["_meta"]
    set_model_price(pricing_file, "gpt-6-sol", {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0})
    assert json.loads(pricing_file.read_text())["_meta"] == meta


# ------------------------------------------------------------------ frontend wiring

HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_pricing_button_sits_with_the_exports():
    exports = HTML[HTML.index('<div class="exports">'):]
    exports = exports[:exports.index("</div>")]
    assert 'id="open-pricing"' in exports and ">Pricing</button>" in exports
    assert exports.index('id="open-pricing"') < exports.index('id="export-csv"')


def test_pricing_modal_markup():
    dlg = HTML[HTML.index('<dialog class="modal modal-wide" id="pricing-dialog"'):]
    dlg = dlg[:dlg.index("</dialog>")]
    heads = re.findall(r'<th scope="col"[^>]*>([^<]+)</th>', dlg)
    assert heads == ["Model", "Input", "Output", "Thinking", "Cache Read", "Cache Write", "Action"]
    assert re.findall(r'data-provider="([^"]*)"', dlg) == ["", "Claude", "Gemini", "GPT", "Other"]
    assert ">All<" in dlg and 'type="search" id="pricing-search"' in dlg
    assert ">+ Add Model</button>" in dlg and "USD per 1M tokens" in dlg
    assert 'id="pricing-close"' in dlg  # Esc is handled natively by <dialog>
    assert 'id="price-model-input"' in HTML


def test_new_markup_is_csp_clean():
    for bad in ("<script>", " onclick=", " onkeydown=", " oninput=", " style=", "http://", "https://"):
        assert bad not in HTML, bad
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "url(" not in css and "@import" not in css
    assert "font-variant-numeric: tabular-nums" in css[css.index("table.pricing-table"):][:200]


def test_js_wiring():
    assert 'api("/api/pricing")' in JS
    assert 'openPriceDialog(m.model, null, { mode: "edit", prefill: m })' in JS
    assert 'openPriceDialog("", null, { mode: "add" })' in JS
    assert "loadPricing();" in JS[JS.index("async function savePrice"):]  # table refresh after save
    assert "fetch(" in JS and "http" not in re.sub(r'"http://www\.w3\.org/2000/svg"', "", JS)


@requires_jsc
def test_app_js_syntax_jsc():
    script = 'try { new Function(readFile(arguments[0])); print("SYNTAX_OK"); } catch (e) { print("SYNTAX_ERROR " + e); }'
    out = subprocess.run([str(JSC), "-e", script, "--", str(STATIC_DIR / "app.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "SYNTAX_OK", out.stdout + out.stderr


@requires_jsc
def test_pricing_modal_runtime_jsc(tmp_path):
    """Run the real app.js against a stub DOM + a real /api/pricing response and click through it."""
    p = tmp_path / "pricing.json"
    shutil.copy(TEST_PRICING, p)
    for name, price in [("gemini-4-neon", EXTRA["gemini-4-neon"]),
                        ("gpt-6-luna", {"input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.0}),
                        ("deepseek-v4", EXTRA["deepseek-v4"])]:
        set_model_price(p, name, price, updated="2026-11-26")
    resp = tmp_path / "pricing_resp.json"
    resp.write_text(client_for(tmp_path / "none.db", p).get("/api/pricing").text)
    out = subprocess.run([str(JSC), str(STUB_DOM), str(SMOKE), "--", str(STATIC_DIR / "app.js"), str(resp)],
                         capture_output=True, text=True, timeout=60)
    lines = out.stdout.strip().splitlines()
    assert lines and lines[-1] == "SMOKE_OK", out.stdout + out.stderr
    assert not [ln for ln in lines if ln.startswith("FAIL")]
    assert sum(ln.startswith("PASS") for ln in lines) >= 30
