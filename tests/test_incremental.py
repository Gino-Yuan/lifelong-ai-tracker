"""Incremental scan: checkpoint resume, partial lines, truncation / rewrite / replacement."""

import os

from _factory import assistant_line, write
from lifelong_tracker.storage import get_checkpoint
from lifelong_tracker.sync import sync_source


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def _sum_out(conn):
    return conn.execute("SELECT COALESCE(SUM(output_tokens), 0) FROM events").fetchone()[0]


def _src(tmp_path):
    return tmp_path / "src"


def test_resume_reads_only_appended_lines(conn, pricing, collector, tmp_path, monkeypatch):
    f = write(_src(tmp_path) / "p" / "s.jsonl", assistant_line("m1", req_id="r1"), assistant_line("m2", req_id="r2"))
    r1 = sync_source(conn, collector, _src(tmp_path), pricing)
    assert r1.lines_read == 2 and _count(conn) == 2
    cp = get_checkpoint(conn, "claude-code", str(f.resolve()))
    assert cp.offset == f.stat().st_size and cp.lines_read == 2 and cp.resets == 0
    assert cp.inode == f.stat().st_ino and cp.head_len == min(4096, cp.offset) and cp.head_hash

    write(f, assistant_line("m3", req_id="r3"), mode="a")
    seen: list[int] = []
    real_parse = collector.parse_record
    monkeypatch.setattr(collector, "parse_record", lambda rec, p: seen.append(1) or real_parse(rec, p))
    r2 = sync_source(conn, collector, _src(tmp_path), pricing)
    assert r2.lines_read == 1 and len(seen) == 1  # earlier lines were not re-parsed
    assert _count(conn) == 3
    assert get_checkpoint(conn, "claude-code", str(f.resolve())).lines_read == 3


def test_unchanged_file_is_not_opened(conn, pricing, collector, tmp_path, monkeypatch):
    write(_src(tmp_path) / "p" / "s.jsonl", assistant_line("m1", req_id="r1"))
    sync_source(conn, collector, _src(tmp_path), pricing)
    monkeypatch.setattr(collector, "parse_record", lambda *a: (_ for _ in ()).throw(AssertionError))
    report = sync_source(conn, collector, _src(tmp_path), pricing)
    assert report.files_unchanged == 1 and report.lines_read == 0


def test_partial_trailing_line_waits_for_newline(conn, pricing, collector, tmp_path):
    full = assistant_line("m2", req_id="r2", out=42)
    head, tail = full[:60], full[60:]
    f = write(_src(tmp_path) / "p" / "s.jsonl", assistant_line("m1", req_id="r1"), head)
    r1 = sync_source(conn, collector, _src(tmp_path), pricing)
    assert r1.bad_lines == 0 and _count(conn) == 1
    cp = get_checkpoint(conn, "claude-code", str(f.resolve()))
    assert cp.offset == len(assistant_line("m1", req_id="r1").encode())  # not past the partial line
    assert cp.size > cp.offset

    write(f, tail, mode="a")  # writer finishes the line
    r2 = sync_source(conn, collector, _src(tmp_path), pricing)
    assert r2.bad_lines == 0 and r2.lines_read == 1
    assert _count(conn) == 2 and _sum_out(conn) == 20 + 42


def test_truncation_resets_without_double_counting(conn, pricing, collector, tmp_path):
    f = write(_src(tmp_path) / "p" / "s.jsonl",
              assistant_line("m1", req_id="r1"), assistant_line("m2", req_id="r2"), assistant_line("m3", req_id="r3"))
    sync_source(conn, collector, _src(tmp_path), pricing)
    assert _count(conn) == 3 and _sum_out(conn) == 60

    write(f, assistant_line("m1", req_id="r1"))  # truncated to its first line
    report = sync_source(conn, collector, _src(tmp_path), pricing)
    assert report.files_reset == 1 and report.resets[0][1] == "truncated"
    # lifelong ledger: m2/m3 remain, m1 is not counted twice
    assert _count(conn) == 3 and _sum_out(conn) == 60
    cp = get_checkpoint(conn, "claude-code", str(f.resolve()))
    assert cp.resets == 1 and cp.offset == f.stat().st_size and cp.lines_read == 1

    write(f, assistant_line("m4", req_id="r4"), mode="a")  # keeps tracking after the reset
    sync_source(conn, collector, _src(tmp_path), pricing)
    assert _count(conn) == 4


def test_in_place_rewrite_same_inode_detected_by_head_hash(conn, pricing, collector, tmp_path):
    f = write(_src(tmp_path) / "p" / "s.jsonl", assistant_line("m1", req_id="r1", out=11))
    sync_source(conn, collector, _src(tmp_path), pricing)
    inode = f.stat().st_ino

    # same inode, larger file, but the prefix we already consumed changed
    with f.open("r+", encoding="utf-8") as fh:
        fh.seek(0)
        fh.write(assistant_line("m9", req_id="r9", out=11) + assistant_line("m10", req_id="r10", out=5))
    assert f.stat().st_ino == inode
    report = sync_source(conn, collector, _src(tmp_path), pricing)
    assert report.resets and report.resets[0][1] == "rewritten"
    ids = {r[0] for r in conn.execute("SELECT message_id FROM events")}
    assert ids == {"m1", "m9", "m10"}


def test_replaced_file_new_inode_rescans_idempotently(conn, pricing, collector, tmp_path):
    f = write(_src(tmp_path) / "p" / "s.jsonl", assistant_line("m1", req_id="r1", out=1))
    sync_source(conn, collector, _src(tmp_path), pricing)

    # atomic replace (new inode) with the same history plus the final chunk and a new message
    tmp = write(f.with_suffix(".new"), assistant_line("m1", req_id="r1", out=1),
                assistant_line("m1", req_id="r1", out=90), assistant_line("m2", req_id="r2", out=5))
    os.replace(tmp, f)
    report = sync_source(conn, collector, _src(tmp_path), pricing)
    assert report.resets[0][1] == "replaced"
    assert _count(conn) == 2 and _sum_out(conn) == 95


def test_new_files_are_picked_up(conn, pricing, collector, tmp_path):
    write(_src(tmp_path) / "p" / "a.jsonl", assistant_line("m1", req_id="r1"))
    sync_source(conn, collector, _src(tmp_path), pricing)
    write(_src(tmp_path) / "q" / "b.jsonl", assistant_line("m2", req_id="r2"))
    report = sync_source(conn, collector, _src(tmp_path), pricing)
    assert report.files_scanned == 1 and report.files_unchanged == 1
    assert _count(conn) == 2


def test_empty_file_checkpoint(conn, pricing, collector, tmp_path):
    f = write(_src(tmp_path) / "p" / "empty.jsonl")
    sync_source(conn, collector, _src(tmp_path), pricing)
    cp = get_checkpoint(conn, "claude-code", str(f.resolve()))
    assert cp.offset == 0 and cp.head_len == 0 and cp.head_hash == ""
    write(f, assistant_line("m1", req_id="r1"), mode="a")
    sync_source(conn, collector, _src(tmp_path), pricing)
    assert _count(conn) == 1
