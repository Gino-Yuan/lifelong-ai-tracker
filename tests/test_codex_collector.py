"""Codex collector: fabricated `state_5.sqlite` built in code by
tests/fixtures/codex/build_codex_db.py (no real database is ever touched)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FIXTURES, TEST_PRICING
from lifelong_tracker import analytics
from lifelong_tracker.cli import app
from lifelong_tracker.collectors import (
    COLLECTORS,
    TOOL_ORDER,
    CodexCollector,
    OpenCodeCollector,
    get_collector,
    tool_label,
)
from lifelong_tracker.collectors import sqlite_source
from lifelong_tracker.sources import load_sources
from lifelong_tracker.storage import connect
from lifelong_tracker.sync import sync_source

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_JS = PROJECT_ROOT / "src" / "lifelong_tracker" / "web" / "static" / "app.js"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, FIXTURES / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fx = _load("build_codex_db", "codex/build_codex_db.py")
ocfx = _load("build_opencode_db", "opencode/build_opencode_db.py")

runner = CliRunner()
TOTAL = sum(v[3] for v in fx.EXPECTED_EVENTS.values())
CONTENT_COLUMNS = ("title", "first_user_message", "preview", "rollout_path")


@pytest.fixture
def cx() -> CodexCollector:
    return CodexCollector()


@pytest.fixture
def cx_dir(tmp_path) -> Path:
    d = tmp_path / "codex home"  # space in the path on purpose
    fx.build(d / "state_5.sqlite")
    return d


@pytest.fixture
def cx_db(cx_dir) -> Path:
    return cx_dir / "state_5.sqlite"


def _sync(db_path, source, pricing, collector=None):
    with connect(db_path) as conn:
        return sync_source(conn, collector or CodexCollector(), source, pricing)


def _rows(db_path):
    with connect(db_path) as conn:
        return {r["request_id"]: dict(r) for r in conn.execute("SELECT * FROM events WHERE tool = 'codex'")}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _update(db: Path, thread_id: str, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    with closing(sqlite3.connect(db)) as w, w:
        w.execute(f"UPDATE threads SET {sets} WHERE id = ?", (*cols.values(), thread_id))


# ------------------------------------------------------------------ registry / discovery

def test_registered_and_ready():
    assert COLLECTORS["codex"] is CodexCollector
    c = get_collector("codex")
    assert c.implemented and c.accepts_file
    assert tool_label("codex") == "Codex"
    assert list(TOOL_ORDER) == ["claude-code", "antigravity", "opencode", "codex", "t3"]


def test_discover_file_and_directory(cx, cx_dir, cx_db):
    assert cx.discover(cx_db) == [cx_db]
    assert cx.discover(cx_dir) == [cx_db]


def test_discover_is_not_recursive_and_skips_symlinks(cx, cx_db, tmp_path):
    fx.build(tmp_path / "root" / "nested" / "state_5.sqlite")
    assert cx.discover(tmp_path / "root") == []
    links = tmp_path / "links"
    links.mkdir()
    (links / "state_5.sqlite").symlink_to(cx_db)
    assert cx.discover(links) == []


def test_discover_ignores_other_state_versions(cx, tmp_path):
    fx.build(tmp_path / "v" / "state_4.sqlite")
    assert cx.discover(tmp_path / "v") == []


def test_discover_rejects_non_sqlite_file(cx, tmp_path):
    bogus = tmp_path / "state_5.sqlite"
    bogus.write_text("not a database")
    with pytest.raises(ValueError, match="not an SQLite database"):
        cx.discover(bogus)
    assert cx.discover(tmp_path) == []


# ------------------------------------------------------------------ parsing

def test_events_match_expected(cx, cx_db):
    scan = cx.read_incremental(cx_db, None)
    assert scan.lines_read == fx.EXPECTED_ROWS and scan.bad_lines == fx.EXPECTED_BAD
    got = {e.request_id: (e.model, e.project_name, e.timestamp_utc, e.input_tokens) for e in scan.events}
    assert got == fx.EXPECTED_EVENTS
    for e in scan.events:
        assert e.tool == "codex"
        assert e.session_id == e.request_id and e.message_id == "thread_total"
        assert (e.output_tokens, e.thinking_tokens, e.cache_read_tokens, e.cache_write_tokens) == (0, 0, 0, 0)
        assert e.dedup_key == ("codex", e.request_id, "thread_total")


def test_spec_sample_row(cx, tmp_path):
    db = fx.build(tmp_path / "s" / "state_5.sqlite", [fx.SPEC])
    (e,) = cx.read_incremental(db, None).events
    assert (e.request_id, e.model, e.project_name, e.timestamp_utc, e.input_tokens, e.total_tokens) == (
        "thr_fx_001", "gpt-6-luna", "my-app", "2026-10-17T18:03:42.023Z", 42850, 42850)


@pytest.mark.parametrize("created_ms, created_s, expected", [
    (1792260222023, 1792260222, "2026-10-17T18:03:42.023Z"),   # ms wins over seconds
    (None, 1792260222, "2026-10-17T18:03:42.000Z"),            # seconds fallback
    (0, 1792260222, "2026-10-17T18:03:42.000Z"),               # unusable ms -> seconds
    ("1792260222023", None, "2026-10-17T18:03:42.023Z"),       # digit strings accepted
    (None, 1792260222.5, "2026-10-17T18:03:42.500Z"),
])
def test_timestamp_fallback(cx, tmp_path, created_ms, created_s, expected):
    db = fx.build(tmp_path / "t" / "state_5.sqlite",
                  [fx.thread("thr_t", created_ms=created_ms, created_s=created_s, tokens=1)])
    (e,) = cx.read_incremental(db, None).events
    assert e.timestamp_utc == expected


@pytest.mark.parametrize("cwd, expected", [
    ("/Users/demo/work/my-app", "my-app"),
    ("/Users/demo/work/my-app/", "my-app"),
    ("C:\\Users\\demo\\proj", "proj"),
    ("relative/dir", "dir"),
    ("/", "codex"),
    ("", "codex"),
    ("   ", "codex"),
    (None, "codex"),
    (b"\x00blob", "codex"),
])
def test_project_name_is_cwd_basename(cx, tmp_path, cwd, expected):
    db = fx.build(tmp_path / "p" / "state_5.sqlite",
                  [fx.thread("thr_p", created_ms=1792260222023, cwd=cwd, tokens=1)])
    (e,) = cx.read_incremental(db, None).events
    assert e.project_name == expected
    assert "/Users" not in e.project_name and "\\" not in e.project_name


@pytest.mark.parametrize("tokens, expected", [
    (42850, 42850), ("350", 350), (7.9, 7),
    (None, None), (0, None), (-1, None), ("abc", None), ("", None),
])
def test_tokens_used(cx, tmp_path, tokens, expected):
    db = fx.build(tmp_path / "k" / "state_5.sqlite",
                  [fx.thread("thr_k", created_ms=1792260222023, tokens=tokens)])
    scan = cx.read_incremental(db, None)
    assert scan.bad_lines == 0
    assert [e.input_tokens for e in scan.events] == ([] if expected is None else [expected])


# ------------------------------------------------------------------ privacy

def test_content_columns_are_never_queried(cx, cx_db, monkeypatch):
    statements: list[str] = []
    real = sqlite_source.connect_readonly

    def traced(path):
        conn = real(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite_source, "connect_readonly", traced)
    cx.read_incremental(cx_db, None)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert selects, statements
    for sql in selects:
        assert not re.search(r"select\s+\*|threads\.\*", sql, re.I), sql
        for col in CONTENT_COLUMNS:
            assert col not in sql, (col, sql)


def test_selected_rows_carry_no_content(cx, cx_db, monkeypatch):
    seen: list[tuple] = []
    real = CodexCollector.parse_row
    monkeypatch.setattr(CodexCollector, "parse_row", lambda self, row: (seen.append(row), real(self, row))[1])
    cx.read_incremental(cx_db, None)
    assert len(seen) == fx.EXPECTED_ROWS
    blob = repr(seen)
    assert fx.CANARY not in blob and fx.SECRET_ROLLOUT not in blob


def test_no_content_reaches_the_ledger(db_path, cx_db, pricing):
    _sync(db_path, cx_db, pricing)
    with closing(sqlite3.connect(db_path)) as raw:
        dump = "\n".join(raw.iterdump()).replace(str(cx_db), "")  # checkpoint key is the source path
    for leaked in (fx.CANARY, fx.SECRET_ROLLOUT, "rollout", "/Users/demo", "C:\\Users", "openai"):
        assert leaked not in dump
    for word in ("title", "first message", "preview"):
        assert f"{word} {fx.CANARY}" not in dump


# ------------------------------------------------------------------ read-only guarantees

def test_source_database_is_never_modified(db_path, cx_dir, cx_db, pricing):
    before = (_digest(cx_db), cx_db.stat().st_mtime_ns, sorted(p.name for p in cx_dir.iterdir()))
    _sync(db_path, cx_db, pricing)
    _sync(db_path, cx_dir, pricing)
    after = (_digest(cx_db), cx_db.stat().st_mtime_ns, sorted(p.name for p in cx_dir.iterdir()))
    assert after == before


def test_works_on_read_only_file_and_directory(db_path, cx_dir, cx_db, pricing):
    os.chmod(cx_db, 0o444)
    os.chmod(cx_dir, 0o555)
    try:
        report = _sync(db_path, cx_dir, pricing)
    finally:
        os.chmod(cx_dir, 0o755)
        os.chmod(cx_db, 0o644)
    assert report.upsert.inserted == len(fx.EXPECTED_EVENTS)


def test_connection_is_read_only(cx_db):
    with closing(sqlite_source.connect_readonly(cx_db)) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE threads SET tokens_used = 0")
        conn.execute("PRAGMA query_only = OFF")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE threads SET tokens_used = 0")  # mode=ro still refuses


@pytest.mark.parametrize("dirname", ["a?b", "c#d", "100% sure", "代码 home"])
def test_awkward_path_characters(cx, tmp_path, dirname):
    db = fx.build(tmp_path / dirname / "state_5.sqlite")
    assert len(cx.read_incremental(db, None).events) == len(fx.EXPECTED_EVENTS)


def test_reads_live_wal_database(cx, tmp_path):
    db = fx.build(tmp_path / "wal" / "state_5.sqlite", [], wal=True)
    writer = sqlite3.connect(db)  # Codex still running: WAL + SHM exist, nothing checkpointed
    try:
        fx.insert(writer, fx.default_rows())
        assert db.with_name("state_5.sqlite-wal").stat().st_size > 0
        scan = cx.read_incremental(db, None)
        assert len(scan.events) == len(fx.EXPECTED_EVENTS)

        with writer:
            writer.execute("UPDATE threads SET tokens_used = 50000, updated_at_ms = ? WHERE id = 'thr_fx_001'",
                           (fx.T_LATE + 10,))
        nxt = cx.read_incremental(db, scan.state)
        assert not nxt.unchanged
        assert {e.request_id: e.input_tokens for e in nxt.events} == {"thr_fx_009": 10, "thr_fx_001": 50000}
    finally:
        writer.close()


# ------------------------------------------------------------------ schema errors

def test_missing_table_is_a_clear_error(cx, tmp_path):
    db = tmp_path / "x" / "state_5.sqlite"
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as c:
        c.execute("CREATE TABLE other (x)")
    with pytest.raises(ValueError, match="no 'threads' table - is this a Codex state database"):
        cx.read_incremental(db, None)


def test_missing_column_is_a_clear_error(cx, tmp_path):
    db = tmp_path / "y" / "state_5.sqlite"
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as c:
        c.execute("CREATE TABLE threads (id, created_at, model, cwd, tokens_used)")
    with pytest.raises(ValueError, match="missing column.*created_at_ms.*updated_at_ms"):
        cx.read_incremental(db, None)


# ------------------------------------------------------------------ end-to-end sync

def test_sync_totals_and_idempotence(db_path, cx_db, pricing):
    report = _sync(db_path, cx_db, pricing)
    assert report.lines_read == fx.EXPECTED_ROWS and report.bad_lines == fx.EXPECTED_BAD
    assert report.upsert.inserted == len(fx.EXPECTED_EVENTS)
    rows = _rows(db_path)
    assert set(rows) == set(fx.EXPECTED_EVENTS)
    assert rows["thr_fx_001"]["project_name"] == "my-app"
    assert all(r["cost_usd"] is None for r in rows.values())  # gpt-* is not in the test prices

    with connect(db_path) as conn:
        tot = analytics.summarize(conn, "all", tool="codex")
        unpriced = {u.model for u in analytics.unpriced_models(conn, pricing)}
    assert tot.events == 5 and tot.total_tokens == tot.input_tokens == TOTAL == 44415
    assert unpriced == {"gpt-6-luna", "gpt-6-sol", "unknown"}

    again = _sync(db_path, cx_db, pricing)
    assert again.files_unchanged == 1 and again.upsert.inserted == 0


def test_thread_total_growth_updates_one_row(db_path, cx_db, pricing):
    _sync(db_path, cx_db, pricing)
    _update(cx_db, "thr_fx_001", tokens_used=60000, updated_at_ms=fx.T_LATE + 1000)
    grown = _sync(db_path, cx_db, pricing)
    # re-read: the grown thread + the tie at the old mark (thr_fx_009)
    assert grown.lines_read == 2
    assert (grown.upsert.inserted, grown.upsert.updated, grown.upsert.unchanged) == (0, 1, 1)
    row = _rows(db_path)["thr_fx_001"]
    assert row["input_tokens"] == 60000
    assert row["timestamp"] == "2026-10-17T18:03:42.023Z"  # still attributed to thread creation

    # growing again and re-syncing the same state is a no-op after the first pass
    _update(cx_db, "thr_fx_001", tokens_used=61000, updated_at_ms=fx.T_LATE + 2000)
    assert _sync(db_path, cx_db, pricing).upsert.updated == 1
    assert _sync(db_path, cx_db, pricing).files_unchanged == 1
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE request_id = 'thr_fx_001'").fetchone()[0] == 1
    assert _rows(db_path)["thr_fx_001"]["input_tokens"] == 61000


def test_total_never_shrinks(db_path, cx_db, pricing):
    _sync(db_path, cx_db, pricing)
    _update(cx_db, "thr_fx_001", tokens_used=100, updated_at_ms=fx.T_LATE + 1000)
    _sync(db_path, cx_db, pricing)
    assert _rows(db_path)["thr_fx_001"]["input_tokens"] == 42850  # MAX keeps the larger total


def test_new_thread_is_inserted(db_path, cx_db, pricing):
    _sync(db_path, cx_db, pricing)
    with closing(sqlite3.connect(cx_db)) as w:
        fx.insert(w, [fx.thread("thr_fx_new", created_ms=fx.T_LATE + 5000, updated_ms=fx.T_LATE + 5000,
                                model="gpt-6-luna", cwd="/Users/demo/work/fresh", tokens=777)])
    inc = _sync(db_path, cx_db, pricing)
    assert inc.upsert.inserted == 1
    assert _rows(db_path)["thr_fx_new"]["project_name"] == "fresh"


def test_replaced_database_rescans(db_path, cx_dir, cx_db, pricing):
    _sync(db_path, cx_db, pricing)
    fresh = cx_dir / "new.sqlite"
    fx.build(fresh)
    os.replace(fresh, cx_db)
    report = _sync(db_path, cx_db, pricing)
    assert report.resets == [(str(cx_db.resolve()), "replaced")]
    assert report.lines_read == fx.EXPECTED_ROWS and report.upsert.inserted == 0


def test_restored_older_copy_rescans(db_path, cx_dir, cx_db, pricing):
    backup = cx_dir.parent / "backup.sqlite"
    shutil.copyfile(cx_db, backup)
    with closing(sqlite3.connect(cx_db)) as w:
        fx.insert(w, [fx.thread("thr_fx_late", created_ms=fx.T_LATE + 9000, updated_ms=fx.T_LATE + 9000,
                                tokens=1)])
    _sync(db_path, cx_db, pricing)
    data = backup.read_bytes()
    with cx_db.open("r+b") as dst:  # restore in place: same inode
        dst.write(data)
        dst.truncate()
    report = _sync(db_path, cx_db, pricing)
    assert report.resets and report.resets[0][1] == "truncated"
    assert report.lines_read == fx.EXPECTED_ROWS


def test_codex_and_opencode_share_one_ledger(db_path, cx_db, tmp_path, pricing):
    oc_db = ocfx.build(tmp_path / "oc" / "opencode.db")
    _sync(db_path, cx_db, pricing)
    _sync(db_path, oc_db, pricing, OpenCodeCollector())
    with connect(db_path) as conn:
        by_tool = {r.tool: r.events for r in analytics.tools_summary(conn)}
    assert by_tool == {"codex": 5, "opencode": 6}


# ------------------------------------------------------------------ CLI / config / web / UI

@pytest.mark.parametrize("which", ["file", "dir"])
def test_cli_sync_file_or_directory(db_path, cx_dir, cx_db, which):
    src = cx_db if which == "file" else cx_dir
    r = runner.invoke(app, ["sync", "--source", str(src), "--tool", "codex",
                            "--db", str(db_path), "--pricing", str(TEST_PRICING)])
    assert r.exit_code == 0, r.output
    assert "Codex" in r.output and len(_rows(db_path)) == 5


def test_cli_reports_wrong_schema_without_traceback(db_path, tmp_path):
    db = tmp_path / "z" / "state_5.sqlite"
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as c:
        c.execute("CREATE TABLE t (x)")
    r = runner.invoke(app, ["sync", "--source", str(db), "--tool", "codex", "--db", str(db_path)])
    assert r.exit_code == 2 and "threads" in r.output and "Traceback" not in r.output


def test_cli_collectors_lists_codex_ready():
    r = runner.invoke(app, ["collectors"])
    line = next(ln for ln in r.output.splitlines() if "codex" in ln)
    assert "Codex" in line and "ready" in line


def test_sync_all_accepts_db_file_path(db_path, cx_db, tmp_path):
    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({"sources": [{"name": "codex", "tool": "codex", "path": str(cx_db)}]}))
    r = runner.invoke(app, ["sync-all", "--config", str(cfg), "--db", str(db_path),
                            "--pricing", str(TEST_PRICING), "--quiet"])
    assert r.exit_code == 0, r.output
    assert "1 ok, 0 failed" in r.output and "5 new" in r.output


def test_sources_example_lists_codex():
    specs = {s.name: s for s in load_sources(PROJECT_ROOT / "sources.example.json")}
    assert specs["codex"].tool == "codex" and specs["codex"].path.name == "state_5.sqlite"
    assert specs["codex"].enabled is False
    assert specs["opencode"].path.name == "opencode.db"


def test_web_lists_codex(db_path, cx_db, pricing):
    from fastapi.testclient import TestClient

    from lifelong_tracker.web.app import create_app

    _sync(db_path, cx_db, pricing)
    client = TestClient(create_app(db_path, TEST_PRICING), base_url="http://127.0.0.1:8765")
    tools = client.get("/api/tools").json()["tools"]
    assert [(t["tool"], t["label"], t["tokens"]) for t in tools] == [("codex", "Codex", 44415)]
    ts = client.get("/api/timeseries", params={"range": "lifelong", "split_by": "tool"}).json()
    assert [s["tool"] for s in ts["series"]] == ["codex"]
    proj = client.get("/api/breakdown", params={"range": "lifelong", "by": "project", "tool": "codex"})
    assert proj.status_code == 200
    keys = {r["key"] for r in proj.json()["rows"]}
    assert {"my-app", "win-app", "trailing", "codex"} <= keys


def test_dashboard_gives_codex_a_fixed_emerald():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'codex: "#1a9a48"' in js
