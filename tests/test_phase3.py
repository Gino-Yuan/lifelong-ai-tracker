"""Phase 3: sync-all, online backup + rotation, launchd plist rendering, SVG report card.

Everything runs on fabricated fixtures inside pytest's workspace-local tmp dir.
No launchctl / crontab / subprocess is ever executed (guarded below), nothing is
written outside tmp_path, and the real data/tracker.db is never opened.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rich.console import Console
from typer.testing import CliRunner

from conftest import CANARY, TEST_PRICING
from lifelong_tracker import cli
from lifelong_tracker.backup import BACKUP_RE, backup_database, list_backups, rotate_backups
from lifelong_tracker.card import (
    build_card_data,
    clean_text,
    fmt_amount,
    fmt_compact,
    fmt_usd,
    model_label,
    render_card,
)
from lifelong_tracker.config import DEFAULT_TRACKER_BIN, PROJECT_ROOT
from lifelong_tracker.models import UsageEvent
from lifelong_tracker.schedule import ScheduleSpec, build_plist, is_privacy_protected
from lifelong_tracker.sources import SourcesConfigError, load_sources, sync_all
from lifelong_tracker.storage import connect, connect_readonly, upsert_events
from lifelong_tracker.sync import sync_source
from lifelong_tracker.web.app import STATIC_DIR, create_app

runner = CliRunner()
LAST_DAY = date(2026, 3, 5)
SVG_NS = "{http://www.w3.org/2000/svg}"
# strings from the fixtures that must never reach a report card
PRIVATE_STRINGS = [
    "/Users", "demo", "Projects", "alpha", "beta", "sess-fx", "req_fx", "msg_", CANARY, "\\",
]


@pytest.fixture(autouse=True)
def _no_external_processes(monkeypatch):
    """Phase 3 must never shell out (launchctl, crontab, open, sudo ...)."""

    def boom(*a, **k):
        raise AssertionError(f"external process spawned: {a!r}")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "system", boom)


@pytest.fixture(autouse=True)
def _wide_plain_console(monkeypatch):
    monkeypatch.setattr(cli, "console", Console(width=400, no_color=True, highlight=False))


def _invoke(*args):
    return runner.invoke(cli.app, [str(a) for a in args], catch_exceptions=False)


def _write_config(path: Path, sources) -> Path:
    path.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    return path


def _count(db: Path) -> int:
    with connect_readonly(db) as c:
        return c.execute("SELECT COUNT(*) FROM events").fetchone()[0]


@pytest.fixture
def ledger(db_path, pricing, claude_source, collector) -> Path:
    with connect(db_path) as conn:
        sync_source(conn, collector, claude_source, pricing)
    return db_path


# =========================================================================== sources config

def test_load_sources_resolves_paths_and_defaults(tmp_path):
    cfg = _write_config(tmp_path / "sources.json", [
        {"tool": "claude-code", "path": "logs/cc"},
        {"name": "abs", "tool": "opencode", "path": str(tmp_path / "oc"), "enabled": False, "_note": "x"},
        {"name": "home", "tool": "t3", "path": "~/somewhere"},
    ])
    specs = load_sources(cfg)
    assert [s.name for s in specs] == ["claude-code", "abs", "home"]
    assert specs[0].path == tmp_path.resolve() / "logs" / "cc" and specs[0].enabled is True
    assert specs[1].enabled is False
    assert specs[2].path.is_absolute() and "~" not in str(specs[2].path)


def test_load_sources_accepts_bare_list(tmp_path):
    cfg = tmp_path / "s.json"
    cfg.write_text(json.dumps([{"tool": "claude-code", "path": "."}]))
    assert len(load_sources(cfg)) == 1


@pytest.mark.parametrize(
    "content, message",
    [
        ("{not json", "not valid JSON"),
        (json.dumps({"sources": {}}), "expected"),
        (json.dumps({"sources": [{"tool": "claude-code"}]}), "'path' is required"),
        (json.dumps({"sources": [{"path": "x"}]}), "'tool' is required"),
        (json.dumps({"sources": [{"tool": "nope", "path": "x"}]}), "unknown tool"),
        (json.dumps({"sources": [{"tool": "claude-code", "path": "x", "enable": True}]}), "unknown key"),
        (json.dumps({"sources": [{"tool": "claude-code", "path": "x", "enabled": "yes"}]}), "true or false"),
        (json.dumps({"sources": [{"tool": "claude-code", "path": "a"}, {"tool": "claude-code", "path": "b"}]}),
         "duplicate name"),
    ],
)
def test_load_sources_rejects_bad_config(tmp_path, content, message):
    cfg = tmp_path / "bad.json"
    cfg.write_text(content)
    with pytest.raises(SourcesConfigError, match=message):
        load_sources(cfg)


def test_load_sources_missing_file(tmp_path):
    with pytest.raises(SourcesConfigError, match="not found"):
        load_sources(tmp_path / "missing.json")


def test_example_config_is_valid_and_gitignore_covers_phase3():
    specs = load_sources(PROJECT_ROOT / "sources.example.json")
    assert {s.tool for s in specs} == {"claude-code", "antigravity", "opencode", "codex", "t3"}
    ignored = (PROJECT_ROOT / ".gitignore").read_text().split()
    for entry in ("sources.json", "logs/", "backups/", "data/"):
        assert entry in ignored


# =========================================================================== sync-all

def test_sync_all_continues_after_failures(db_path, pricing, claude_source, tmp_path, monkeypatch):
    broken = tmp_path / "broken_src"
    broken.mkdir()
    placeholder_dir = tmp_path / "oc"
    placeholder_dir.mkdir()
    cfg = _write_config(tmp_path / "sources.json", [
        {"name": "missing", "tool": "claude-code", "path": str(tmp_path / "does-not-exist")},
        {"name": "broken", "tool": "claude-code", "path": str(broken)},
        {"name": "placeholder", "tool": "t3", "path": str(placeholder_dir)},
        {"name": "off", "tool": "t3", "path": str(tmp_path / "nowhere"), "enabled": False},
        {"name": "good", "tool": "claude-code", "path": str(claude_source)},
    ])

    import lifelong_tracker.sources as sources_mod

    real = sources_mod.sync_source

    def flaky(conn, collector, source, pricing_):
        if Path(source).name == "broken_src":
            raise RuntimeError("simulated parser crash")
        return real(conn, collector, source, pricing_)

    monkeypatch.setattr(sources_mod, "sync_source", flaky)
    with connect(db_path) as conn:
        results = sync_all(conn, load_sources(cfg), pricing)
        assert not conn.in_transaction
    by = {r.spec.name: r for r in results}
    assert by["missing"].status == "failed" and "does not exist" in by["missing"].error
    assert by["broken"].status == "failed" and "simulated parser crash" in by["broken"].error
    assert by["placeholder"].status == "failed" and "placeholder" in by["placeholder"].error
    assert by["off"].status == "disabled"
    assert by["good"].ok and by["good"].report.upsert.inserted == 5
    assert _count(db_path) == 5


def test_cli_sync_all_exit_codes_and_quiet(db_path, claude_source, tmp_path):
    good = _write_config(tmp_path / "good.json", [{"name": "cc", "tool": "claude-code", "path": str(claude_source)}])
    r = _invoke("sync-all", "--config", good, "--db", db_path, "--pricing", TEST_PRICING, "--quiet")
    assert r.exit_code == 0, r.output
    lines = [ln for ln in r.output.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d sync-all: 1 ok, 0 failed, 0 disabled; 5 new", lines[0])

    # idempotent re-run
    r = _invoke("sync-all", "--config", good, "--db", db_path, "--pricing", TEST_PRICING, "--quiet")
    assert r.exit_code == 0 and "0 new / 0 updated" in r.output
    assert _count(db_path) == 5

    mixed = _write_config(tmp_path / "mixed.json", [
        {"name": "gone", "tool": "claude-code", "path": str(tmp_path / "gone")},
        {"name": "cc", "tool": "claude-code", "path": str(claude_source)},
    ])
    r = _invoke("sync-all", "--config", mixed, "--db", db_path, "--pricing", TEST_PRICING)
    assert r.exit_code == 1
    assert "FAILED gone" in r.output and "ok" in r.output


def test_cli_sync_all_config_error_is_exit_2(db_path, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("[{]")
    r = _invoke("sync-all", "--config", bad, "--db", db_path, "--pricing", TEST_PRICING)
    assert r.exit_code == 2 and "config error" in r.output
    assert not db_path.exists()


def test_cli_sync_all_with_backup(db_path, claude_source, tmp_path):
    cfg = _write_config(tmp_path / "s.json", [{"tool": "claude-code", "path": str(claude_source)}])
    out = tmp_path / "bk"
    r = _invoke("sync-all", "--config", cfg, "--db", db_path, "--pricing", TEST_PRICING,
                "--quiet", "--backup-dir", out, "--keep", 2)
    assert r.exit_code == 0, r.output
    backups = list_backups(out)
    assert len(backups) == 1 and "backup tracker-" in r.output and "integrity ok" in r.output
    assert _count(backups[0]) == 5


# =========================================================================== backup

def test_backup_is_consistent_while_writer_holds_wal(db_path, pricing, claude_source, collector, tmp_path):
    out = tmp_path / "backups"
    with connect(db_path) as conn:  # keep the writer open: data still lives in the -wal file
        sync_source(conn, collector, claude_source, pricing)
        assert Path(str(db_path) + "-wal").exists()
        res = backup_database(db_path, out, keep=5, now=datetime(2026, 3, 5, 23, 30, 0))
    assert res.path.name == "tracker-20260305-233000.db"
    assert res.integrity == "ok" and res.events == 5
    assert not Path(str(res.path) + "-wal").exists() and not Path(str(res.path) + "-shm").exists()
    assert not list(out.glob("*.partial"))

    with sqlite3.connect(res.path) as b:
        assert b.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert b.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        got = b.execute("SELECT tool, request_id, message_id, input_tokens, output_tokens, "
                        "cache_read_tokens, cache_write_tokens, cost_usd FROM events ORDER BY id").fetchall()
    with connect_readonly(db_path) as src:
        want = src.execute("SELECT tool, request_id, message_id, input_tokens, output_tokens, "
                           "cache_read_tokens, cache_write_tokens, cost_usd FROM events ORDER BY id").fetchall()
    assert [tuple(r) for r in got] == [tuple(r) for r in want]


def test_backup_rotation_keeps_newest_and_ignores_other_files(ledger, tmp_path):
    out = tmp_path / "rot"
    out.mkdir()
    (out / "notes.txt").write_text("keep me")
    (out / "tracker-latest.db").write_text("not a backup name")
    for day in range(1, 7):
        backup_database(ledger, out, keep=100, now=datetime(2026, 3, day, 23, 30, 0))
    assert len(list_backups(out)) == 6

    res = backup_database(ledger, out, keep=3, now=datetime(2026, 3, 7, 23, 30, 0))
    names = [p.name for p in list_backups(out)]
    assert names == ["tracker-20260305-233000.db", "tracker-20260306-233000.db", "tracker-20260307-233000.db"]
    assert len(res.removed) == 4 and len(res.kept) == 3
    assert (out / "notes.txt").read_text() == "keep me"
    assert (out / "tracker-latest.db").exists()


def test_backup_same_second_gets_suffix(ledger, tmp_path):
    out = tmp_path / "same"
    t = datetime(2026, 3, 5, 12, 0, 0)
    a = backup_database(ledger, out, now=t).path
    b = backup_database(ledger, out, now=t).path
    assert a.name == "tracker-20260305-120000.db" and b.name == "tracker-20260305-120000-2.db"
    assert BACKUP_RE.match(b.name)
    assert list_backups(out)[-1] == b
    removed, kept = rotate_backups(out, 1)
    assert kept == [b] and removed == [a]


def test_backup_does_not_modify_source(ledger, tmp_path):
    before = _count(ledger)
    with connect_readonly(ledger) as c:
        sig_before = c.execute("SELECT group_concat(updated_at) FROM events").fetchone()[0]
    backup_database(ledger, tmp_path / "b")
    with connect_readonly(ledger) as c:
        assert c.execute("SELECT group_concat(updated_at) FROM events").fetchone()[0] == sig_before
    assert _count(ledger) == before


def test_backup_errors(tmp_path, ledger):
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / "missing.db", tmp_path / "out")
    with pytest.raises(ValueError):
        backup_database(ledger, tmp_path / "out", keep=0)
    with pytest.raises(ValueError):
        backup_database(ledger, ledger.parent)


def test_cli_backup(ledger, tmp_path):
    out = tmp_path / "cli_bk"
    r = _invoke("backup", "--db", ledger, "--out-dir", out, "--keep", 1)
    assert r.exit_code == 0 and "integrity ok" in r.output
    r = _invoke("backup", "--db", ledger, "--out-dir", out, "--keep", 1)
    assert r.exit_code == 0 and len(list_backups(out)) == 1

    missing = tmp_path / "none.db"
    r = _invoke("backup", "--db", missing, "--out-dir", out)
    assert r.exit_code == 1 and not missing.exists()


# =========================================================================== schedule render

@pytest.fixture
def sched_cfg(tmp_path, claude_source) -> Path:
    return _write_config(tmp_path / "sources.json", [{"tool": "claude-code", "path": str(claude_source)}])


def test_schedule_render_writes_valid_plist_and_prints_commands(tmp_path, sched_cfg, db_path):
    out = tmp_path / "deploy" / "com.lifelong-tracker.sync.plist"
    log = tmp_path / "logs" / "sync.log"
    bk = tmp_path / "backups"
    r = _invoke("schedule", "render", "--config", sched_cfg, "--out", out, "--hour", 6, "--minute", 5,
                "--db", db_path, "--pricing", TEST_PRICING, "--log-file", log, "--backup-dir", bk, "--keep", 7)
    assert r.exit_code == 0, r.output

    raw = out.read_bytes()
    ET.fromstring(raw)  # well-formed XML
    plist = plistlib.loads(raw)
    assert plist["Label"] == "com.lifelong-tracker.sync"
    args = plist["ProgramArguments"]
    assert args[0] == str(DEFAULT_TRACKER_BIN) and args[0].endswith(".venv/bin/tracker")
    assert args[1] == "sync-all"
    assert args[args.index("--config") + 1] == str(sched_cfg.resolve())
    assert args[args.index("--db") + 1] == str(db_path.resolve())
    assert args[args.index("--backup-dir") + 1] == str(bk.resolve())
    assert args[args.index("--keep") + 1] == "7"
    assert "--quiet" in args
    assert all(Path(a).is_absolute() for a in args if "/" in a)
    assert plist["StartCalendarInterval"] == {"Hour": 6, "Minute": 5}
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(log.resolve())
    assert plist["WorkingDirectory"] == str(PROJECT_ROOT)
    assert plist["RunAtLoad"] is False
    assert log.parent.is_dir()  # launchd does not create the log folder

    out_text = r.output
    assert "nothing has been installed" in out_text
    assert f"cp {shlex.quote(str(out))} ~/Library/LaunchAgents/com.lifelong-tracker.sync.plist" in out_text
    assert "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lifelong-tracker.sync.plist" in out_text
    assert "launchctl kickstart -k gui/$(id -u)/com.lifelong-tracker.sync" in out_text
    assert "launchctl bootout gui/$(id -u)/com.lifelong-tracker.sync" in out_text
    assert f"plutil -lint {shlex.quote(str(out))}" in out_text


def test_schedule_render_no_backup_and_defaults(tmp_path, sched_cfg):
    out = tmp_path / "x" / "com.lifelong-tracker.sync.plist"
    r = _invoke("schedule", "render", "--config", sched_cfg, "--out", out, "--no-backup",
                "--log-file", tmp_path / "l" / "sync.log")
    assert r.exit_code == 0, r.output
    plist = plistlib.loads(out.read_bytes())
    assert "--backup-dir" not in plist["ProgramArguments"]
    assert plist["StartCalendarInterval"] == {"Hour": 23, "Minute": 30}


@pytest.mark.parametrize(
    "extra",
    [["--hour", "24"], ["--minute", "60"], ["--label", "bad label; rm -rf ~"]],
)
def test_schedule_render_rejects_bad_input(tmp_path, sched_cfg, extra):
    out = tmp_path / "p.plist"
    r = runner.invoke(cli.app, ["schedule", "render", "--config", str(sched_cfg), "--out", str(out),
                                "--log-file", str(tmp_path / "l.log"), *extra])
    assert r.exit_code == 2
    assert not out.exists()


def test_schedule_render_rejects_invalid_config(tmp_path):
    bad = tmp_path / "s.json"
    bad.write_text(json.dumps({"sources": [{"tool": "claude-code"}]}))
    out = tmp_path / "p.plist"
    r = _invoke("schedule", "render", "--config", bad, "--out", out, "--log-file", tmp_path / "l.log")
    assert r.exit_code == 2 and not out.exists()


def test_build_plist_requires_absolute_paths(tmp_path):
    spec = ScheduleSpec(
        label="x", tracker_bin=Path("tracker"), project_root=tmp_path, config=tmp_path / "s.json",
        db=tmp_path / "d.db", pricing=tmp_path / "p.json", log_file=tmp_path / "l.log",
    )
    with pytest.raises(ValueError, match="absolute"):
        build_plist(spec)


def test_privacy_protected_detection(tmp_path):
    home = tmp_path / "home"
    assert is_privacy_protected(home / "Documents" / "proj", home)
    assert is_privacy_protected(home / "Desktop", home)
    assert not is_privacy_protected(home / "code" / "proj", home)
    assert not is_privacy_protected(tmp_path / "elsewhere", home)


# =========================================================================== report card

def _svg_root(svg: str) -> ET.Element:
    root = ET.fromstring(svg.encode("utf-8"))
    assert root.tag == f"{SVG_NS}svg"
    return root


def _visible_text(root: ET.Element) -> str:
    return " | ".join("".join(el.itertext()) for el in root.iter(f"{SVG_NS}text"))


def _assert_safe_svg(svg: str) -> None:
    low = svg.lower()
    for bad in ("<script", "<style", "style=", "<foreignobject", "<image", "href", "@import", "onload", "javascript:"):
        assert bad not in low, bad
    assert set(re.findall(r"https?://[^\s\"'<>]+", svg)) == {"http://www.w3.org/2000/svg"}


def test_card_content_from_fixtures(ledger):
    with connect_readonly(ledger) as conn:
        data = build_card_data(conn, "lifelong", LAST_DAY)
        svg = render_card(conn, "lifelong", LAST_DAY)
    root = _svg_root(svg)
    assert root.get("width") == "1200" and root.get("height") == "630"
    text = _visible_text(root)
    for expected in (
        "Lifelong AI Usage", "TOTAL COST", "$0.02", "TOTAL TOKENS", "9.33K", "CACHE HIT", "91.2%",
        "LONGEST STREAK", "3 days", "TOKEN MIX", "Input", "Output", "Thinking", "Cache Read",
        "Cache Write", "TOP MODELS", "claude-opus-4-5-20251101", "≈ 55 lines of code written",
        "novels read", "Mar 1 – Mar 5, 2026", "excludes 1 unpriced request",
    ):
        assert expected in text, expected
    assert len(data.top_models) == 3
    assert data.longest_streak == 3 and data.active_days == 4
    _assert_safe_svg(svg)


def test_card_never_contains_paths_or_project_names(ledger):
    with connect_readonly(ledger) as conn:
        projects = [r[0] for r in conn.execute("SELECT DISTINCT project_name FROM events")]
        sessions = [r[0] for r in conn.execute("SELECT DISTINCT session_id FROM events")]
        svgs = [render_card(conn, r, LAST_DAY) for r in ("lifelong", "ytd", "30d", "7d", "today", "90d")]
    assert set(projects) == {"alpha", "beta"}
    for svg in svgs:
        for s in PRIVATE_STRINGS + projects + sessions:
            assert s not in svg, s


def test_card_escapes_hostile_model_names(db_path, pricing):
    hostile = [
        ']]></text><script>alert(1)<!--',
        "/Users/demo/Projects/secret-proj/models/evil&<b>\"'",
        "ctl\x00\x1b\x07-model",
    ]
    events = [
        UsageEvent(
            timestamp=datetime(2026, 3, 5, 10, i, tzinfo=timezone.utc), tool="claude-code", model=m,
            project_name="secret-proj", session_id="sess-secret", request_id=f"req-h{i}",
            message_id=f"msg-h{i}", input_tokens=100 * (i + 1), output_tokens=10,
        )
        for i, m in enumerate(hostile)
    ]
    with connect(db_path) as conn:
        upsert_events(conn, events, pricing, None)
        svg = render_card(conn, "lifelong", LAST_DAY)
    root = _svg_root(svg)  # still well-formed XML
    _assert_safe_svg(svg)
    for s in ("secret-proj", "/Users", "sess-secret", "\x00", "\x1b"):
        assert s not in svg
    text = _visible_text(root)
    assert "evil&<b>\"'" in text  # shown literally, escaped in the markup
    assert "&lt;script&gt;" in svg and "ctl-model" in text


def test_card_empty_ledger(db_path):
    with connect_readonly(db_path) as conn:
        svg = render_card(conn, "30d", LAST_DAY)
    text = _visible_text(_svg_root(svg))
    assert "Last 30 Days of AI Usage" in text and "No usage recorded in this period" in text
    assert "$0.00" in text and "0 days" in text
    assert not db_path.exists()


def test_card_titles_and_unknown_range(ledger):
    with connect_readonly(ledger) as conn:
        assert build_card_data(conn, "ytd", LAST_DAY).title == "2026 AI Usage"
        assert build_card_data(conn, "ytd", LAST_DAY).period == "Jan 1 – Mar 5, 2026"
        assert build_card_data(conn, "today", LAST_DAY).period == "Mar 5, 2026"
        with pytest.raises(ValueError):
            build_card_data(conn, "forever", LAST_DAY)


def test_card_formatting_helpers():
    assert model_label("bedrock/us.anthropic.claude-x") == "us.anthropic.claude-x"
    assert model_label("C:\\models\\local") == "local"
    assert model_label("") == "(unknown model)"
    assert len(model_label("x" * 80)) == 28 and model_label("x" * 80).endswith("…")
    assert clean_text("a\x00b\n  c") == "ab c"
    assert fmt_compact(1_234_567_890) == "1.23B" and fmt_compact(999) == "999"
    assert fmt_usd(1234.5) == "$1,234.50" and fmt_usd(250_000) == "$250.0K"
    assert fmt_amount(0.071) == "0.07" and fmt_amount(5.5) == "5.5" and fmt_amount(55.4) == "55"


def test_cli_card(ledger, tmp_path):
    out = tmp_path / "cards" / "report.svg"
    r = _invoke("card", "--range", "lifelong", "--out", out, "--db", ledger)
    assert r.exit_code == 0, r.output
    svg = out.read_text(encoding="utf-8")
    assert "Lifelong AI Usage" in _visible_text(_svg_root(svg))

    r = _invoke("card", "--range", "year", "--out", tmp_path / "y.svg", "--db", ledger)
    assert r.exit_code == 0 and "AI Usage" in (tmp_path / "y.svg").read_text()

    r = _invoke("card", "--range", "decade", "--out", tmp_path / "z.svg", "--db", ledger)
    assert r.exit_code == 2 and not (tmp_path / "z.svg").exists()

    missing = tmp_path / "nope.db"
    r = _invoke("card", "--out", tmp_path / "m.svg", "--db", missing)
    assert r.exit_code == 1 and not missing.exists()


# =========================================================================== web /api/card

def _client(db_path) -> TestClient:
    return TestClient(create_app(db_path, TEST_PRICING, today=lambda: LAST_DAY), base_url="http://127.0.0.1:8765")


def test_api_card(ledger):
    c = _client(ledger)
    r = c.get("/api/card?range=lifelong")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["content-disposition"].startswith("inline")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    _assert_safe_svg(r.text)
    assert "Lifelong AI Usage" in _visible_text(_svg_root(r.text))
    for s in PRIVATE_STRINGS:
        assert s not in r.text

    r = c.get("/api/card?range=ytd&download=1")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == (
        'attachment; filename="lifelong-ai-tracker_card_ytd_2026-03-05.svg"'
    )
    assert "2026 AI Usage" in r.text

    assert c.get("/api/card?range=forever").status_code == 422


def test_api_card_empty_ledger_and_host_guard(db_path):
    c = _client(db_path)
    r = c.get("/api/card")
    assert r.status_code == 200 and "No usage recorded" in r.text
    assert not db_path.exists()
    evil = TestClient(create_app(db_path, TEST_PRICING), base_url="http://evil.example")
    assert evil.get("/api/card").status_code == 403


def test_dashboard_has_export_card_button():
    html = (STATIC_DIR / "index.html").read_text()
    assert 'id="export-card"' in html and "Export Card (SVG)" in html
    assert "/api/card?range=" in html
    js = (STATIC_DIR / "app.js").read_text()
    # the link follows the current range *and* tool / model scope (see qs() in app.js)
    assert '$("export-card").href = `/api/card?${qs({ download: "1" })}`' in js
    assert 'p.set("range", state.range)' in js
