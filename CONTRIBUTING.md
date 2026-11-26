# Contributing to Lifelong AI Tracker

Thanks for helping. Bug reports, collectors for new tools, and fixes are all welcome. This guide covers the rules every change has to follow, how to set up your machine, and how to add a collector for another coding agent.

## Ground rules

These rules are what make the project worth trusting. A pull request that breaks one of them will not be merged.

1. **Metadata only.** A collector copies only timestamps, model ids, session / request / message ids, the five token counts, and at most a project *basename*. Prompts, responses, thinking text, tool arguments, file contents and full paths are never read into an event, never stored, and never logged.
2. **Offline.** No network calls of any kind: no telemetry, no update checks, no remote price lists. The web server binds loopback addresses only.
3. **No implicit scanning.** A collector reads only the path it is handed (`--source`, or a `sources.json` entry). It never guesses a location on its own, never follows symlinks out of the source, and never writes to another tool's files.
4. **Read-only access to foreign databases.** Open SQLite files through `sqlite_source.connect_readonly()` (`mode=ro`, `query_only`, `trusted_schema = OFF`).
5. **Self-contained dashboard.** The UI text is in English. Use vanilla JS and inline SVG: no CDN, no web fonts, no Node.js build step, and nothing that loosens the strict Content-Security-Policy.
6. **Fabricated test data only.** Tests and fixtures never point at a real home folder (`~/.claude`, `~/.codex`, ...) or a real ledger (`data/tracker.db`). Build fixtures in `tests/fixtures/` or under pytest's `tmp_path`, with fake ids and fake paths.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

On Windows, use `.venv\Scripts\pip` and `.venv\Scripts\python`. The suite must pass on macOS, Linux and Windows. Tests that need macOS's JavaScriptCore shell (`jsc`) skip themselves elsewhere.

Before opening a pull request:

- add tests for the change, and make sure `pytest` passes;
- open text files with an explicit `encoding="utf-8"` (a test enforces this);
- write files atomically with `fsutil.atomic_write_text()`;
- update `README.md` when a command, endpoint or limitation changes.

## How to Add a New Agent Collector

A collector turns one tool's local usage logs into `UsageEvent`s. Everything after that is shared with the existing collectors: storage, dedup, pricing, the dashboard and `sync-all`. That shared part covers:

- the UPSERT on `(tool, request_id, message_id)`;
- incremental checkpoints;
- cost.

### 1. Pick a base class

| The tool stores usage as... | Subclass | You implement |
|---|---|---|
| append-only JSONL files (one JSON object per line) | `JsonlCollector` (`collectors/base.py`) | `parse_record(record, path)` |
| an SQLite database | `SqliteCollector` (`collectors/sqlite_source.py`) | `query`, `max_query`, `row_stamp(row)`, `parse_row(row)` |
| anything else | `BaseCollector` (`collectors/base.py`) | `discover(source)` and `read_incremental(path, checkpoint)` |

Both concrete bases give you incremental scans for free:

- `JsonlCollector` handles byte offsets, half-written last lines, and detecting a replaced, truncated or rewritten file.
- `SqliteCollector` handles the read-only URI, the file + `-wal` signature, and the high-water mark.

**Confirm the real format first.** Start from one or two *redacted* sample records, and never guess at a format by probing a real user folder. Until a sample exists, register a placeholder (see `collectors/placeholders.py`) that refuses to scan.

### 2a. Example: a JSONL tool (e.g. Gemini CLI)

The field names below are **hypothetical**. Map them to what the real sample shows.

```python
from pathlib import Path
from typing import Any

from lifelong_tracker.collectors.base import JsonlCollector
from lifelong_tracker.collectors.sqlite_source import tok
from lifelong_tracker.models import UsageEvent, parse_iso_utc


class GeminiCliCollector(JsonlCollector):
    """Gemini CLI session logs (hypothetical format - confirm with a redacted sample).

    Whitelisted fields: ts, session, request_id, model, usage.{input, output, thoughts, cached}.
    `text`, `prompt`, `tool_args` and every other field are never read.
    """

    tool = "gemini-cli"
    display_name = "Gemini CLI"
    pattern = "*.jsonl"  # discover() walks --source for these; symlinks are skipped

    def parse_record(self, record: dict[str, Any], path: Path) -> UsageEvent | None:
        usage = record.get("usage")
        if record.get("type") != "response" or not isinstance(usage, dict):
            return None  # not a model response: no event
        tokens = {
            "input_tokens": tok(usage.get("input")),
            "output_tokens": tok(usage.get("output")),
            "thinking_tokens": tok(usage.get("thoughts")),
            "cache_read_tokens": tok(usage.get("cached")),
        }
        if not any(tokens.values()):
            return None  # all-zero usage is skipped
        request_id = str(record.get("request_id") or "")
        return UsageEvent(
            timestamp=parse_iso_utc(record["ts"]),  # a missing / bad ts counts as a bad line
            tool=self.tool,
            model=str(record.get("model") or "unknown"),
            project_name="gemini-cli",  # a basename at most, never a full path
            session_id=str(record.get("session") or ""),
            request_id=request_id,
            message_id=request_id,
            **tokens,
        )
```

If `parse_record` raises `ValueError`, `TypeError`, `KeyError` or `AttributeError`, that line is counted as a bad line and the sync moves on. Return `None` for lines that carry no usage.

### 2b. Example: an SQLite tool (e.g. Cursor or Cline)

The table and column names below are **hypothetical**. Select only metadata columns. If usage sits inside a JSON column, pull out just the keys you need with SQLite's `json_extract`, as `opencode.py` does, so that the document itself never reaches Python.

```python
from typing import Any

from lifelong_tracker.collectors.sqlite_source import SqliteCollector, ms_to_utc, to_ms, tok
from lifelong_tracker.models import UsageEvent

_STAMP = "COALESCE(updated_ms, created_ms, 0)"  # change stamp in epoch ms


class CursorCollector(SqliteCollector):
    """Cursor-style usage table (hypothetical schema - confirm with a redacted sample).

    Only the columns named in `query` are ever selected; `prompt` / `response` never are.
    """

    tool = "cursor"
    display_name = "Cursor"
    db_name = "usage.db"  # --source may be this file or the folder that contains it
    table = "usage_events"
    required_columns = frozenset(
        {"id", "created_ms", "updated_ms", "model", "input_tokens", "output_tokens", "cache_read_tokens"})
    product = "a Cursor"  # used in error messages
    query = f"""SELECT id, created_ms, updated_ms, model, input_tokens, output_tokens, cache_read_tokens
                FROM usage_events WHERE {_STAMP} >= ? ORDER BY {_STAMP}, id"""
    max_query = f"SELECT MAX({_STAMP}) FROM usage_events"

    def row_stamp(self, row: tuple[Any, ...]) -> int:
        return to_ms(row[2]) or to_ms(row[1]) or 0

    def parse_row(self, row: tuple[Any, ...]) -> UsageEvent | None:
        row_id, created, _updated, model, inp, out, cached = row
        tokens = {"input_tokens": tok(inp), "output_tokens": tok(out), "cache_read_tokens": tok(cached)}
        if not any(tokens.values()):
            return None
        ms = to_ms(created)
        if ms is None:
            raise ValueError("no usable timestamp")  # counted as a bad row; the sync continues
        return UsageEvent(
            timestamp=ms_to_utc(ms), tool=self.tool, model=str(model or "unknown"),
            project_name="cursor", request_id=str(row_id), message_id=str(row_id), **tokens,
        )
```

`query` must keep its `WHERE <stamp> >= ?` filter. The base class passes in the high-water mark, so each sync reads only new or updated rows. A row read twice merges through the UPSERT.

Neither example is invented from nothing. `tests/test_contributing.py` runs both against fabricated data, so they stay correct as the code changes.

### 3. Register it

1. Put the module in `src/lifelong_tracker/collectors/<tool>.py` and add the class to `COLLECTORS` in `collectors/__init__.py`. The order there is the order the dashboard shows tools in. `sources.json`, `tracker sync --tool`, `tracker collectors` and the subscriptions editor pick the tool up from this registry.
2. Optional, but expected for a finished collector:
   - give the tool a fixed colour in `TOOL_COLORS` (`web/static/app.js`);
   - add its default log location to `_DEFAULTS` in `setup_wizard.py`, so `tracker init` offers it;
   - add a row to the path table in `README.md`, and a section describing exactly which fields are read.
3. Add the tool's models to `pricing.json` and to `src/lifelong_tracker/default_pricing.json` (the two must stay identical), with prices from the vendor's public price list.

### 4. Privacy and redaction tests

Every collector ships with tests built on fabricated fixtures:

- **Fixtures.** Put them in `tests/fixtures/<tool>/`. Use fake ids and fake paths, like `/Users/demo/...`. For SQLite, build the database in code, like `tests/fixtures/codex/build_codex_db.py`, rather than committing a binary.
- **A content canary.** Put a unique string (e.g. `FAKE_CURSOR_CANARY_1234`) into every content-like field: prompts, responses, thinking, tool arguments, titles, full paths. Then assert that it appears nowhere in the parsed events, in a dump of the ledger after `sync_source()`, or in the CSV / JSON export.
- **The SQL actually run (SQLite tools).** Wrap `sqlite_source.connect_readonly` with `conn.set_trace_callback(...)` and assert that no `SELECT *` and no content column appears in any statement (see `test_codex_collector.py`).
- **Read-only.** After a sync, the source database is byte-for-byte unchanged, and no `-journal` file appears next to it.
- **Incremental behaviour.** Cover all of these:
  - appending new lines or rows (only the new ones are read);
  - a half-written last line;
  - a file that is truncated or replaced;
  - an unchanged source (skipped without being opened);
  - a resync that changes nothing (no duplicate rows).
- **Malformed input.** Bad JSON, missing fields, negative or non-numeric counts and all-zero usage are skipped or counted as bad lines. None of them makes the sync fail.

## Reporting a bug

Describe what you ran, what you expected, and what happened. If a log format looks wrong, attach a **redacted** record:

- replace every prompt, response, path and id with a placeholder;
- keep only the field names and the numbers.

Please never attach a real `tracker.db`, `sources.json` or `subscriptions.json`.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
