import json
from pathlib import Path

import pytest

from conftest import CLAUDE_FIXTURES
from lifelong_tracker.collectors import COLLECTORS, get_collector
from lifelong_tracker.collectors.placeholders import T3Collector

FAKE = Path("/fake/projects/-Users-demo-x/s.jsonl")


def _rec(**over):
    rec = {
        "type": "assistant",
        "timestamp": "2026-01-02T03:04:05.678Z",
        "sessionId": "sess-1",
        "requestId": "req-1",
        "cwd": "/Users/demo/Projects/gamma",
        "message": {
            "id": "msg-1",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "FAKE_CONTENT_CANARY_7f3a"}],
            "usage": {"input_tokens": 3, "output_tokens": 7,
                      "cache_read_input_tokens": 11, "cache_creation_input_tokens": 13},
        },
    }
    rec.update(over)
    return rec


def test_parses_whitelisted_metadata_only(collector):
    ev = collector.parse_record(_rec(), FAKE)
    assert ev.tool == "claude-code"
    assert ev.model == "claude-sonnet-4-5"
    assert ev.project_name == "gamma"
    assert ev.session_id == "sess-1"
    assert (ev.request_id, ev.message_id) == ("req-1", "msg-1")
    assert (ev.input_tokens, ev.output_tokens, ev.cache_read_tokens, ev.cache_write_tokens) == (3, 7, 11, 13)
    assert ev.thinking_tokens == 0
    assert ev.timestamp_utc == "2026-01-02T03:04:05.678Z"
    assert "CANARY" not in repr(ev)


@pytest.mark.parametrize(
    "record",
    [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "summary", "summary": "x"},
        _rec(message={"id": "m", "model": "x"}),  # no usage
        _rec(message={"id": "m", "model": "<synthetic>", "usage": {"input_tokens": 5}}),
        _rec(message={"id": "m", "model": "x", "usage": {"input_tokens": 0, "output_tokens": 0}}),
        _rec(timestamp=None),
    ],
)
def test_ignores_records_without_billable_usage(collector, record):
    assert collector.parse_record(record, FAKE) is None


def test_cache_creation_breakdown_fallback(collector):
    rec = _rec()
    rec["message"]["usage"] = {
        "input_tokens": 1, "output_tokens": 1,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 50},
    }
    assert collector.parse_record(rec, FAKE).cache_write_tokens == 150


def test_missing_ids_fall_back_to_line_uuid(collector):
    rec = _rec(uuid="line-uuid-9")
    rec.pop("requestId")
    rec["message"].pop("id")
    ev = collector.parse_record(rec, FAKE)
    assert ev.message_id == "uuid:line-uuid-9" and ev.request_id == ""


def test_project_falls_back_to_parent_dir(collector):
    rec = _rec()
    rec.pop("cwd")
    assert collector.parse_record(rec, FAKE).project_name == "-Users-demo-x"


def test_garbage_token_values_are_clamped(collector):
    rec = _rec()
    rec["message"]["usage"] = {"input_tokens": -5, "output_tokens": "7", "cache_read_input_tokens": True,
                               "cache_creation_input_tokens": 2.0}
    ev = collector.parse_record(rec, FAKE)
    assert (ev.input_tokens, ev.output_tokens, ev.cache_read_tokens, ev.cache_write_tokens) == (0, 0, 0, 2)


def test_full_fixture_file_scan(collector):
    path = CLAUDE_FIXTURES / "-Users-demo-Projects-alpha" / "0f1e2d3c-0000-4000-8000-00000000a1a1.jsonl"
    scan = collector.read_incremental(path, None)
    assert scan.lines_read == 9
    assert scan.bad_lines == 1  # the truncated JSON line
    # 3 streaming chunks of A001 + A002 + A003 (user/summary/synthetic skipped)
    assert [e.message_id for e in scan.events] == ["msg_fx_A001"] * 3 + ["msg_fx_A002", "msg_fx_A003"]
    assert scan.state.offset == path.stat().st_size


def test_discover_is_limited_to_source_and_skips_symlinks(collector, tmp_path):
    src = tmp_path / "src"
    (src / "p").mkdir(parents=True)
    (src / "p" / "a.jsonl").write_text("", encoding="utf-8")
    (src / "p" / "notes.txt").write_text("", encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    (src / "p" / "link.jsonl").symlink_to(outside)
    assert [p.name for p in collector.discover(src)] == ["a.jsonl"]


def test_registry_and_placeholders(tmp_path):
    assert set(COLLECTORS) == {"claude-code", "antigravity", "t3", "opencode", "codex"}
    assert get_collector("claude-code").implemented
    with pytest.raises(ValueError):
        get_collector("nope")
    assert get_collector("opencode").implemented
    for cls in (T3Collector,):
        c = cls()
        assert not c.implemented and c.required_fields
        with pytest.raises(NotImplementedError, match="redacted sample"):
            c.discover(tmp_path)
        with pytest.raises(NotImplementedError):
            c.read_incremental(tmp_path / "x", None)
