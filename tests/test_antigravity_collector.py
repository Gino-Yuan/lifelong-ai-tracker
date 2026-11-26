"""AntigravityCollector on fabricated fixtures only (tests/fixtures/antigravity).

Fixture layout (all IDs, paths and text are fake):
    brain/conv-fx-ag-0001/.system_generated/logs/transcript.jsonl       steps 1, 2, 5 (5 written twice)
    brain/conv-fx-ag-0002/.system_generated/logs/transcript.jsonl       step 1
    brain/conv-fx-ag-0002/.system_generated/logs/transcript_full.jsonl  9M-token duplicates: never read
    brain/conv-fx-ag-0002/transcript.jsonl                              decoy: wrong level
    brain/notes-fx/.system_generated/transcript.jsonl                   decoy: no logs/ level
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import CANARY, FIXTURES, TEST_PRICING
from lifelong_tracker import analytics
from lifelong_tracker.cli import app
from lifelong_tracker.collectors import AntigravityCollector, get_collector
from lifelong_tracker.storage import connect
from lifelong_tracker.sync import sync_source

AG_FIXTURES = FIXTURES / "antigravity" / "brain"
LOG = Path(".system_generated") / "logs" / "transcript.jsonl"
FAKE = Path("/fake/brain/conv-fx-unit/.system_generated/logs/transcript.jsonl")

# expected ledger totals for the fixtures
EXP = {
    "events": 4,
    "input_tokens": 27895 + 31000 + 1200 + 5000,
    "output_tokens": 3410 + 120 + 950 + 700,
    "thinking_tokens": 800,
    "cache_read_tokens": 0 + 24000 + 30000 + 1000,
    "cache_write_tokens": 500,
}


@pytest.fixture
def ag() -> AntigravityCollector:
    return AntigravityCollector()


@pytest.fixture
def ag_source(tmp_path) -> Path:
    dst = tmp_path / "brain"
    shutil.copytree(AG_FIXTURES, dst)
    return dst


def _sync(db_path, source, pricing, collector=None):
    with connect(db_path) as conn:
        return sync_source(conn, collector or AntigravityCollector(), source, pricing)


def _rows(db_path):
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY request_id, message_id")]


# ------------------------------------------------------------------ registry / discovery

def test_registered_and_ready():
    c = get_collector("antigravity")
    assert isinstance(c, AntigravityCollector) and c.implemented
    assert c.tool == "antigravity"


def test_discover_only_system_generated_transcripts(ag):
    found = [p.relative_to(AG_FIXTURES) for p in ag.discover(AG_FIXTURES)]
    assert found == [Path("conv-fx-ag-0001") / LOG, Path("conv-fx-ag-0002") / LOG]
    assert all(p.name == "transcript.jsonl" for p in found)


def test_discover_skips_symlinks(ag, ag_source, tmp_path):
    outside = tmp_path / "outside" / "conv-x" / ".system_generated" / "logs"
    outside.mkdir(parents=True)
    (outside / "transcript.jsonl").write_text("")
    link = ag_source / "conv-link" / ".system_generated" / "logs"
    link.mkdir(parents=True)
    (link / "transcript.jsonl").symlink_to(outside / "transcript.jsonl")
    (ag_source / "dirlink").symlink_to(tmp_path / "outside", target_is_directory=True)
    names = {p.parent.parent.parent.name for p in ag.discover(ag_source)}
    assert names == {"conv-fx-ag-0001", "conv-fx-ag-0002"}


# ------------------------------------------------------------------ record parsing

SAMPLE_MODEL = {
    "step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
    "created_at": "2026-11-25T15:14:16Z", "model": "gemini-4-neon-low", "input_tokens": 27895,
    "cache_read_tokens": 0, "output_tokens": 3410, "content": "REDACTED", "thinking": "REDACTED",
}
SAMPLE_USER = {
    "step_index": 0, "source": "USER", "type": "USER_INPUT", "status": "DONE",
    "created_at": "2026-11-25T15:14:10Z", "content": "REDACTED",
}


def test_maps_spec_sample(ag):
    ev = ag.parse_record(dict(SAMPLE_MODEL), FAKE)
    assert ev.tool == "antigravity"
    assert ev.session_id == ev.request_id == "conv-fx-unit"
    assert ev.message_id == "step_1"
    assert ev.timestamp_utc == "2026-11-25T15:14:16.000Z"
    assert ev.model == "gemini-4-neon-low"
    assert ev.project_name == "antigravity"
    assert (ev.input_tokens, ev.output_tokens, ev.cache_read_tokens) == (27895, 3410, 0)
    assert (ev.thinking_tokens, ev.cache_write_tokens) == (0, 0)


def test_reads_optional_thinking_and_cache_write(ag):
    ev = ag.parse_record({**SAMPLE_MODEL, "thinking_tokens": 42, "cache_write_tokens": 7}, FAKE)
    assert (ev.thinking_tokens, ev.cache_write_tokens) == (42, 7)


def test_missing_token_fields_default_to_zero(ag):
    rec = {k: v for k, v in SAMPLE_MODEL.items() if k not in ("input_tokens", "cache_read_tokens")}
    ev = ag.parse_record(rec, FAKE)
    assert (ev.input_tokens, ev.cache_read_tokens, ev.output_tokens) == (0, 0, 3410)


@pytest.mark.parametrize(
    "record",
    [
        SAMPLE_USER,
        {**SAMPLE_MODEL, "source": "TOOL"},
        {k: v for k, v in SAMPLE_MODEL.items() if not k.endswith("_tokens")},  # no token fields
        {**SAMPLE_MODEL, "input_tokens": "27895", "output_tokens": True, "cache_read_tokens": None},
        {**SAMPLE_MODEL, "input_tokens": 0, "output_tokens": 0},  # all-zero usage
        {k: v for k, v in SAMPLE_MODEL.items() if k != "step_index"},
        {**SAMPLE_MODEL, "step_index": -1},
        {**SAMPLE_MODEL, "step_index": 1.5},
        {k: v for k, v in SAMPLE_MODEL.items() if k != "created_at"},
    ],
)
def test_ignored_records(ag, record):
    assert ag.parse_record(dict(record), FAKE) is None


def test_missing_model_is_unknown_and_string_step_accepted(ag):
    rec = {k: v for k, v in SAMPLE_MODEL.items() if k != "model"}
    rec["step_index"] = "12"
    ev = ag.parse_record(rec, FAKE)
    assert ev.model == "unknown" and ev.message_id == "step_12"


def test_event_never_carries_text_fields(ag):
    rec = {**SAMPLE_MODEL, "content": CANARY, "thinking": CANARY,
           "tool_calls": [{"args": CANARY}], "media": [{"data": CANARY}]}
    ev = ag.parse_record(rec, FAKE)
    assert CANARY not in repr(ev)
    assert not hasattr(ev, "content") and not hasattr(ev, "__dict__")


# ------------------------------------------------------------------ end-to-end sync

def test_sync_totals_and_dedup(db_path, ag_source, pricing):
    report = _sync(db_path, ag_source, pricing)
    assert report.files_seen == 2 and report.files_scanned == 2
    assert report.bad_lines == 1  # the malformed line in conversation 1
    assert report.upsert.inserted == 4

    rows = _rows(db_path)
    assert [(r["request_id"], r["message_id"]) for r in rows] == [
        ("conv-fx-ag-0001", "step_1"), ("conv-fx-ag-0001", "step_2"),
        ("conv-fx-ag-0001", "step_5"), ("conv-fx-ag-0002", "step_1"),
    ]
    assert all(r["tool"] == "antigravity" and r["project_name"] == "antigravity" for r in rows)
    assert all(r["session_id"] == r["request_id"] for r in rows)

    step5 = rows[2]
    assert step5["output_tokens"] == 950  # rewritten step merged, final (larger) usage kept
    assert step5["timestamp"] == "2026-11-25T15:20:09.000Z"  # first time the step was seen
    assert step5["model"] == "gemini-4-neon-high"

    with connect(db_path) as conn:
        tot = analytics.summarize(conn, "all")
    for key, value in EXP.items():
        assert getattr(tot, key) == value, key
    assert tot.total_tokens == sum(v for k, v in EXP.items() if k != "events")


def test_transcript_full_is_never_opened(db_path, ag_source, pricing):
    full = ag_source / "conv-fx-ag-0002" / ".system_generated" / "logs" / "transcript_full.jsonl"
    os.chmod(full, 0)  # any attempt to read it would raise PermissionError
    try:
        report = _sync(db_path, ag_source, pricing)
    finally:
        os.chmod(full, 0o644)
    assert report.upsert.inserted == 4
    assert max(r["input_tokens"] for r in _rows(db_path)) < 9_000_000


def test_no_text_reaches_the_ledger(db_path, ag_source, pricing):
    _sync(db_path, ag_source, pricing)
    with sqlite3.connect(db_path) as raw:
        dump = "\n".join(raw.iterdump())
    assert CANARY not in dump
    for leaked in ("REDACTED", "fake_tool", "/Users/demo", "RkFLRV9NRURJQQ==", "image/png"):
        assert leaked not in dump


def test_resync_idempotent_and_incremental(db_path, ag_source, pricing):
    _sync(db_path, ag_source, pricing)
    again = _sync(db_path, ag_source, pricing)
    assert again.files_unchanged == 2 and again.upsert.inserted == 0

    log = ag_source / "conv-fx-ag-0002" / LOG
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 1, "source": "MODEL", "created_at": "2026-11-26T01:00:30Z",
                            "model": "gemini-4-neon-low", "input_tokens": 5000,
                            "cache_read_tokens": 1000, "output_tokens": 720, "content": CANARY}) + "\n")
        f.write(json.dumps({"step_index": 3, "source": "MODEL", "created_at": "2026-11-26T01:02:00Z",
                            "model": "gemini-4-neon-low", "input_tokens": 6000, "output_tokens": 50}) + "\n")
        f.write('{"step_index": 4, "source": "MODEL", "input_tok')  # half-written line
    inc = _sync(db_path, ag_source, pricing)
    assert (inc.files_scanned, inc.upsert.inserted, inc.upsert.updated) == (1, 1, 1)
    rows = {(r["request_id"], r["message_id"]): r for r in _rows(db_path)}
    assert rows[("conv-fx-ag-0002", "step_1")]["output_tokens"] == 720
    assert rows[("conv-fx-ag-0002", "step_1")]["timestamp"] == "2026-11-26T01:00:07.000Z"
    assert ("conv-fx-ag-0002", "step_4") not in rows
    assert len(rows) == 5


def test_same_step_in_two_conversations_stays_distinct(db_path, ag_source, pricing):
    _sync(db_path, ag_source, pricing)
    rows = _rows(db_path)
    step1 = [r for r in rows if r["message_id"] == "step_1"]
    assert {r["request_id"] for r in step1} == {"conv-fx-ag-0001", "conv-fx-ag-0002"}


def test_unpriced_gemini_models_are_flagged(db_path, ag_source, pricing):
    _sync(db_path, ag_source, pricing)
    with connect(db_path) as conn:
        unpriced = {u.model for u in analytics.unpriced_models(conn, pricing)}
    assert unpriced == {"gemini-4-neon-low", "gemini-4-neon-high"}


def test_cli_sync_and_sync_all(db_path, ag_source, tmp_path):
    runner = CliRunner()
    r = runner.invoke(app, ["sync", "--source", str(ag_source), "--tool", "antigravity",
                            "--db", str(db_path), "--pricing", str(TEST_PRICING)])
    assert r.exit_code == 0, r.output
    assert len(_rows(db_path)) == 4

    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({"sources": [{"name": "ag", "tool": "antigravity", "path": str(ag_source)}]}))
    db2 = tmp_path / "second" / "t.db"
    r = runner.invoke(app, ["sync-all", "--config", str(cfg), "--db", str(db2),
                            "--pricing", str(TEST_PRICING), "--quiet"])
    assert r.exit_code == 0, r.output
    assert "1 ok, 0 failed" in r.output and "4 new" in r.output
