"""OpenCode collector: fabricated `opencode.db` built in code by
tests/fixtures/opencode/build_opencode_db.py (no real database is ever touched)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FIXTURES, TEST_PRICING
from lifelong_tracker import analytics
from lifelong_tracker.cli import app
from lifelong_tracker.collectors import COLLECTORS, TOOL_ORDER, OpenCodeCollector, get_collector, tool_label
from lifelong_tracker.collectors.opencode import connect_readonly, ms_to_utc
from lifelong_tracker.models import format_utc
from lifelong_tracker.sources import SourceSpec, sync_one
from lifelong_tracker.storage import connect
from lifelong_tracker.sync import sync_source

_spec = importlib.util.spec_from_file_location(
    "build_opencode_db", FIXTURES / "opencode" / "build_opencode_db.py")
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)

runner = CliRunner()
TOTAL_TOKENS = sum(sum(v[3:]) for v in fx.EXPECTED_EVENTS.values())


@pytest.fixture
def oc() -> OpenCodeCollector:
    return OpenCodeCollector()


@pytest.fixture
def oc_dir(tmp_path) -> Path:
    d = tmp_path / "opencode data"  # space in the path on purpose
    fx.build(d / "opencode.db")
    return d


@pytest.fixture
def oc_db(oc_dir) -> Path:
    return oc_dir / "opencode.db"


def _sync(db_path, source, pricing):
    with connect(db_path) as conn:
        return sync_source(conn, OpenCodeCollector(), source, pricing)


def _rows(db_path):
    with connect(db_path) as conn:
        return {r["message_id"]: dict(r) for r in conn.execute("SELECT * FROM events")}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scan(oc, path, checkpoint=None):
    return oc.read_incremental(path, checkpoint)


# ------------------------------------------------------------------ registry / discovery

def test_registered_and_ready():
    assert COLLECTORS["opencode"] is OpenCodeCollector
    c = get_collector("opencode")
    assert c.implemented and c.accepts_file
    assert tool_label("opencode") == "OpenCode"
    assert TOOL_ORDER.index("opencode") > TOOL_ORDER.index("antigravity")


def test_discover_file_and_directory(oc, oc_dir, oc_db):
    assert oc.discover(oc_db) == [oc_db]
    assert oc.discover(oc_dir) == [oc_db]


def test_discover_directory_is_not_recursive(oc, tmp_path):
    fx.build(tmp_path / "root" / "nested" / "opencode.db")
    assert oc.discover(tmp_path / "root") == []
    assert oc.discover(tmp_path / "empty-does-not-matter") == []


def test_discover_skips_symlinked_db_in_directory(oc, oc_db, tmp_path):
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    (link_dir / "opencode.db").symlink_to(oc_db)
    assert oc.discover(link_dir) == []


def test_discover_rejects_non_sqlite_file(oc, tmp_path):
    bogus = tmp_path / "opencode.db"
    bogus.write_text("not a database")
    with pytest.raises(ValueError, match="not an SQLite database"):
        oc.discover(bogus)
    assert oc.discover(tmp_path) == []  # in a directory it is simply not picked up


# ------------------------------------------------------------------ parsing

def test_spec_sample_timestamp_conversion():
    assert format_utc(ms_to_utc(fx.SPEC_CREATED)) == "2026-10-17T18:03:42.023Z"
    assert format_utc(ms_to_utc(0 + 1)) == "1970-01-01T00:00:00.001Z"


def test_events_match_expected(oc, oc_db):
    scan = _scan(oc, oc_db)
    assert scan.lines_read == fx.EXPECTED_ROWS and scan.bad_lines == fx.EXPECTED_BAD
    got = {
        e.message_id: (e.session_id, e.model, e.timestamp_utc, e.input_tokens, e.output_tokens,
                       e.thinking_tokens, e.cache_read_tokens, e.cache_write_tokens)
        for e in scan.events
    }
    assert got == fx.EXPECTED_EVENTS
    for e in scan.events:
        assert e.tool == "opencode" and e.project_name == "opencode"
        assert e.request_id == e.message_id
        assert e.dedup_key == ("opencode", e.message_id, e.message_id)


@pytest.mark.parametrize("model, extra, expected", [
    ({"id": "gemini-3-flash", "providerID": "gcp-proxy", "variant": "high"}, {}, "gemini-3-flash"),
    ("claude-sonnet-4-5", {}, "claude-sonnet-4-5"),
    ("  claude-sonnet-4-5 ", {}, "claude-sonnet-4-5"),
    ({"id": ""}, {"modelID": "fallback-model"}, "fallback-model"),
    ({"id": 42}, {}, "unknown"),
    (None, {}, "unknown"),
    (None, {"modelID": "flat-model"}, "flat-model"),
    ("", {}, "unknown"),
    (["list"], {}, "unknown"),
])
def test_model_resolution(oc, tmp_path, model, extra, expected):
    t = fx.ms("2026-10-20T00:00:00Z")
    db = fx.build(tmp_path / "m" / "opencode.db",
                  [fx.message("msg_m", "ses_m", 1, t, data=fx.assistant(t, model, fx.tokens(1), **extra))])
    (event,) = _scan(oc, db).events
    assert event.model == expected


@pytest.mark.parametrize("tok, expected", [
    (fx.tokens(1, 2, 3, 4, 5), (1, 2, 3, 4, 5)),
    ({"input": 9}, (9, 0, 0, 0, 0)),                        # missing keys default to 0
    ({"input": 1, "cache": 7}, (1, 0, 0, 0, 0)),            # cache not an object
    ({"input": 2.9, "output": True}, (2, 0, 0, 0, 0)),      # floats truncated, bools ignored
    ({"input": "100", "output": None, "reasoning": -3}, None),  # all unusable -> skipped
    (fx.tokens(), None),                                    # all zero -> skipped
    ({}, None),
    ([], None),
])
def test_token_mapping(oc, tmp_path, tok, expected):
    t = fx.ms("2026-10-20T00:00:00Z")
    db = fx.build(tmp_path / "t" / "opencode.db",
                  [fx.message("msg_t", "ses_t", 1, t, data=fx.assistant(t, "m", tok))])
    scan = _scan(oc, db)
    assert scan.bad_lines == 0
    if expected is None:
        assert scan.events == []
    else:
        (e,) = scan.events
        assert (e.input_tokens, e.output_tokens, e.thinking_tokens,
                e.cache_read_tokens, e.cache_write_tokens) == expected


@pytest.mark.parametrize("created, data_time, expected", [
    (fx.ms("2026-10-20T01:02:03.456Z"), None, "2026-10-20T01:02:03.456Z"),
    (0, fx.ms("2026-10-21T00:00:00Z"), "2026-10-21T00:00:00.000Z"),       # column unusable -> data.time
    (-5, fx.ms("2026-10-22T00:00:00Z"), "2026-10-22T00:00:00.000Z"),
    (10 ** 18, fx.ms("2026-10-23T00:00:00Z"), "2026-10-23T00:00:00.000Z"),  # out of range
])
def test_timestamp_sources(oc, tmp_path, created, data_time, expected):
    data = fx.assistant(data_time or 0, "m", fx.tokens(1))
    if data_time is None:
        data.pop("time")
    db = fx.build(tmp_path / "ts" / "opencode.db",
                  [fx.message("msg_ts", "ses_ts", 1, created, updated=max(created, 1), data=data)])
    (e,) = _scan(oc, db).events
    assert e.timestamp_utc == expected


def test_row_without_any_timestamp_is_bad(oc, tmp_path):
    data = fx.assistant(0, "m", fx.tokens(1))
    data.pop("time")
    db = fx.build(tmp_path / "nt" / "opencode.db", [fx.message("msg_nt", "s", 1, 0, data=data)])
    scan = _scan(oc, db)
    assert scan.events == [] and scan.bad_lines == 1


def test_event_never_carries_text(oc, oc_db):
    dump = repr(_scan(oc, oc_db).events)
    for leaked in (fx.CANARY, fx.SECRET_DIR, "gcp-proxy", "providerID", "variant"):
        assert leaked not in dump


def test_text_columns_are_never_selected(oc, oc_db, monkeypatch):
    """Only json_extract'ed sub-trees are fetched; `data` itself never reaches Python."""
    seen: list[tuple] = []
    real = OpenCodeCollector.parse_row

    def spy(self, row):
        seen.append(row)
        return real(self, row)

    monkeypatch.setattr(OpenCodeCollector, "parse_row", spy)
    _scan(oc, oc_db)
    assert len(seen) == fx.EXPECTED_ROWS
    blob = repr(seen)
    assert fx.CANARY not in blob and fx.SECRET_DIR not in blob and '"content"' not in blob


# ------------------------------------------------------------------ read-only guarantees

def test_source_database_is_never_modified(db_path, oc_dir, oc_db, pricing):
    before = (_digest(oc_db), oc_db.stat().st_mtime_ns, sorted(p.name for p in oc_dir.iterdir()))
    _sync(db_path, oc_db, pricing)
    _sync(db_path, oc_dir, pricing)
    after = (_digest(oc_db), oc_db.stat().st_mtime_ns, sorted(p.name for p in oc_dir.iterdir()))
    assert after == before  # no -journal / -wal / -shm left behind either


def test_works_on_read_only_file_and_directory(db_path, oc_dir, oc_db, pricing):
    os.chmod(oc_db, 0o444)
    os.chmod(oc_dir, 0o555)
    try:
        report = _sync(db_path, oc_db, pricing)
    finally:
        os.chmod(oc_dir, 0o755)
        os.chmod(oc_db, 0o644)
    assert report.upsert.inserted == len(fx.EXPECTED_EVENTS)


def test_connection_is_read_only(oc_db):
    with closing(connect_readonly(oc_db)) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM session_message")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("PRAGMA query_only = OFF")
            conn.execute("DELETE FROM session_message")  # mode=ro still refuses
    assert sqlite3.connect(oc_db).execute("SELECT COUNT(*) FROM session_message").fetchone()[0] == 10


@pytest.mark.parametrize("dirname", ["a?b", "c#d", "100% sure", "名字 with space"])
def test_awkward_path_characters(oc, tmp_path, dirname):
    db = fx.build(tmp_path / dirname / "opencode.db")
    assert len(_scan(oc, db).events) == len(fx.EXPECTED_EVENTS)


def test_reads_live_wal_database(oc, tmp_path):
    db = fx.build(tmp_path / "wal" / "opencode.db", [], wal=True)
    writer = sqlite3.connect(db)  # OpenCode still running: WAL + SHM exist, nothing checkpointed
    try:
        fx.insert(writer, fx.default_rows())
        assert db.with_name("opencode.db-wal").stat().st_size > 0
        scan = _scan(oc, db)
        assert len(scan.events) == len(fx.EXPECTED_EVENTS)

        t = fx.ms("2026-10-25T00:00:00Z")
        fx.insert(writer, [fx.message("msg_live", "ses_live", 1, t, data=fx.assistant(t, "m", fx.tokens(5)))])
        nxt = _scan(oc, db, scan.state)
        assert not nxt.unchanged  # WAL growth changes the signature even if the main file did not
        # only the new row plus the row sitting exactly at the old high-water mark
        assert [e.message_id for e in nxt.events] == ["msg_fx_b6", "msg_live"]
    finally:
        writer.close()


def test_reads_closed_wal_database(oc, tmp_path):
    db = fx.build(tmp_path / "walc" / "opencode.db", wal=True)
    assert len(_scan(oc, db).events) == len(fx.EXPECTED_EVENTS)


# ------------------------------------------------------------------ schema errors

def test_missing_table_is_a_clear_error(oc, tmp_path):
    db = tmp_path / "other" / "opencode.db"
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as c:
        c.execute("CREATE TABLE something (x)")
    with pytest.raises(ValueError, match="no 'session_message' table"):
        _scan(oc, db)


def test_missing_column_is_a_clear_error(oc, tmp_path):
    db = tmp_path / "old" / "opencode.db"
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as c:
        c.execute("CREATE TABLE session_message (id, session_id, data)")
    with pytest.raises(ValueError, match="missing column.*time_created"):
        _scan(oc, db)


# ------------------------------------------------------------------ end-to-end sync

def test_sync_totals_cost_and_dedup(db_path, oc_db, pricing):
    report = _sync(db_path, oc_db, pricing)
    assert (report.files_seen, report.files_scanned) == (1, 1)
    assert report.lines_read == fx.EXPECTED_ROWS and report.bad_lines == fx.EXPECTED_BAD
    assert report.upsert.inserted == len(fx.EXPECTED_EVENTS)

    rows = _rows(db_path)
    assert set(rows) == set(fx.EXPECTED_EVENTS)
    assert all(r["tool"] == "opencode" and r["project_name"] == "opencode" for r in rows.values())
    assert rows["msg_fx_a2"]["cost_usd"] == pytest.approx((1200 * 3 + 340 * 15 + 800 * 0.3 + 200 * 3.75) / 1e6)
    assert rows["msg_fx_b3"]["cost_usd"] == pytest.approx((10 * 5 + 20 * 25 + 30 * 25) / 1e6)
    assert rows["msg_fx_a1"]["cost_usd"] is None  # gemini-3-flash is not in the test prices

    with connect(db_path) as conn:
        tot = analytics.summarize(conn, "all", tool="opencode")
        unpriced = {u.model for u in analytics.unpriced_models(conn, pricing)}
    assert tot.events == 6 and tot.total_tokens == TOTAL_TOKENS == 8203
    assert unpriced == {"gemini-3-flash", "unknown", "gemini-4-hydrogen"}

    again = _sync(db_path, oc_db, pricing)
    assert again.files_unchanged == 1 and again.upsert.inserted == 0
    assert len(_rows(db_path)) == 6


def test_no_text_reaches_the_ledger(db_path, oc_db, pricing):
    _sync(db_path, oc_db, pricing)
    with closing(sqlite3.connect(db_path)) as raw:
        dump = "\n".join(raw.iterdump())
    for leaked in (fx.CANARY, fx.SECRET_DIR, "/Users/demo", "gcp-proxy", "opencode data"):
        assert leaked not in dump.replace(str(oc_db), "")  # checkpoint key is the source path


def test_incremental_new_and_updated_rows(db_path, oc_db, pricing):
    first = _sync(db_path, oc_db, pricing)
    assert first.upsert.inserted == 6

    t_new = fx.ms("2026-10-26T10:00:00Z")
    with closing(sqlite3.connect(oc_db)) as w:
        fx.insert(w, [
            fx.message("msg_fx_c1", "ses_fx_gamma", 1, t_new,
                       data=fx.assistant(t_new, "claude-sonnet-4-5", fx.tokens(40, 60))),
            # streaming finished: same id, bigger final usage, time_updated bumped
            fx.message("msg_fx_b1", "ses_fx_beta", 1, fx.T_B1, updated=t_new + 5,
                       data=fx.assistant(fx.T_B1, None, fx.tokens(100, 500))),
        ])
    inc = _sync(db_path, oc_db, pricing)
    # only rows at/after the high-water mark are re-read: the 2 changed rows + the tie at the mark
    assert inc.lines_read == 3
    assert (inc.upsert.inserted, inc.upsert.updated, inc.upsert.unchanged) == (1, 1, 1)
    rows = _rows(db_path)
    assert rows["msg_fx_b1"]["output_tokens"] == 500
    assert rows["msg_fx_b1"]["timestamp"] == "2026-10-17T00:00:00.000Z"
    assert len(rows) == 7

    idle = _sync(db_path, oc_db, pricing)
    assert idle.files_unchanged == 1


def test_replaced_database_rescans(db_path, oc_dir, oc_db, pricing):
    _sync(db_path, oc_db, pricing)
    fresh = oc_dir / "new.db"
    fx.build(fresh)
    os.replace(fresh, oc_db)  # new inode
    report = _sync(db_path, oc_db, pricing)
    assert report.files_reset == 1 and report.resets[0][1] == "replaced"
    assert report.lines_read == fx.EXPECTED_ROWS and report.upsert.inserted == 0


def test_restored_older_copy_rescans(db_path, oc_dir, oc_db, pricing):
    backup = oc_dir.parent / "backup.db"
    shutil.copyfile(oc_db, backup)
    t_new = fx.ms("2026-11-01T00:00:00Z")
    with closing(sqlite3.connect(oc_db)) as w:
        fx.insert(w, [fx.message("msg_fx_late", "s", 1, t_new, data=fx.assistant(t_new, "m", fx.tokens(1)))])
    _sync(db_path, oc_db, pricing)

    ino = oc_db.stat().st_ino
    with backup.open("rb") as src, oc_db.open("r+b") as dst:  # restore in place: same inode
        data = src.read()
        dst.seek(0)
        dst.write(data)
        dst.truncate()
    assert oc_db.stat().st_ino == ino
    report = _sync(db_path, oc_db, pricing)
    assert report.files_reset == 1 and report.resets[0][1] == "truncated"
    assert report.lines_read == fx.EXPECTED_ROWS


# ------------------------------------------------------------------ CLI / sync-all / web

@pytest.mark.parametrize("which", ["file", "dir"])
def test_cli_sync_file_or_directory(db_path, oc_dir, oc_db, which):
    src = oc_db if which == "file" else oc_dir
    r = runner.invoke(app, ["sync", "--source", str(src), "--tool", "opencode",
                            "--db", str(db_path), "--pricing", str(TEST_PRICING)])
    assert r.exit_code == 0, r.output
    assert "OpenCode" in r.output
    assert len(_rows(db_path)) == 6


def test_cli_rejects_file_for_directory_tools(db_path, oc_db):
    r = runner.invoke(app, ["sync", "--source", str(oc_db), "--tool", "claude-code", "--db", str(db_path)])
    assert r.exit_code == 2 and "must be a directory" in r.output
    assert not db_path.exists()


def test_cli_reports_bad_database_without_creating_ledger(db_path, tmp_path):
    bogus = tmp_path / "opencode.db"
    bogus.write_text("nope")
    r = runner.invoke(app, ["sync", "--source", str(bogus), "--tool", "opencode", "--db", str(db_path)])
    assert r.exit_code == 2 and "not an SQLite database" in r.output
    assert not db_path.exists()


def test_cli_reports_wrong_schema(db_path, tmp_path):
    db = tmp_path / "x" / "opencode.db"
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as c:
        c.execute("CREATE TABLE t (x)")
    r = runner.invoke(app, ["sync", "--source", str(db), "--tool", "opencode", "--db", str(db_path)])
    assert r.exit_code == 2 and "session_message" in r.output


def test_cli_collectors_lists_opencode_ready():
    r = runner.invoke(app, ["collectors"])
    assert r.exit_code == 0
    line = next(ln for ln in r.output.splitlines() if "opencode" in ln)
    assert "ready" in line


def test_sync_all_accepts_db_file_path(db_path, oc_db, tmp_path):
    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({"sources": [{"name": "oc", "tool": "opencode", "path": str(oc_db)}]}))
    r = runner.invoke(app, ["sync-all", "--config", str(cfg), "--db", str(db_path),
                            "--pricing", str(TEST_PRICING), "--quiet"])
    assert r.exit_code == 0, r.output
    assert "1 ok, 0 failed" in r.output and "6 new" in r.output


def test_sync_one_rejects_file_for_directory_tool(db_path, oc_db, pricing):
    with connect(db_path) as conn:
        res = sync_one(conn, SourceSpec(name="cc", tool="claude-code", path=oc_db), pricing)
    assert res.status == "failed" and "not a directory" in res.error


def test_web_lists_opencode(db_path, oc_db, pricing):
    from fastapi.testclient import TestClient

    from lifelong_tracker.web.app import create_app

    _sync(db_path, oc_db, pricing)
    client = TestClient(create_app(db_path, TEST_PRICING), base_url="http://127.0.0.1:8765")
    tools = client.get("/api/tools").json()
    assert any(t.get("tool") == "opencode" and t.get("label") == "OpenCode" for t in tools["tools"])
    ov = client.get("/api/overview", params={"range": "lifelong", "tool": "opencode"}).json()
    assert ov["totals"]["events"] == 6
