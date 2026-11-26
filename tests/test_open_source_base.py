"""Open-source base upgrades: cross-platform schedules, API vs subscription billing,
pricing `used` flags and dated (`effective_from`) prices.

Every config, log and ledger here is fabricated under pytest's workspace-local tmp
dir; the real sources.json, subscriptions.json, pricing.json and data/ are never read,
and no scheduler command (launchctl / systemctl / crontab / PowerShell) is ever run.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from _factory import assistant_line, write
from conftest import JSC, TEST_PRICING, requires_jsc
from lifelong_tracker import cli
from lifelong_tracker import schedule as sched
from lifelong_tracker import sources as sources_mod
from lifelong_tracker.analytics import cache_savings
from lifelong_tracker.config import PROJECT_ROOT
from lifelong_tracker.models import UsageEvent
from lifelong_tracker.pricing import PricingTable, price_on, set_model_price
from lifelong_tracker.storage import connect, pricing_drift, reprice
from lifelong_tracker.storage.db import upsert_events
from lifelong_tracker.subscriptions import (
    DAYS_PER_MONTH,
    api_spend,
    billing_rules,
    subscription_roi,
)
from lifelong_tracker.web.app import create_app

TODAY = date(2026, 11, 26)
BASE = "http://127.0.0.1:8765"
runner = CliRunner()


# =========================================================================== 1. schedules

@pytest.mark.parametrize("raw, expected", [
    ("darwin", "macos"), ("linux", "linux"), ("linux2", "linux"), ("freebsd14", "linux"),
    ("win32", "windows"), ("cygwin", "windows"), ("msys", "windows"),
])
def test_detect_platform(raw, expected):
    assert sched.detect_platform(raw) == expected


def test_detect_platform_defaults_to_this_machine():
    assert sched.detect_platform() in sched.PLATFORMS


def test_default_tracker_bin_and_out(tmp_path):
    assert sched.default_tracker_bin(tmp_path, "windows") == tmp_path / ".venv" / "Scripts" / "tracker.exe"
    assert sched.default_tracker_bin(tmp_path, "linux") == tmp_path / ".venv" / "bin" / "tracker"
    assert sched.default_tracker_bin(tmp_path, "macos") == tmp_path / ".venv" / "bin" / "tracker"
    assert [sched.default_out(tmp_path, "job", p).name for p in sched.PLATFORMS] == [
        "job.plist", "job.service", "job.ps1"]


@pytest.fixture
def sched_cfg(tmp_path) -> Path:
    cfg = tmp_path / "cfg" / "sources.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"sources": [{"tool": "claude-code", "path": str(tmp_path / "logs-src")}]}))
    return cfg


def _render(tmp_path, cfg, platform, out, *extra):
    return runner.invoke(cli.app, [
        "schedule", "render", "--config", str(cfg), "--out", str(out),
        "--db", str(tmp_path / "my db" / "tracker.db"), "--pricing", str(TEST_PRICING),
        "--log-file", str(tmp_path / "logs" / "sync.log"), "--backup-dir", str(tmp_path / "bk"),
        "--tracker-bin", str(tmp_path / "venv bin" / "tracker"),
        *(["--platform", platform] if platform else []), *map(str, extra)])


def _spec(tmp_path, **kw) -> sched.ScheduleSpec:
    base = dict(label="com.lifelong-tracker.sync", tracker_bin=tmp_path / "bin" / "tracker",
                project_root=tmp_path, config=tmp_path / "sources.json", db=tmp_path / "d.db",
                pricing=tmp_path / "p.json", log_file=tmp_path / "logs" / "sync.log", hour=6, minute=5)
    base.update(kw)
    return sched.ScheduleSpec(**base)


def test_render_linux_writes_systemd_pair_and_crontab(tmp_path, sched_cfg):
    out = tmp_path / "deploy" / "com.lifelong-tracker.sync.service"
    r = _render(tmp_path, sched_cfg, "linux", out, "--hour", 6, "--minute", 5, "--keep", 9)
    assert r.exit_code == 0, r.output
    service, timer, cron = (out, out.with_name("com.lifelong-tracker.sync.timer"),
                            out.with_name("com.lifelong-tracker.sync.crontab"))
    assert sorted(p.name for p in out.parent.iterdir()) == sorted([service.name, timer.name, cron.name])

    svc = service.read_text()
    assert "[Service]" in svc and "Type=oneshot" in svc
    assert f"WorkingDirectory={PROJECT_ROOT}" in svc
    exec_line = next(ln for ln in svc.splitlines() if ln.startswith("ExecStart="))
    assert exec_line.startswith(f'ExecStart="{tmp_path / "venv bin" / "tracker"}" "sync-all" "--config"')
    assert f'"--db" "{tmp_path / "my db" / "tracker.db"}"' in exec_line  # spaces stay in one argument
    assert '"--keep" "9"' in exec_line and '"--quiet"' in exec_line
    log = tmp_path / "logs" / "sync.log"
    assert f"StandardOutput=append:{log}" in svc and f"StandardError=append:{log}" in svc

    tim = timer.read_text()
    assert "OnCalendar=*-*-* 06:05:00" in tim and "Persistent=true" in tim
    assert "Unit=com.lifelong-tracker.sync.service" in tim and "WantedBy=timers.target" in tim

    line = [ln for ln in cron.read_text().splitlines() if not ln.startswith("#")]
    assert len(line) == 1
    assert line[0].startswith("5 6 * * * cd ")
    assert line[0].endswith(f"2>&1 {sched.CRON_MARKER}com.lifelong-tracker.sync")
    assert shlex.quote(str(tmp_path / "my db" / "tracker.db")) in line[0]

    text = r.output
    assert "Platform: linux" in text and "nothing has been installed" in text
    assert "systemctl --user enable --now com.lifelong-tracker.sync.timer" in text
    assert "systemctl --user disable --now com.lifelong-tracker.sync.timer" in text
    assert f"| crontab -" in text and "grep -vF" in text
    assert "launchctl" not in text and "plist" not in text
    assert log.parent.is_dir()


def test_render_linux_escapes_systemd_and_cron_specials(tmp_path):
    spec = _spec(tmp_path, db=tmp_path / "100%" / "a$b\"c" / "t.db")
    svc = sched.render_systemd_service(spec)
    assert f'"{tmp_path}/100%%/a$$b\\"c/t.db"' in svc  # %% specifier, $$ variable, \" quote
    cron = sched.render_crontab(spec)
    assert "100\\%" in cron and "100%/" not in cron.replace("100\\%", "")


def test_render_linux_adds_suffix_when_out_has_none(tmp_path, sched_cfg):
    r = _render(tmp_path, sched_cfg, "linux", tmp_path / "out" / "nightly")
    assert r.exit_code == 0, r.output
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "nightly.crontab", "nightly.service", "nightly.timer"]
    assert "Unit=nightly.service" in (tmp_path / "out" / "nightly.timer").read_text()
    assert "systemctl --user enable --now nightly.timer" in r.output


def test_render_windows_writes_register_and_unregister_scripts(tmp_path, sched_cfg):
    out = tmp_path / "deploy" / "com.lifelong-tracker.sync.ps1"
    r = _render(tmp_path, sched_cfg, "windows", out, "--hour", 23, "--minute", 0)
    assert r.exit_code == 0, r.output
    uninstall = out.with_name("com.lifelong-tracker.sync.uninstall.ps1")
    for path in (out, uninstall):
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")                       # UTF-8 BOM for PowerShell 5.1
        assert b"\n" not in raw.replace(b"\r\n", b"")                # CRLF only
    ps = out.read_text(encoding="utf-8-sig")
    assert "$ErrorActionPreference = 'Stop'" in ps
    assert "$TaskName = 'com.lifelong-tracker.sync'" in ps
    assert "New-ScheduledTaskTrigger -Daily -At '23:00'" in ps
    assert "New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $Arguments -WorkingDirectory $WorkDir" in ps
    assert "Register-ScheduledTask -TaskName $TaskName" in ps and "-Force" in ps
    args_line = next(ln for ln in ps.splitlines() if ln.startswith("$Arguments = "))
    assert args_line.startswith(f"$Arguments = '/d /s /c \"\"{tmp_path / 'venv bin' / 'tracker'}\" \"sync-all\"")
    assert f'>> "{tmp_path / "logs" / "sync.log"}" 2>&1"\'' in args_line
    un = uninstall.read_text(encoding="utf-8-sig")
    assert "Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false" in un
    assert "Register-ScheduledTask" not in un.replace("Unregister-ScheduledTask", "")

    text = r.output
    assert "Platform: windows" in text and "Run these yourself in PowerShell" in text
    assert f'powershell -NoProfile -ExecutionPolicy Bypass -File "{out}"' in text
    assert f'powershell -NoProfile -ExecutionPolicy Bypass -File "{uninstall}"' in text
    assert "Start-ScheduledTask -TaskName 'com.lifelong-tracker.sync'" in text
    assert "launchctl" not in text and "systemctl" not in text


def test_render_windows_rejects_cmd_unsafe_paths(tmp_path, sched_cfg):
    out = tmp_path / "w" / "job.ps1"
    r = runner.invoke(cli.app, ["schedule", "render", "--platform", "windows", "--config", str(sched_cfg),
                                "--out", str(out), "--log-file", str(tmp_path / "100%" / "sync.log"),
                                "--tracker-bin", str(tmp_path / "t.exe"), "--no-backup"])
    assert r.exit_code == 2 and "%" in r.output
    assert not out.parent.exists() and not (tmp_path / "100%").exists()


@pytest.mark.parametrize("value, quoted", [
    ("C:\\Users\\me\\tracker.exe", "'C:\\Users\\me\\tracker.exe'"),
    ("O'Brien", "'O''Brien'"),
    ("a\u2019b", "'a\u2019\u2019b'"),       # PowerShell treats curly quotes as quotes too
    ("$env:HOME `x", "'$env:HOME `x'"),    # single quotes: no expansion, no escapes
])
def test_ps_quote(value, quoted):
    assert sched.ps_quote(value) == quoted


def test_render_auto_detects_platform(tmp_path, sched_cfg, monkeypatch):
    monkeypatch.setattr(sched, "detect_platform", lambda *a: "windows")
    r = runner.invoke(cli.app, ["schedule", "render", "--config", str(sched_cfg),
                                "--out", str(tmp_path / "auto" / "job"),
                                "--log-file", str(tmp_path / "l" / "sync.log"), "--no-backup"])
    assert r.exit_code == 0, r.output
    assert sorted(p.name for p in (tmp_path / "auto").iterdir()) == ["job.ps1", "job.uninstall.ps1"]
    ps = (tmp_path / "auto" / "job.ps1").read_text(encoding="utf-8-sig")
    assert str(PROJECT_ROOT / ".venv" / "Scripts" / "tracker.exe") in ps  # Windows venv layout
    assert "--backup-dir" not in ps


def test_render_rejects_unknown_platform(tmp_path, sched_cfg):
    out = tmp_path / "x.plist"
    r = _render(tmp_path, sched_cfg, "solaris", out)
    assert r.exit_code == 2 and not out.exists()


@pytest.mark.parametrize("platform", ["linux", "windows"])
@pytest.mark.parametrize("extra", [["--hour", "24"], ["--minute", "60"], ["--label", "bad label; rm -rf ~"]])
def test_render_rejects_bad_input_on_every_platform(tmp_path, sched_cfg, platform, extra):
    out = tmp_path / "o" / "job"
    r = _render(tmp_path, sched_cfg, platform, out, *extra)
    assert r.exit_code == 2
    assert not out.parent.exists()


def test_renderers_validate_spec(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        sched.render_systemd_service(_spec(tmp_path, tracker_bin=Path("tracker")))
    with pytest.raises(ValueError, match="hour"):
        sched.render_crontab(_spec(tmp_path, hour=24))
    with pytest.raises(ValueError, match="line breaks"):
        sched.render_windows_install(_spec(tmp_path, db=tmp_path / "a\nb.db"))
    with pytest.raises(ValueError, match="unknown platform"):
        sched.render(_spec(tmp_path), "beos", tmp_path / "x")


def test_render_macos_unchanged(tmp_path):
    files = sched.render(_spec(tmp_path), "macos", tmp_path / "a.plist")
    assert [p.name for p, _ in files] == ["a.plist"] and files[0][1] == sched.render_plist(_spec(tmp_path))
    cmds = sched.commands_for("macos", [tmp_path / "a.plist"], _spec(tmp_path))
    assert any("launchctl bootstrap" in c for c in cmds["Install"])


# =========================================================================== 2. API vs subscription

def test_sources_billing_field(tmp_path):
    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({"sources": [
        {"name": "plan", "tool": "claude-code", "path": "a"},
        {"name": "key", "tool": "claude-code", "path": "b", "billing": "api"},
        {"name": "plan2", "tool": "codex", "path": "c", "billing": "subscription"},
    ]}))
    specs = sources_mod.load_sources(cfg)
    assert [s.billing for s in specs] == ["subscription", "api", "subscription"]


@pytest.mark.parametrize("bad", ["API", "metered", "", None, 1, True])
def test_sources_billing_rejects_unknown_values(tmp_path, bad):
    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps([{"tool": "claude-code", "path": "a", "billing": bad}]))
    with pytest.raises(sources_mod.SourcesConfigError, match="billing"):
        sources_mod.load_sources(cfg)


def test_billing_rules_empty_without_api_sources(tmp_path):
    specs = [sources_mod.SourceSpec("a", "claude-code", tmp_path / "a"),
             sources_mod.SourceSpec("b", "codex", tmp_path / "b", billing="subscription")]
    assert billing_rules(specs) == ()
    rules = billing_rules([*specs, sources_mod.SourceSpec("k", "claude-code", tmp_path / "a" / "key", billing="api")])
    assert [r.billing for r in rules][0] == "api"  # longest (nested) path first
    assert len(rules) == 3


HAIKU = "claude-haiku-4-5"


def _haiku(msg, ts, inp):
    return assistant_line(msg, req_id=f"req-{msg}", ts=ts, model=HAIKU, inp=inp, out=0)


@pytest.fixture
def billing_world(tmp_path):
    """Two Claude Code folders: one on a plan, one on an API key, with a plan folder nested in it."""
    plan = tmp_path / "logs" / "plan"
    key = tmp_path / "logs" / "api-key"
    write(plan / "p1" / "s.jsonl", _haiku("p1", "2026-11-10T12:00:00.000Z", 6_000_000),
          _haiku("p2", "2026-11-20T12:00:00.000Z", 4_000_000))
    write(key / "k1" / "s.jsonl", _haiku("k1", "2026-11-15T12:00:00.000Z", 3_000_000),
          _haiku("k0", "2026-10-01T12:00:00.000Z", 2_000_000))
    # a sub-folder of the API folder that is itself declared as plan usage (longest path wins)
    write(key / "team-plan" / "s.jsonl", _haiku("t1", "2026-11-18T12:00:00.000Z", 1_000_000))

    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({"sources": [
        {"name": "plan", "tool": "claude-code", "path": "logs/plan"},
        {"name": "key", "tool": "claude-code", "path": "logs/api-key", "billing": "api"},
        {"name": "team", "tool": "claude-code", "path": "logs/api-key/team-plan", "billing": "subscription"},
    ]}))
    subs = tmp_path / "subscriptions.json"
    subs.write_text(json.dumps({"subscriptions": {"claude-code": [
        {"start": "2026-11-01", "end": None, "monthly_usd": 20.0}]}}))
    db = tmp_path / "ledger" / "tracker.db"
    pricing = PricingTable.load(TEST_PRICING)
    with connect(db) as conn:
        results = sources_mod.sync_all(conn, sources_mod.load_sources(cfg), pricing)
    assert all(r.ok for r in results), [r.error for r in results]
    return {"db": db, "cfg": cfg, "subs": subs, "tmp": tmp_path}


def _client(w, *, sources=True, subs=True) -> TestClient:
    app = create_app(w["db"], TEST_PRICING, today=lambda: TODAY,
                     subscriptions_path=w["subs"] if subs else None,
                     sources_path=w["cfg"] if sources else None)
    return TestClient(app, base_url=BASE)


def test_api_spend_attribution(billing_world):
    specs = sources_mod.load_sources(billing_world["cfg"])
    rules = billing_rules(specs)
    with connect(billing_world["db"]) as conn:
        everything = api_spend(conn, rules, None, TODAY)
        recent = api_spend(conn, rules, date(2026, 10, 28), TODAY)
        none = api_spend(conn, rules, None, TODAY, tool="codex")
        nothing = api_spend(conn, (), None, TODAY)
    assert everything.usd == pytest.approx(5.0) and everything.events == 2  # nested team-plan excluded
    assert everything.tokens == 5_000_000 and everything.unpriced_events == 0
    assert [(t.tool, t.usd) for t in everything.tools] == [("claude-code", pytest.approx(5.0))]
    assert recent.usd == pytest.approx(3.0) and recent.events == 1
    assert none.usd == 0 and none.events == 0 and none.tools == []
    assert nothing.usd == 0 and nothing.tools == []


def test_roi_excludes_api_usage(billing_world):
    specs = sources_mod.load_sources(billing_world["cfg"])
    plans = {"claude-code": [{"start": "2026-11-01", "end": None, "monthly_usd": 20.0}]}
    with connect(billing_world["db"]) as conn:
        roi = subscription_roi(conn, plans, "30d", TODAY, sources=specs)
        legacy = subscription_roi(conn, plans, "30d", TODAY)
    plan = 26 / DAYS_PER_MONTH * 20.0  # Nov 1..26
    assert roi.plan_cost_usd == pytest.approx(plan)
    assert roi.value_usd == pytest.approx(11.0)          # 6 + 4 + nested team plan 1
    assert roi.multiple == pytest.approx(11.0 / plan)
    assert roi.api_direct_spend_usd == pytest.approx(3.0)
    assert roi.tools[0].api_spend_usd == pytest.approx(3.0) and roi.tools[0].value_usd == pytest.approx(11.0)
    # without billing information everything is plan usage (the old behaviour)
    assert legacy.value_usd == pytest.approx(14.0) and legacy.api_direct_spend_usd == 0.0


def test_overview_billing_split(billing_world):
    c = _client(billing_world)
    ov = c.get("/api/overview", params={"range": "30d"}).json()
    assert ov["totals"]["cost_usd"] == pytest.approx(14.0)   # the KPI still shows all usage
    sub = ov["subscription"]
    assert sub["value_usd"] == pytest.approx(11.0) and sub["api_direct_spend_usd"] == pytest.approx(3.0)
    assert sub["tools"][0]["api_spend_usd"] == pytest.approx(3.0)
    b = ov["billing"]
    assert b["api_sources"] == 1 and b["basis"] == "tool"
    assert b["api_direct_spend_usd"] == pytest.approx(3.0) and b["api_events"] == 1
    assert b["api_tokens"] == 3_000_000 and b["api_unpriced_events"] == 0
    assert b["subscription_value_usd"] == pytest.approx(11.0)
    assert b["tools"] == [{"tool": "claude-code", "label": "Claude Code", "api_spend_usd": pytest.approx(3.0),
                           "events": 1, "unpriced_events": 0, "tokens": 3_000_000}]

    life = c.get("/api/overview", params={"range": "lifelong"}).json()
    assert life["billing"]["api_direct_spend_usd"] == pytest.approx(5.0)
    assert life["subscription"]["value_usd"] == pytest.approx(11.0)

    other = c.get("/api/overview", params={"range": "30d", "tool": "codex"}).json()
    assert other["billing"]["api_direct_spend_usd"] == 0 and other["billing"]["tools"] == []
    custom = c.get("/api/overview", params={"range": "custom", "start": "2026-11-15",
                                            "end": "2026-11-15"}).json()
    assert custom["billing"]["api_direct_spend_usd"] == pytest.approx(3.0)
    assert custom["subscription"]["value_usd"] == pytest.approx(0.0)


def test_overview_api_only_without_subscriptions(billing_world):
    ov = _client(billing_world, subs=False).get("/api/overview", params={"range": "30d"}).json()
    assert ov["subscription"] is None
    assert ov["billing"]["api_direct_spend_usd"] == pytest.approx(3.0)
    assert ov["billing"]["subscription_value_usd"] is None


def test_overview_without_api_sources_is_unchanged(billing_world):
    cfg = billing_world["cfg"]
    raw = json.loads(cfg.read_text())
    for s in raw["sources"]:
        s.pop("billing", None)
    cfg.write_text(json.dumps(raw))
    ov = _client(billing_world).get("/api/overview", params={"range": "30d"}).json()
    assert ov["billing"] is None
    assert ov["subscription"]["value_usd"] == pytest.approx(14.0)
    assert ov["subscription"]["api_direct_spend_usd"] == 0.0
    # no sources file at all, or a broken one: same as "no API sources"
    assert _client(billing_world, sources=False).get("/api/overview").json()["billing"] is None
    cfg.write_text("{ broken")
    assert _client(billing_world).get("/api/overview").json()["billing"] is None


def test_billing_never_leaks_into_card_or_export(billing_world):
    c = _client(billing_world)
    card = c.get("/api/card", params={"range": "30d"}).text
    exported = c.get("/api/export", params={"range": "30d", "format": "json"}).text
    for text in (card, exported):
        assert "api-key" not in text and "Direct API" not in text and "billing" not in text
        assert str(billing_world["tmp"]) not in text


# =========================================================================== 3a. pricing `used`

def test_pricing_used_flags(tmp_path):
    db = tmp_path / "ledger" / "t.db"
    pricing = PricingTable.load(TEST_PRICING)
    write(tmp_path / "src" / "p" / "s.jsonl",
          assistant_line("u1", req_id="r1", model="claude-sonnet-4-5-20250929"),
          assistant_line("u2", req_id="r2", model="claude-haiku-4-5-high"),   # tier variant -> base row
          assistant_line("u3", req_id="r3", model="mystery-model-9"))          # unpriced: no row
    with connect(db) as conn:
        sources_mod.sync_one(conn, sources_mod.SourceSpec("c", "claude-code", tmp_path / "src"), pricing)
    body = TestClient(create_app(db, TEST_PRICING, today=lambda: TODAY), base_url=BASE).get("/api/pricing").json()
    used = {m["model"]: m["used"] for m in body["models"]}
    assert used == {"claude-haiku-4-5": True, "claude-opus-4": False, "claude-opus-4-5": False,
                    "claude-sonnet-4-5": True}
    assert body["used_count"] == 2 and body["count"] == 4

    empty = TestClient(create_app(tmp_path / "none.db", TEST_PRICING, today=lambda: TODAY),
                       base_url=BASE).get("/api/pricing").json()
    assert empty["used_count"] == 0 and not any(m["used"] for m in empty["models"])
    assert not (tmp_path / "none.db").exists()


STATIC = Path(__file__).resolve().parents[1] / "src" / "lifelong_tracker" / "web" / "static"


def test_used_only_markup_and_wiring():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    toolbar = html[html.index('<div class="pricing-toolbar">'):]
    toolbar = toolbar[:toolbar.index('id="pricing-add"')]
    assert '<input type="checkbox" id="pricing-used" role="switch" checked>' in toolbar
    assert "<span>Used only</span>" in toolbar
    assert 'id="kpi-roi-split"' in html
    assert "usedOnly: true" in js and '$("pricing-used").addEventListener("change"' in js
    assert "Subscription: ${fmtUsd(subValue)} · Direct API: ${fmtUsd(billing.api_direct_spend_usd)}" in js
    assert "renderRoi(ov.subscription, ov.subscription_error, ov.billing)" in js
    for bad in (" style=", "http://", "https://", " onchange="):
        assert bad not in html


# =========================================================================== 3b. effective_from

def _seg(eff, inp, **kw):
    seg = {"input": inp, "output": kw.get("output", 0.0), "cache_read": kw.get("cache_read", 0.0),
           "cache_write": 0.0}
    if eff is not None:
        seg["effective_from"] = eff
    return seg


DATED = {"models": {
    HAIKU: [_seg("2026-01-01", 2.0, cache_read=0.5), _seg("2026-12-01", 4.0, cache_read=1.0)],
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
}}


def test_dated_entry_parses_and_picks_segment_by_day():
    t = PricingTable.from_dict(DATED)
    assert t.has_dated_prices and len(t) == 2
    segs = t.segments(HAIKU)
    assert [s.effective_from for s in segs] == [date(2026, 1, 1), date(2026, 12, 1)]
    assert t.lookup(HAIKU)[1].input == 4.0                        # no date -> latest
    assert t.lookup_at(HAIKU, None)[1].input == 4.0
    assert t.lookup_at(HAIKU, "2025-06-01")[1].input == 2.0       # before the first date -> first price
    assert t.lookup_at(HAIKU, "2026-11-30")[1].input == 2.0
    assert t.lookup_at(HAIKU, date(2026, 12, 1))[1].input == 4.0  # the change day itself
    assert t.lookup_at("claude-haiku-4-5-20251001", "2026-12-02") == (HAIKU, t.segments(HAIKU)[1].price)
    assert t.lookup_at("claude-haiku-4-5-xhigh", "2026-06-01")[1].input == 2.0  # tier variant inherits
    assert t.lookup_at("claude-sonnet-4-5", "1999-01-01")[1].input == 3.0       # plain entry: any date
    assert t.lookup_at("nope", "2026-01-01") is None
    assert [m for _, m, _ in t.entries()] == [HAIKU, "claude-sonnet-4-5"]


def test_dated_cost_for_and_price_on():
    t = PricingTable.from_dict(DATED)
    assert t.cost_for(HAIKU, day="2026-11-30", input_tokens=1_000_000) == pytest.approx(2.0)
    assert t.cost_for(HAIKU, day="2026-12-01", input_tokens=1_000_000) == pytest.approx(4.0)
    assert t.cost_for(HAIKU, input_tokens=1_000_000) == pytest.approx(4.0)
    assert price_on(t.segments(HAIKU), "2026-12-01T00:00:00.000Z").input == 4.0  # ISO timestamps work too


def test_first_segment_may_omit_date_and_object_may_carry_one():
    t = PricingTable.from_dict({"models": {
        "a": [_seg(None, 1.0), _seg("2026-03-01", 2.0)],
        "b": {**_seg("2026-05-01", 7.0)},
    }})
    assert t.lookup_at("a", "2000-01-01")[1].input == 1.0 and t.lookup_at("a", "2026-03-01")[1].input == 2.0
    assert t.lookup_at("b", "2001-01-01")[1].input == 7.0 and not PricingTable.from_dict(
        {"models": {"b": _seg("2026-05-01", 7.0)}}).has_dated_prices


@pytest.mark.parametrize("entry, match", [
    ([], "empty price list"),
    ([_seg("2026-01-01", 1.0), _seg(None, 2.0)], "needs an effective_from"),
    ([_seg("2026-02-01", 1.0), _seg("2026-02-01", 2.0)], "strictly increasing"),
    ([_seg("2026-03-01", 1.0), _seg("2026-02-01", 2.0)], "strictly increasing"),
    ([_seg("2026-02-30", 1.0)], "not a real calendar date"),
    ([_seg("2026/02/01", 1.0)], "YYYY-MM-DD"),
    ([{"effective_from": 20260101, "input": 1, "output": 1, "cache_read": 0, "cache_write": 0}], "YYYY-MM-DD"),
    ([_seg("2026-01-01", -1.0)], ">= 0"),
    ([{"effective_from": "2026-01-01", "input": 1}], "missing"),
    (["2026-01-01"], "must be an object"),
    ("cheap", "object or a list"),
    ([_seg("2026-01-01", float("nan"))], "finite"),
])
def test_dated_entry_validation(entry, match):
    with pytest.raises(ValueError, match=match):
        PricingTable.from_dict({"models": {HAIKU: entry}})


def _event(msg, ts, inp=0, cr=0):
    return UsageEvent(timestamp=datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc),
                      tool="claude-code", model=HAIKU, request_id=f"r-{msg}", message_id=msg,
                      input_tokens=inp, cache_read_tokens=cr)


@pytest.fixture
def dated_ledger(tmp_path):
    p = tmp_path / "pricing.json"
    p.write_text(json.dumps(DATED))
    db = tmp_path / "ledger" / "t.db"
    with connect(db) as conn:
        conn.execute("BEGIN")
        upsert_events(conn, [
            _event("old", "2025-06-01T08:00:00.000Z", inp=1_000_000),
            _event("eve", "2026-11-30T23:59:59.999Z", inp=1_000_000),
            _event("new", "2026-12-01T00:00:00.000Z", inp=1_000_000),
            _event("later", "2026-12-24T10:00:00.000Z", inp=500_000),
        ], PricingTable.load(p))
        conn.execute("COMMIT")
    return db, p


def _costs(db):
    with connect(db) as conn:
        return {r["message_id"]: r["cost_usd"] for r in conn.execute("SELECT message_id, cost_usd FROM events")}


def test_ingest_prices_each_event_on_its_utc_day(dated_ledger):
    db, p = dated_ledger
    assert _costs(db) == {"old": pytest.approx(2.0), "eve": pytest.approx(2.0),
                          "new": pytest.approx(4.0), "later": pytest.approx(2.0)}
    with connect(db) as conn:
        assert pricing_drift(conn, PricingTable.load(p)) == []


def test_reprice_and_drift_across_a_price_change(dated_ledger):
    db, p = dated_ledger
    raw = json.loads(p.read_text())
    raw["models"][HAIKU][1]["input"] = 8.0   # the December price changes
    p.write_text(json.dumps(raw))
    table = PricingTable.load(p)
    with connect(db) as conn:
        assert pricing_drift(conn, table) == [HAIKU]
        res = reprice(conn, table)
        assert pricing_drift(conn, table) == []
    assert res.cost_before == pytest.approx(10.0) and res.cost_after == pytest.approx(2 + 2 + 8 + 4)
    assert _costs(db) == {"old": pytest.approx(2.0), "eve": pytest.approx(2.0),
                          "new": pytest.approx(8.0), "later": pytest.approx(4.0)}

    raw["models"][HAIKU] = {"input": 1.0, "output": 0, "cache_read": 0, "cache_write": 0}  # back to one price
    p.write_text(json.dumps(raw))
    flat = PricingTable.load(p)
    with connect(db) as conn:
        assert pricing_drift(conn, flat) == [HAIKU]
        reprice(conn, flat)
    assert sum(_costs(db).values()) == pytest.approx(3.5)


def test_cache_savings_uses_the_dated_rates(tmp_path):
    p = tmp_path / "pricing.json"
    p.write_text(json.dumps(DATED))
    table = PricingTable.load(p)
    with connect(tmp_path / "t.db") as conn:
        conn.execute("BEGIN")
        upsert_events(conn, [_event("a", "2026-11-20T10:00:00.000Z", cr=1_000_000),
                             _event("b", "2026-12-02T10:00:00.000Z", cr=1_000_000)], table)
        conn.execute("COMMIT")
        saved = cache_savings(conn, table, "lifelong", date(2026, 12, 31))
    # (2.0 - 0.5) before the change + (4.0 - 1.0) after it
    assert saved.usd == pytest.approx(1.5 + 3.0) and saved.cache_read_tokens == 2_000_000 and saved.events == 2


def test_set_model_price_edits_only_the_latest_segment(tmp_path):
    p = tmp_path / "pricing.json"
    p.write_text(json.dumps(DATED))
    upd = set_model_price(p, HAIKU, {"input": 5.0, "output": 1.0, "cache_read": 0.2, "cache_write": 0.0})
    assert upd.replaced is True
    entry = json.loads(p.read_text())["models"][HAIKU]
    assert entry[0] == DATED["models"][HAIKU][0]
    assert entry[1] == {"effective_from": "2026-12-01", "input": 5.0, "output": 1.0, "cache_read": 0.2,
                        "cache_write": 0.0}
    assert upd.table.lookup_at(HAIKU, "2026-06-01")[1].input == 2.0
    assert upd.table.lookup(HAIKU)[1].input == 5.0


def test_web_pricing_lists_dated_history_and_edit_keeps_it(dated_ledger):
    db, p = dated_ledger
    c = TestClient(create_app(db, p, today=lambda: TODAY), base_url=BASE)
    rows = {m["model"]: m for m in c.get("/api/pricing").json()["models"]}
    h = rows[HAIKU]
    assert (h["input"], h["effective_from"], h["used"]) == (4.0, "2026-12-01", True)
    assert h["history"] == [{"effective_from": "2026-01-01", "input": 2.0, "output": 0.0, "thinking": 0.0,
                             "cache_read": 0.5, "cache_write": 0.0}]
    assert rows["claude-sonnet-4-5"]["effective_from"] is None and rows["claude-sonnet-4-5"]["used"] is False

    r = c.post("/api/pricing", json={"model": HAIKU, "input": 6.0, "output": 0.0, "cache_read": 1.0,
                                     "cache_write": 0.0})
    assert r.status_code == 200, r.text
    assert r.json()["reprice"]["cost_after"] == pytest.approx(2 + 2 + 6 + 3)
    assert _costs(db)["old"] == pytest.approx(2.0) and _costs(db)["new"] == pytest.approx(6.0)
    after = {m["model"]: m for m in c.get("/api/pricing").json()["models"]}[HAIKU]
    assert after["effective_from"] == "2026-12-01" and len(after["history"]) == 1
    assert c.get("/api/overview").json()["alerts"]["needs_reprice"] is False


def test_plain_pricing_file_round_trips_unchanged(tmp_path):
    """Backward compatibility: a classic pricing.json prices exactly as before."""
    t = PricingTable.load(TEST_PRICING)
    assert not t.has_dated_prices
    assert t.cost_for("claude-sonnet-4-5", day="2001-01-01", input_tokens=1_000_000, output_tokens=1_000_000) == \
        t.cost_for("claude-sonnet-4-5", input_tokens=1_000_000, output_tokens=1_000_000) == pytest.approx(18.0)


# =========================================================================== runtime (JavaScriptCore)

JS = Path(__file__).parent / "fixtures" / "js"


@requires_jsc
@pytest.mark.parametrize("mode", ["split", "apionly", "none"])
def test_roi_card_billing_split_runtime_jsc(billing_world, tmp_path, mode):
    q = {"range": "30d"}
    if mode == "none":
        raw = json.loads(billing_world["cfg"].read_text())
        for s in raw["sources"]:
            s.pop("billing", None)
        billing_world["cfg"].write_text(json.dumps(raw))
    c = _client(billing_world, subs=mode != "apionly")
    rec = tmp_path / "recorded_billing.json"
    rec.write_text(json.dumps({  # real responses for everything the dashboard loads on boot
        "overview": c.get("/api/overview", params=q).json(),
        "tools": c.get("/api/tools").json(),
        "timeseries": c.get("/api/timeseries", params=q).json(),
        "breakdown": c.get("/api/breakdown", params={**q, "group_variants": "true"}).json(),
        "hourly": c.get("/api/hourly", params=q).json(),
        "heatmap": c.get("/api/heatmap").json(),
    }))
    out = subprocess.run([str(JSC), str(JS / "stub_dom.js"), str(JS / "billing_smoke.js"), "--",
                          str(STATIC / "app.js"), str(rec), mode, "#range=30d"], capture_output=True, text=True, timeout=60)
    lines = out.stdout.strip().splitlines()
    assert lines and lines[-1] == "SMOKE_OK", out.stdout + out.stderr
    assert sum(ln.startswith("PASS") for ln in lines) >= 4
