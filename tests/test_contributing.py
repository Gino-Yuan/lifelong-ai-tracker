"""Open-source docs: LICENSE, CONTRIBUTING.md and the README sections stay present and true.

The collector examples in CONTRIBUTING.md are executed here against fabricated
data (fake ids, a content canary) in `tmp_path`, so the guide cannot drift from
the real base classes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from lifelong_tracker import setup_wizard
from lifelong_tracker.collectors import base as base_mod
from lifelong_tracker.collectors import sqlite_source
from lifelong_tracker.storage.db import connect
from lifelong_tracker.sync import sync_source

ROOT = Path(__file__).resolve().parents[1]
LICENSE = ROOT / "LICENSE"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
README = ROOT / "README.md"
CANARY = "FAKE_DOCS_CANARY_7f3e"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _python_block(doc: str, marker: str) -> str:
    blocks = [b for b in re.findall(r"```python\n(.*?)```", doc, re.S) if marker in b]
    assert len(blocks) == 1, f"expected one python block containing {marker!r}"
    return blocks[0]


def _load_example(marker: str) -> type:
    code = _python_block(_text(CONTRIBUTING), marker)
    ns: dict = {"__name__": "contributing_example"}
    exec(compile(code, str(CONTRIBUTING), "exec"), ns)  # noqa: S102 - our own docs
    return ns[marker]


# ------------------------------------------------------------------ LICENSE

def test_license_exists_and_is_mit():
    assert LICENSE.is_file()
    text = _text(LICENSE)
    assert text.startswith("MIT License\n")
    assert re.search(r"^Copyright \(c\) 2026 \S", text, re.M)
    for phrase in (
        "Permission is hereby granted, free of charge, to any person obtaining a copy",
        "The above copyright notice and this permission notice shall be included in all",
        'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND',
    ):
        assert phrase in text


def test_license_is_ascii_with_lf_endings():
    raw = LICENSE.read_bytes()
    raw.decode("ascii")
    assert b"\r" not in raw and raw.endswith(b"\n")


# ------------------------------------------------------------------ CONTRIBUTING.md

def test_contributing_exists_with_collector_guide():
    assert CONTRIBUTING.is_file()
    text = _text(CONTRIBUTING)
    assert "## How to Add a New Agent Collector" in text
    for name in ("BaseCollector", "JsonlCollector", "SqliteCollector", "COLLECTORS",
                 "connect_readonly", "set_trace_callback", "canary"):
        assert name in text, name
    for tool in ("Cursor", "Cline", "Gemini CLI"):
        assert tool in text, tool


def test_contributing_names_real_modules_and_classes():
    from lifelong_tracker import collectors

    assert issubclass(base_mod.JsonlCollector, base_mod.BaseCollector)
    assert issubclass(sqlite_source.SqliteCollector, base_mod.BaseCollector)
    assert hasattr(collectors, "COLLECTORS") and hasattr(setup_wizard, "_DEFAULTS")
    text = _text(CONTRIBUTING)
    for rel in ("collectors/base.py", "collectors/sqlite_source.py", "collectors/placeholders.py",
                "collectors/__init__.py", "setup_wizard.py", "web/static/app.js"):
        assert rel in text, rel
        assert (ROOT / "src" / "lifelong_tracker" / rel).is_file(), rel
    for rel in ("tests/test_codex_collector.py", "tests/fixtures/codex/build_codex_db.py",
                "tests/test_contributing.py"):
        assert (ROOT / rel).is_file(), rel


@pytest.mark.parametrize("marker", ["GeminiCliCollector", "CursorCollector"])
def test_collector_examples_fit_the_guideline_length(marker):
    lines = [ln for ln in _python_block(_text(CONTRIBUTING), marker).splitlines() if ln.strip()]
    assert 30 <= len(lines) <= 50, len(lines)


def test_jsonl_example_collects_metadata_only(tmp_path, pricing):
    cls = _load_example("GeminiCliCollector")
    assert issubclass(cls, base_mod.JsonlCollector)
    src = tmp_path / "gemini"
    (src / "s1").mkdir(parents=True)
    rows = [
        {"type": "user", "text": CANARY, "ts": "2026-11-20T10:00:00Z"},
        {"type": "response", "ts": "2026-11-20T10:00:05Z", "session": "fake-sess-1",
         "request_id": "fake-req-1", "model": "gemini-x", "text": CANARY, "tool_args": {"q": CANARY},
         "usage": {"input": 100, "output": 20, "thoughts": 5, "cached": 400}},
        {"type": "response", "ts": "2026-11-20T10:01:00Z", "request_id": "fake-req-2",
         "usage": {"input": 0, "output": 0}},
        {"type": "response", "request_id": "fake-req-3", "usage": {"input": 1}},  # no ts -> bad line
    ]
    log = src / "s1" / "session.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in rows) + '{"type": "resp', encoding="utf-8")

    scan = cls().read_incremental(log, None)
    assert scan.bad_lines == 1 and scan.lines_read == 4
    (event,) = scan.events
    assert (event.tool, event.model, event.session_id, event.request_id) == (
        "gemini-cli", "gemini-x", "fake-sess-1", "fake-req-1")
    assert (event.input_tokens, event.output_tokens, event.thinking_tokens,
            event.cache_read_tokens, event.cache_write_tokens) == (100, 20, 5, 400, 0)
    assert CANARY not in repr(event)

    db = tmp_path / "ledger.db"
    with connect(db) as conn:
        report = sync_source(conn, cls(), src, pricing)
        assert report.upsert.inserted == 1
        again = sync_source(conn, cls(), src, pricing)
        assert again.files_unchanged == 1
        dump = "\n".join(conn.iterdump())
    assert CANARY not in dump and "fake-req-1" in dump


def _cursor_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE usage_events (id TEXT PRIMARY KEY, created_ms INTEGER, updated_ms INTEGER,
            model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            prompt TEXT, response TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("fake-row-1", 1_763_600_000_000, 1_763_600_001_000, "cursor-fast", 50, 10, 200, CANARY, CANARY),
            ("fake-row-2", 1_763_600_100_000, None, None, 0, 0, 0, CANARY, CANARY),
            ("fake-row-3", None, 1_763_600_200_000, "cursor-fast", 5, 1, 0, CANARY, CANARY),
        ],
    )
    conn.commit()
    conn.close()


def test_sqlite_example_collects_metadata_only(tmp_path, pricing, monkeypatch):
    cls = _load_example("CursorCollector")
    assert issubclass(cls, sqlite_source.SqliteCollector)
    folder = tmp_path / "cursor"
    folder.mkdir()
    dbfile = folder / "usage.db"
    _cursor_db(dbfile)
    before = dbfile.read_bytes()

    statements: list[str] = []
    real = sqlite_source.connect_readonly

    def traced(path):
        conn = real(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite_source, "connect_readonly", traced)
    collector = cls()
    assert collector.discover(folder) == [dbfile]
    scan = collector.read_incremental(dbfile, None)
    assert scan.bad_lines == 1  # fake-row-3 has no creation time
    (event,) = scan.events
    assert (event.request_id, event.model, event.input_tokens, event.output_tokens,
            event.cache_read_tokens) == ("fake-row-1", "cursor-fast", 50, 10, 200)
    assert CANARY not in repr(event)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert selects
    for sql in selects:
        assert "*" not in sql.replace("COUNT(*)", "")
        assert "prompt" not in sql and "response" not in sql

    db = tmp_path / "ledger.db"
    with connect(db) as conn:
        assert sync_source(conn, cls(), folder, pricing).upsert.inserted == 1
        assert sync_source(conn, cls(), folder, pricing).files_unchanged == 1
        dump = "\n".join(conn.iterdump())
    assert CANARY not in dump
    assert dbfile.read_bytes() == before
    assert not (folder / "usage.db-journal").exists()


# ------------------------------------------------------------------ README

def test_readme_has_open_source_sections():
    text = _text(README)
    for heading in ("## Quick Start", "## Default log locations", "## Desktop App Mode",
                    "## Known Limitations & Non-Goals", "## Contributing", "## License"):
        assert re.search(rf"^{re.escape(heading)}\s*$", text, re.M), heading
    assert "](LICENSE)" in text and "](CONTRIBUTING.md)" in text


def test_readme_quick_start_uses_init_and_open_app():
    quick = _text(README).split("## Quick Start", 1)[1].split("\n## ", 1)[0]
    assert quick.index("pip install") < quick.index("tracker init") < quick.index("tracker open --app")


def test_readme_path_table_matches_the_init_wizard():
    table = _text(README).split("## Default log locations", 1)[1].split("\n## ", 1)[0]
    for platform in ("macos", "linux"):
        for d in setup_wizard.tool_defaults(platform):
            assert f"`{d.path}`" in table, (platform, d.path)
    for d in setup_wizard.tool_defaults("windows"):
        assert f"`{d.hint}`" in table, d.hint
    header = next(ln for ln in table.splitlines() if ln.startswith("| Tool"))
    assert all(os in header for os in ("macOS", "Linux", "Windows"))


def test_readme_desktop_app_mode_documents_app_window_and_dock():
    section = _text(README).split("## Desktop App Mode", 1)[1].split("\n## ", 1)[0]
    for phrase in ("tracker open --app", "--app=", "Chrome", "Edge", "Brave", "Arc",
                   "Safari", "File → Add to Dock", "default browser"):
        assert phrase in section, phrase


def test_readme_known_limitations_are_explicit():
    section = _text(README).split("## Known Limitations & Non-Goals", 1)[1].split("\n## ", 1)[0]
    for phrase in ("remaining quota", "burn rate", "offline", "OAuth", "API keys",
                   "state_5.sqlite", "90/8/2", "Cache Hit %",
                   "updated by hand", "Pricing", "effective_from", "`unknown`", "Set Price"):
        assert phrase in section, phrase


def test_readme_and_contributing_are_english_only():
    for path in (README, CONTRIBUTING):
        text = _text(path)
        assert not re.search(r"[぀-ヿ㐀-鿿가-힯]", text), path.name


def test_docs_contain_no_personal_paths():
    for path in (README, CONTRIBUTING, LICENSE):
        text = _text(path)
        assert "/Users/" not in text.replace("/Users/demo", ""), path.name
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), path.name
