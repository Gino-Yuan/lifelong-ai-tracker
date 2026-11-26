"""Streaming-duplicate semantics: one row per (tool, request_id, message_id) holding the
complete final usage - never the first placeholder chunk, never a sum of chunks."""

from datetime import datetime, timezone

import pytest

from _factory import assistant_line, write
from conftest import CANARY
from lifelong_tracker.models import UsageEvent
from lifelong_tracker.storage import merge_batch, upsert_events
from lifelong_tracker.sync import sync_source


def _ev(out, *, inp=10, cr=1000, cw=200, ts=1, model="claude-sonnet-4-5", req="req-1", msg="msg-1"):
    return UsageEvent(
        timestamp=datetime(2026, 4, 1, 10, 0, ts, tzinfo=timezone.utc), tool="claude-code",
        model=model, request_id=req, message_id=msg, session_id="s", project_name="p",
        input_tokens=inp, output_tokens=out, cache_read_tokens=cr, cache_write_tokens=cw,
    )


def _rows(conn):
    return conn.execute("SELECT * FROM events ORDER BY id").fetchall()


def test_chunks_in_one_batch_merge_to_final_usage(conn, pricing):
    res = upsert_events(conn, [_ev(1, ts=1), _ev(45, ts=2), _ev(300, ts=3)], pricing)
    assert (res.inserted, res.updated, res.unchanged) == (1, 0, 0)
    (row,) = _rows(conn)
    assert row["output_tokens"] == 300  # final, not first (1) and not sum (346)
    assert row["input_tokens"] == 10 and row["cache_read_tokens"] == 1000
    assert row["timestamp"] == "2026-04-01T10:00:01.000Z"  # earliest chunk


def test_chunks_across_calls_use_on_conflict_update(conn, pricing):
    upsert_events(conn, [_ev(1, ts=1)], pricing)  # placeholder chunk stored first
    res = upsert_events(conn, [_ev(300, ts=3)], pricing)
    assert (res.inserted, res.updated) == (0, 1)
    (row,) = _rows(conn)
    assert row["output_tokens"] == 300
    # cost recomputed for the merged tokens: (10*3 + 300*15 + 1000*0.3 + 200*3.75) / 1e6
    assert row["cost_usd"] == pytest.approx((30 + 4500 + 300 + 750) / 1e6)


def test_order_independent_and_idempotent(conn, pricing):
    upsert_events(conn, [_ev(300, ts=3)], pricing)
    res = upsert_events(conn, [_ev(1, ts=1), _ev(45, ts=2)], pricing)
    (row,) = _rows(conn)
    assert row["output_tokens"] == 300
    assert row["timestamp"] == "2026-04-01T10:00:01.000Z"
    assert res.updated == 1  # only the earlier timestamp changed
    again = upsert_events(conn, [_ev(300, ts=3), _ev(1, ts=1)], pricing)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 1)
    assert len(_rows(conn)) == 1


def test_distinct_keys_are_distinct_rows(conn, pricing):
    upsert_events(conn, [_ev(5, msg="a"), _ev(5, msg="b"), _ev(5, req="req-2", msg="a")], pricing)
    assert len(_rows(conn)) == 3


def test_merge_batch_rule():
    merged = merge_batch([_ev(1, ts=5, cw=0), _ev(9, ts=2, cw=7, cr=3)])
    assert len(merged) == 1
    m = merged[0]
    assert (m.output_tokens, m.cache_write_tokens, m.cache_read_tokens) == (9, 7, 1000)
    assert m.timestamp.second == 2


def test_unpriced_model_gets_null_cost(conn, pricing):
    upsert_events(conn, [_ev(5, model="fictional-model-x1")], pricing)
    assert _rows(conn)[0]["cost_usd"] is None


def test_fixture_sync_end_to_end(conn, pricing, claude_source, collector):
    report = sync_source(conn, collector, claude_source, pricing)
    assert report.files_seen == 2
    assert report.bad_lines == 1
    # A001 x3 chunks, A002, A003 in alpha; A002 replay, B001, B002 in beta
    assert report.events_parsed == 8
    assert report.upsert.inserted == 5
    rows = {r["message_id"]: r for r in _rows(conn)}
    assert set(rows) == {"msg_fx_A001", "msg_fx_A002", "msg_fx_A003", "msg_fx_B001", "msg_fx_B002"}
    a1 = rows["msg_fx_A001"]
    assert (a1["input_tokens"], a1["output_tokens"], a1["cache_read_tokens"], a1["cache_write_tokens"]) == (
        12, 180, 2000, 500)
    # replayed A002 in another session file was not double counted and kept its first session
    assert rows["msg_fx_A002"]["output_tokens"] == 300
    assert rows["msg_fx_A002"]["session_id"] == "sess-fx-alpha-0001"
    assert rows["msg_fx_A003"]["cache_write_tokens"] == 150
    assert rows["msg_fx_B002"]["request_id"] == ""
    assert rows["msg_fx_B001"]["cost_usd"] is None


def test_no_conversation_text_is_persisted(db_path, pricing, claude_source, collector):
    from lifelong_tracker.storage import connect

    with connect(db_path) as c:
        sync_source(c, collector, claude_source, pricing)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = b"".join(p.read_bytes() for p in db_path.parent.iterdir() if p.is_file())
    assert CANARY.encode() not in blob
    assert b"do not store" not in blob


def test_resync_unchanged_source_is_noop(conn, pricing, claude_source, collector):
    sync_source(conn, collector, claude_source, pricing)
    before = [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]
    report = sync_source(conn, collector, claude_source, pricing)
    assert report.files_unchanged == 2 and report.files_scanned == 0
    assert [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY id")] == before


def test_streaming_chunks_split_across_syncs(conn, pricing, collector, tmp_path):
    src = tmp_path / "src"
    f = write(src / "p" / "s.jsonl", assistant_line("msg-s", req_id="req-s", out=1))
    sync_source(conn, collector, src, pricing)
    assert _rows(conn)[0]["output_tokens"] == 1
    write(f, assistant_line("msg-s", req_id="req-s", out=77), mode="a")
    report = sync_source(conn, collector, src, pricing)
    assert report.upsert.updated == 1
    (row,) = _rows(conn)
    assert row["output_tokens"] == 77
