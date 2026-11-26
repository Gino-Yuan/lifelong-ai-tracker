# Lifelong AI Tracker

A local, offline ledger of AI coding-tool token usage: Claude Code, Antigravity, Codex and OpenCode. It runs on Python 3.11+ and SQLite on macOS, Linux and Windows. It comes with:

- a dashboard on your own machine;
- CSV / JSON export;
- nightly archiving and backups;
- a shareable SVG report card.

Three guarantees:

- **Reads only what you name.** It reads only the paths you configure (`sources.json` or `--source`). There is no hidden default scan location.
- **Metadata only.** It stores the timestamp, tool, model, project basename, session / request / message ids, and the five token counts. Conversation text is never stored.
- **No network.** It makes no network calls, and the dashboard binds loopback addresses only. The ledger is written to `./data/tracker.db`, which is git-ignored.

## Quick Start

```bash
git clone <this-repo-url> lifelong-ai-tracker
cd lifelong-ai-tracker
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/tracker init
.venv/bin/tracker open --app
```

On Windows, use `py -m venv .venv`, then `.venv\Scripts\pip install -e .`, `.venv\Scripts\tracker init` and `.venv\Scripts\tracker open --app`.

1. **`tracker init`** asks, one tool at a time, whether you use it. Answer *n* for tools you don't have. For each *y*:
   - press Enter to accept the usual log location (see the table below) or type your own;
   - choose `subscription` (a flat plan) or `api` (a pay-as-you-go API key).

   The answers are written to `sources.json`. It never overwrites an existing file unless you pass `--force`. It does not check whether the paths exist; the first sync reports any that are missing.
2. **`tracker open --app`** does four things:
   - syncs every enabled source (incrementally, so later runs take a moment);
   - starts the dashboard on `http://127.0.0.1:8765`;
   - opens it once the server is ready (see [Desktop App Mode](#desktop-app-mode));
   - stops on Ctrl+C.

   Other options: `--no-sync` skips the sync, `--no-browser` only starts the server, and `--port` picks another port.

You can also install with `pip install .` (not editable). The tracker then keeps `data/`, `sources.json` and `pricing.json` in the folder you run it from. Until you save your first price edit, it uses the price table bundled with the package.

To keep the ledger growing while you're away, see [`schedule render`](#phase-3-scheduled-archiving-backups-report-card). It generates a nightly job, which you install yourself.

## Default log locations

These are the paths `tracker init` suggests. The tracker never looks at them on its own: only paths you confirm end up in `sources.json`. If a tool keeps its data somewhere else on your machine, enter that path instead.

| Tool | `tool` id | macOS | Linux | Windows | Source type |
|---|---|---|---|---|---|
| Claude Code | `claude-code` | `~/.claude/projects` | `~/.claude/projects` | `%USERPROFILE%\.claude\projects` | folder of session JSONL files |
| Antigravity | `antigravity` | `~/.gemini/antigravity/brain` | `~/.gemini/antigravity/brain` | `%USERPROFILE%\.gemini\antigravity\brain` | folder of conversations |
| Codex | `codex` | `~/.codex/state_5.sqlite` | `~/.codex/state_5.sqlite` | `%USERPROFILE%\.codex\state_5.sqlite` | SQLite file (or its folder) |
| OpenCode | `opencode` | `~/.local/share/opencode/opencode.db` | `~/.local/share/opencode/opencode.db` | `%LOCALAPPDATA%\opencode\opencode.db` | SQLite file (or its folder) |

`sources.json` expands `~` but not `%VARS%`, so `tracker init` stores the Windows paths in `~` form, such as `~/AppData/Local/opencode/opencode.db`. Relative paths are resolved against the folder `sources.json` is in. The SQLite sources are always opened read-only.

## Desktop App Mode

**`tracker open --app`** shows the dashboard in its own window, with no tabs or address bar, so it behaves like a desktop app.

- **Which browser:** it looks for an installed Chromium-family browser in this order: Chrome → Edge → Brave → Arc → Chromium. It starts the first one it finds with `--app=http://127.0.0.1:8765`.
- **Where it looks:**
  - macOS: `/Applications` and `~/Applications`;
  - Windows: the usual Program Files / `AppData\Local` install folders, then `PATH`;
  - Linux: `PATH`.
- **Fallback:** if no such browser is found or it fails to start, the dashboard opens in your default browser instead. Without `--app` it always uses the default browser.
- **Stopping:** the window is only a view, and the server keeps running in the terminal. Closing the window does not stop it. Press Ctrl+C in the terminal to stop it.

**A Dock icon on macOS (Safari 17+, macOS Sonoma or later):**

1. Start the server: `.venv/bin/tracker open --no-browser`. Use the same `--port` every time, because the Dock app remembers the full URL.
2. Open `http://127.0.0.1:8765` in Safari.
3. Choose **File → Add to Dock…** and confirm the name.

The dashboard now has its own Dock icon and window. The icon opens the page; it does not start the server. So run `tracker open --no-browser` (or `tracker serve`) first; if the server isn't running, the window shows a connection error.

Chrome and Edge offer the same thing from their menu (install the page as an app). That works on every OS.

## Known Limitations & Non-Goals

These limits are deliberate trade-offs, not bugs that are waiting to be fixed.

1. **No official remaining quota and no burn rate.** The tracker cannot show:
   - how much of a plan's quota you have left;
   - rate-limit windows or the countdown to the next reset;
   - how fast you are using up an allowance.

   Vendors publish these only through their online services, which need your logged-in session. The tracker stays 100% offline and makes no outside network requests. It never reads OAuth tokens, API keys, keychains or other login credentials. What you see is the usage it recorded from local logs, priced at list prices. *Plan Spent & ROI* is computed from the plan periods you enter yourself.
2. **Codex `state_5.sqlite` has no cache breakdown.** Codex stores only a running token total for each thread. It has no per-request rows and no input / output / reasoning / cache split. So for each thread:
   - **One event:** the tracker records one event with the whole total as input tokens.
   - **Cost:** it estimates the cost at a 90/8/2 blended rate (`0.90 × cache_read + 0.08 × input + 0.02 × output` per 1M tokens).
   - **Cache Hit %:** these rows are left out of the global Cache Hit %. When only Codex is in scope, the card shows `—`.
   - **Timing:** the whole thread is counted on the day and hour it started.

   Treat Codex cost as an estimate. Its daily and hourly shape is coarse. See [Codex collector](#codex-collector).
3. **Model prices are updated by hand.** `pricing.json` is a snapshot of public list prices, and its date is in `_meta.updated`. The tracker never downloads a newer one. When a vendor changes prices, you have three options:
   - edit the model in the dashboard (**Pricing** → **Edit** or **+ Add Model**, then **Save & Reprice**);
   - edit `pricing.json` and run `tracker reprice`;
   - add a dated segment with `effective_from` (see [Dated prices](#key-semantics)), so usage before the change keeps the old rate.

   Repricing rewrites only `cost_usd`; raw token counts are never touched. Earlier versions shipped a guessed price for the model name `unknown`, which is used when a tool logs no model name. That entry is gone. If your ledger has `unknown` rows, they now show under *Unpriced*. Click **Set Price** on them if you want an estimate.

**Non-goals:**

- **Not a live monitor.** Data appears when you sync: `tracker open`, **Sync Now**, or the nightly job.
- **Not your invoice.** Costs are list-price estimates. Discounts, credits, taxes and batch pricing are not modelled.
- **No cloud sync and no account.** Every ledger is local. Merging several machines is not supported.
- **Only tools that keep local logs.** Chat in a web browser, or a tool that keeps usage only on a server, cannot be tracked.

## Layout

```
LICENSE                          MIT
CONTRIBUTING.md                  ground rules + how to add a collector for another agent
pricing.json                     per-model USD / 1M tokens (editable; `tracker reprice` applies it)
sources.example.json             template for sources.json (git-ignored) used by `sync-all` and Sync Now
subscriptions.example.json       template for subscriptions.json (git-ignored): dated plan periods per tool
pyproject.toml                   deps: typer, rich (+ pytest for dev); console script `tracker`
data/tracker.db                  the ledger (created on first sync, git-ignored)
backups/ logs/ deploy/           backups, scheduler log, generated job files (all git-ignored)
src/lifelong_tracker/
  config.py                      default paths (project root in a source checkout, else the current folder)
  default_pricing.json           bundled copy of pricing.json (fallback for a non-editable install)
  fsutil.py                      63-bit inode, atomic UTF-8 writes, Windows-safe replace / chmod
  setup_wizard.py                `tracker init`: per-OS default paths, sources.json builder (never touches tool paths)
  launcher.py                    `tracker open`: Chromium-family app window detection, loopback-only opening
  models.py                      UsageEvent, FileState, UTC timestamp helpers
  pricing.py                     normalisation + longest-prefix price lookup + model_family()
  subscriptions.py               subscriptions.json periods: load / validate / atomic save + overlap-day ROI
  storage/schema.sql             DDL: events, sync_checkpoints, daily_stats (view), schema_meta
  storage/db.py                  WAL connection ctx-manager, UPSERT, checkpoints, reprice, drift check
  collectors/base.py             BaseCollector ABC + JsonlCollector (incremental reader)
  collectors/claude_code.py      Claude Code JSONL parser (whitelisted fields only)
  collectors/antigravity.py      Antigravity <conv>/.system_generated/logs/transcript.jsonl parser
  collectors/sqlite_source.py    shared read-only SQLite base: ro URI, signature + high-water mark
  collectors/opencode.py         OpenCode opencode.db reader (json_extract of metadata only)
  collectors/codex.py            Codex state_5.sqlite reader (threads metadata columns only)
  collectors/placeholders.py     T3Collector (refuses until samples exist)
  sync.py                        collector -> storage, one transaction per file
  sources.py                     sources.json loader + fault-tolerant sync_all
  backup.py                      sqlite3 backup() API, integrity check, --keep rotation
  schedule.py                    launchd plist / systemd unit + timer / crontab line / PowerShell task scripts + install commands (text only)
  card.py                        pure-Python SVG report card (no paths / project names)
  analytics.py                   totals, ratios, ranges, breakdowns, 24h, streaks, unpriced, cache savings, records
  cli.py                         `tracker init | open | sync | sync-all | stats | reprice | serve | card | backup | schedule render | collectors`
  web/app.py                     FastAPI JSON API (read-only GETs + guarded POSTs: pricing, subscriptions, sync) + static files
  web/security.py                loopback-only bind check, Host-header guard, CSP headers
  web/static/                    index.html, app.css, app.js (vanilla JS + inline SVG)
tests/
  fixtures/claude_code/…         fabricated JSONL (fake IDs/paths, content canary)
  fixtures/antigravity/brain/…   fabricated Antigravity transcripts (+ transcript_full / decoys)
  fixtures/opencode/build_opencode_db.py  builds a fabricated opencode.db in code (fake IDs, content canary)
  fixtures/codex/build_codex_db.py        builds a fabricated state_5.sqlite in code (fake IDs, content canary)
  fixtures/pricing_test.json     frozen test prices
  fixtures/js/stub_dom.js        shared JavaScriptCore stub DOM (fetch is routed to the scenario)
  fixtures/js/pricing_smoke.js   click-through of the Model Pricing modal
  fixtures/js/features_smoke.js  KPIs, records, group switch, shortcuts, Sync Now, Subscriptions tab
  test_*.py                      1222 tests (77 Phase 1 + 68 web + 40 Phase 3 + 24 Antigravity + 221 filters / pricing + 61 trend + 56 OpenCode + 57 Codex + 43 pricing table + 114 enhancements + 93 subscription periods + 26 Codex thread totals / estimates + 164 custom ranges / period-over-period + 68 cross-platform schedules / API billing / dated prices + 38 portability / packaging + 57 init / open + 15 docs / collector examples)
```

## Key semantics

**Dedup and streaming.** Each event is unique on `(tool, request_id, message_id)`. Claude Code writes one line per content block of a response, and each line carries a cumulative usage snapshot. New lines are merged with `INSERT … ON CONFLICT DO UPDATE SET col = MAX(col, excluded.col)`, plus `MIN(timestamp)`. This merge:
- keeps the final, complete usage no matter what order the lines arrive in;
- never sums the chunks;
- never keeps only the first placeholder chunk.

Replays of the same message in a resumed session's file collapse into the existing row. When nothing would change, the `DO UPDATE … WHERE` guard leaves the row untouched.

**Incremental scan.** Each file has a checkpoint in `sync_checkpoints`, made of inode, size, mtime_ns, byte offset, and a sha256 of the first ≤4 KiB.
- Only newline-terminated lines are consumed. A half-written last line waits for the next sync.
- If inode, size, and mtime are all unchanged, the file is skipped without being opened.
- If the inode is new, the file shrank below the offset, or the head hash changed, the file is rescanned from 0. This is safe because the UPSERT is idempotent.
- The events and the new checkpoint of each file commit in one transaction.
- The ledger is lifelong: rows are never deleted when a source log is rotated away.

**Cost.** `events.cost_usd` = tokens × `pricing.json` price. It is NULL for unpriced models.
- **Dated prices.** A model entry is either one price object (valid for every date) or a list of segments with `effective_from`, e.g. `"claude-x": [{"effective_from": "2026-01-01", "input": 2.0, ...}, {"effective_from": "2026-12-01", "input": 4.0, ...}]`. Each event is priced by the segment in force on its **UTC date** (the latest `effective_from` on or before it); the first segment also covers earlier days and may omit `effective_from`. Dates must be real and strictly increasing. Lookups without a date (the price table, Set Price) use the latest segment. Ingest, `reprice`, the drift check and Cache Saved all follow the dates. Editing a dated model in the dashboard changes only its latest segment and keeps the history.
- `tracker reprice` (or **Set Price** in the dashboard) rewrites only `cost_usd`. Raw tokens are never touched.
- Model matching: exact name, then longest prefix at a segment boundary (date suffixes), then the same with one reasoning-tier suffix stripped (`-xhigh`, `-high`, `-medium`, `-low`, `-tiered-high`, `-tiered-low`). So one `gemini-4-neon` entry prices `gemini-4-neon-low/-high/-xhigh`. An explicit variant entry always wins, and a different version number never matches (`gemini-5-neon-high`, `gemini-4-5-high` and `claude-opus-4-9-high` stay unpriced).
- `stats` warns when stored costs drift from `pricing.json`.
- `stats` lists unpriced models in red at the end.

**Metrics.**
- Cache hit % = CacheRead / (Input + CacheRead + CacheWrite). Codex thread totals with no cache breakdown (`thread_total`, cache read 0) are left out, so they don't drag the All Tools rate down. When every row in scope is one of them, the rate is `null` and the card shows `—` with *No cache breakdown*.
- Output share = Output / Total.
- $/M tokens = cost / priced tokens.
- Every ratio returns 0 when its denominator is 0.
- Days and hours use local time.

## Commands

```bash
.venv/bin/tracker init [--out sources.json] [--force]
.venv/bin/tracker open [--app] [--no-sync] [--no-browser] [--host 127.0.0.1] [--port 8765] [--sources sources.json] [--db PATH]
.venv/bin/tracker sync --source <dir> [--tool claude-code] [--db PATH] [--pricing PATH]
.venv/bin/tracker sync --tool opencode --source <opencode.db or its folder>
.venv/bin/tracker sync --tool codex --source <state_5.sqlite or its folder>
.venv/bin/tracker stats [--range today|7d|30d|year|all] [--top N]
.venv/bin/tracker reprice
.venv/bin/tracker serve [--host 127.0.0.1] [--port 8765] [--db PATH] [--sources sources.json] [--subscriptions subscriptions.json]
.venv/bin/tracker collectors
.venv/bin/tracker sync-all --config sources.json [--db PATH] [--quiet] [--backup-dir backups --keep 14]
.venv/bin/tracker backup [--db PATH] [--out-dir backups] [--keep 14]
.venv/bin/tracker card [--range lifelong|ytd|90d|30d|7d|today] [--out report.svg]
.venv/bin/tracker schedule render --config sources.json [--platform macos|linux|windows] [--out deploy/<label>.plist|.service|.ps1] [--hour 23 --minute 30] [--no-backup]
.venv/bin/python -m pytest
```

## Web dashboard (Phase 2)

`tracker serve` runs in the foreground (stop it with Ctrl+C). It only binds a loopback address (`127.0.0.1`, `::1`, or `localhost`); any other `--host` is refused before anything starts.

- **Read-only GETs.** Each GET opens the ledger with `PRAGMA query_only`. A missing database shows an empty dashboard and is not created.
- **Three write endpoints, one guard.** `POST /api/pricing`, `POST /api/subscriptions` and `POST /api/sync` are the only writes; everything below applies to all three.
- **Pricing.** `POST /api/pricing` writes one `pricing.json` entry (and stamps `_meta.updated` with today's date) (temp file + fsync + `os.replace`; the whole new file is validated first) and then reprices, which updates only `cost_usd`. It accepts only `Content-Type: application/json` bodies of 4 KiB or less. A cross-site `Origin` or `Sec-Fetch-Site` gets 403, so another web page in your browser cannot call it.
- **Offline.** There are no CDNs, web fonts, or build step. The Content-Security-Policy is `default-src 'none'` with only same-origin script, style, and fetch allowed, so the page cannot contact any other host.
- **Swagger/ReDoc disabled.** They load assets from a CDN.
- **Host-header guard.** Requests whose `Host` is not loopback get 403, which blocks DNS-rebinding.

| Endpoint | Parameters |
|---|---|
| `GET /api/tools` | tools that have data, in registry order (`claude-code`, `antigravity`, ...) with labels and totals |
| `GET /api/overview` | `range=today\|7d\|30d\|90d\|ytd\|last_year\|lifelong\|custom` - KPIs, five categories, streak, unpriced models (+ `base_model`), reprice alert, `cache_savings`, `records`, `subscription` (ROI or null, with `api_direct_spend_usd`), `subscription_error`, `billing` (API vs subscription split, or null) and `comparison` (see below) |
| `GET /api/timeseries` | `range`, `granularity=auto\|day\|week\|month`, `split_by=none\|tool` - `days` (daily, unchanged), `points` (zero-filled calendar buckets: day, Monday-start ISO week, or month; `auto` = daily ≤ 120 days, weekly ≤ 2 years, else monthly), and with `split_by=tool` a `series` list (one per agent active in scope, identical bucket axis, sums to `points`) |
| `GET /api/breakdown` | `by=model\|tool\|project`, `range`, `limit`, `group_variants=false\|true` - ranking with token / cost share. With `by=model&group_variants=true` rows are model families (tier and date suffixes stripped) with `variants` / `variant_count`; the limit applies after merging. Ignored for other `by` values |
| `GET /api/heatmap` | `year` - every day of the year (local time) |
| `GET /api/hourly` | `range` - 24 local-hour buckets |
| `GET /api/export` | `format=csv\|json`, `range` - per-event metadata rows as a download |
| `GET /api/card` | `range`, `download=1` - the SVG report card (`image/svg+xml`) |
| `GET /api/pricing` | the current price table: `currency`, `unit` (`per_million_tokens`), `unit_label`, `updated` (`_meta.updated`, else the file's date), `count`, `used_count`, `providers` (Claude / Gemini / GPT / Other with counts) and `models` (`model, provider, input, output, thinking, cache_read, cache_write` at the latest price, `used` = the ledger has events priced by this row (tier variants count for their base row), `effective_from` of that price or null, and `history` = the older dated prices), grouped by provider then by name (digit runs compare as numbers). Read-only; no file paths |
| `POST /api/pricing` | JSON `{model, input, output, thinking?, cache_read, cache_write}` (USD / 1M, each ≥ 0) - saves to `pricing.json` and reprices. Unknown fields, negative or non-numeric values, and bad names return 422 |
| `GET /api/subscriptions` | per registered tool: `periods` (`start`, `end` or null, `monthly_usd`, `ongoing`), `legacy` (an undated number), `monthly_usd` in force today; plus `today`, `writable`, `configured`, `error` (a broken file), `days_per_month`, `max_monthly_usd`, `max_periods`. Read-only |
| `POST /api/subscriptions` | JSON `{subscriptions: {tool: [{start, end, monthly_usd}, ...] \| USD}}` - known tool ids only, `start` `YYYY-MM-DD`, `end` `YYYY-MM-DD` ≥ start or null (ongoing), fee 0 to 10,000, at most 48 periods per tool, no overlapping periods within a tool. The legacy `{fees: {tool: USD}}` body is still accepted. Replaces `subscriptions.json` atomically (top-level `_` keys are kept). 422 on bad values, 503 when the server has no subscriptions path |
| `POST /api/sync` | body `{}` - runs `sync_all` over `sources.json` (the workspace one, or `serve --sources PATH`). One run at a time (a second press gets 409). A missing file returns `status: not_configured` with instructions; an invalid file returns 422. Otherwise `status` `ok \| partial \| failed`, per-source rows and `by_tool` new / updated counts |

**Custom range.** Every endpoint that takes `range` (overview, timeseries, breakdown, hourly, export, card) also accepts `range=custom&start=YYYY-MM-DD&end=YYYY-MM-DD`, both days inclusive.
- `start` and `end` are required, must be real calendar days (`2026-02-30` is not), `end ≥ start`, years 1970-9999 and at most 36,600 days. Anything else is 422.
- `start` / `end` sent with any other range are rejected (422) rather than ignored, so a stale link never shows the wrong span.
- Responses carry `range: "custom"`, `range_label` such as `Oct 1 – Oct 31, 2026` (`Dec 15, 2025 – Jan 14, 2026` across years), plus `range_start` / `range_end`.
- Totals, trend, ranking, hours, Personal Records and Plan Spent & ROI all use the window. Export files are named `lifelong-ai-tracker_custom_2026-10-01_2026-10-31[_tool][_model]_<today>.csv`; the SVG card is titled with the dates and its header says `Custom range · 31 days`.

**Period-over-period (`comparison` in `/api/overview`).** The same tool / model scope, over the period right before the range:
- `today` → yesterday · `7d` / `30d` / `90d` → the 7 / 30 / 90 days before · `ytd` → Jan 1 to the same month and day last year (Feb 29 → Feb 28) · `last_year` → the calendar year before it · `custom` of N days → the N days before · `lifelong` → `comparison: null`.
- Fields: `prev_label` (`yesterday`, `prev 30d`, `prior YTD`, `2024`, `prev 31d`), `prev_start`, `prev_end`, `prev_cost_usd`, `prev_total_tokens`, `cost_pct_change`, `tokens_pct_change`. The changes are percentages (`18.4` = +18.4%), `null` when the previous value is 0.
- The **Total Cost** and **Total Tokens** cards show a small badge such as `↑ 18.4% vs prev 30d` or `↓ 12.0% vs prev 7d` (`→ 0.0%` when flat). When the previous period is 0 and this one is not it reads `New vs prev 7d`. It is hidden for Lifelong and when both periods are 0. Hovering shows the previous period's dates and its exact amount or token count. The badge uses neutral colours: a rise is not marked good or bad.

**Scope filters.** All seven statistics endpoints above (overview to card) also accept `tool` and `model`.
- `tool`: omitted or `all` means every tool. `claude_code` is accepted for `claude-code`.
- `model`: an exact model id. Omitted or `all` means every model.
- The two combine with each other and with `range`. Export and card filenames include the scope.

**Trend chart.**
- An SVG line chart with a soft gradient area, point markers (up to 62 points), horizontal reference lines, and a hover guide.
- Controls: `Tokens | Cost`, `Auto | Daily | Weekly | Monthly`, and `Combined | By Agent`.
- *By Agent* draws one line per tool. Each tool keeps a fixed colour: Claude Code is orange, Antigravity is teal, OpenCode is pink, Codex is emerald.
- In *By Agent*, a legend shows each agent's period total, and the tooltip lists every agent plus the total.
- *By Agent* is disabled while a single tool is selected.
- `Last Year` (the whole previous calendar year) is a range next to `YTD` and `Lifelong`.
- The hash keeps the trend settings, e.g. `#range=lifelong&gran=month&view=agent`.

**Dashboard interactions.**
- The top bar has an `All Tools | Claude Code | Antigravity | …` switcher, built from `/api/tools` (only tools with data appear).
- Clicking a model row in the Breakdown table focuses every chart on that model. A `Model: … ×` chip clears it; clicking the row again also clears it. Clicking a tool row switches the tool.
- The state lives in the URL hash, e.g. `#range=30d&tool=antigravity&model=gemini-4-neon-low`.
- **Custom** (right of `Lifelong`, key `8`) shows compact `Start` / `End` date inputs and **Apply**. It starts with the last 30 days (or the custom window applied before) and loads at once; edit the dates and press **Apply** or Enter to reload every panel. An end before the start or an impossible date is refused in place. The hash keeps the window, e.g. `#range=custom&start=2026-10-01&end=2026-10-31`; a hash with a bad window falls back to the last 30 days.
- Unpriced models get a **Set Price** button, both in the warning banner and next to the `unpriced` badge.
- In the price dialog, *Apply to base model* (on by default for tiered names) saves one price for the base model (`gemini-4-neon`) instead of a single variant.
- **Pricing** (top bar, next to the exports) opens the *Model Pricing* table: `USD per 1M tokens`, the last-updated date, `All | Claude | Gemini | GPT` filters (with counts; `Other` appears when needed), a live search box, a **Used only** switch (on by default: only the models your ledger has used; turn it off to see every built-in price; a model you just added stays listed until the dialog closes) and **+ Add Model**. A model with dated prices shows `since Dec 1, 2026 · 2 price periods` under its name and the older prices on hover. Columns are `Model | Input | Output | Thinking | Cache Read | Cache Write | Action`, in tabular figures; the *All* view is grouped by provider.
- A row's **Edit** opens the same price dialog prefilled with that model's current price. **Save & Reprice** writes `pricing.json` atomically, reprices the ledger, then refreshes the table (the edited row is highlighted) and the dashboard. **+ Add Model** opens it with an editable model name.
- `Esc` or the backdrop closes the topmost dialog (the price dialog first, then the table).

**Savings, plans, records and shortcuts.**
- **Cache Saved** KPI: `sum(cache_read_tokens / 1M * max(0, input rate - cache-read rate))` over priced events in scope, using the current `pricing.json` (a Set Price edit shows immediately). Cache reads of unpriced models are counted separately.
- **Subscription ROI**: record plan periods in *Pricing → Subscriptions* (or copy `subscriptions.example.json` to `subscriptions.json`). Each tool has a list of non-overlapping periods `{start, end, monthly_usd}`; an empty / null `end` means the plan is still running (counted up to today), so cancellations, different start days and mid-way up- or downgrades are all just periods. For every paid period the overlap with the range (inclusive days, never past today) costs `overlap_days / 30.436875 × monthly_usd`, and only the API-equivalent value of those same days counts, so days after a plan ends add neither cost nor value. The **Plan Spent & ROI** card leads with what the plans actually cost in the range, in full (e.g. `$142.50 spent`, never cut off), then the multiple (`17.4× value`), then `$2,484.12 API value · Saved $2,341.62 · 30 days` (*Net loss* when the value is below the plan cost). It follows the range and tool filters. If paid plans exist but none overlaps the range it shows `–` and *No paid plan in this range*. *Lifelong* starts at the earliest of the paid tools' first active day and their earliest plan start. The tool filter applies; the model filter does not (a plan pays for the whole tool). Old files with a plain number per tool (`"claude-code": 20.0`) still load as an undated plan that counts every day.
- **API vs subscription billing**: mark a source in `sources.json` with `"billing": "api"` when it runs on a pay-as-you-go API key (the default `"subscription"` keeps the old behaviour). Events are attributed to a source by the log file they were read from (the source whose folder is the longest prefix of that file's path, so a nested source overrides its parent; files no source claims count as subscription usage). API-billed usage is real spend, so it is left out of the plan value and the ROI multiple and summed separately: `/api/overview` returns `billing` (`api_direct_spend_usd`, `api_events`, `api_unpriced_events`, `api_tokens`, `subscription_value_usd`, per-tool `tools`) and `subscription.api_direct_spend_usd` / per-tool `api_spend_usd`. When any API source exists the **Plan Spent & ROI** card adds `Subscription: $Y · Direct API: $X`, and the tooltip explains both and the total out of pocket (plans + API). With API sources but no plan the card shows the direct API spend and *Pay as you go*. Like the plans, it follows the range and tool filters, not the model filter. Total Cost and the other KPIs still include all usage.
- **Privacy**: subscription dates and fees appear only in the dashboard and `/api/subscriptions` / `/api/overview`. The SVG card and the CSV / JSON usage export never include them.
- **Personal Records** row: peak day (most tokens, with its cost; ties go to the earliest day), heaviest session (tool, day, tokens and cost only - never an id, path or project) and longest streak with its dates. All three follow the range, tool and model filters.
- **Group variants** switch on the Breakdown card (Model tab only): merges `-low / -high / dated` releases into one family row with a `×n` badge; family rows do not focus the dashboard. On by default; turning it off is kept in the hash as `group=0`.
- **Sync Now** (top bar) runs `POST /api/sync`, shows *Syncing…* while it runs, then a toast with new / updated counts per tool and reloads the dashboard.
- **Keys** (not while typing in a field or while a dialog is open): `1`-`7` pick Today → Lifelong, `8` picks Custom (and focuses Start), `P` opens Pricing, `S` syncs, `Esc` clears the model filter (with a dialog open, `Esc` closes the dialog instead).

## Phase 3: scheduled archiving, backups, report card

**`sync-all`** reads `sources.json` (copy `sources.example.json`). Each source has a `tool`, a `path`, an optional `name`, and `enabled`.
- `~` is expanded. Relative paths resolve against the config file's folder. Unknown keys are rejected, so a typo cannot silently change behaviour.
- A missing path, a placeholder collector, or a crash while parsing marks only that source as failed; the rest still sync.
- Exit code: 0 when every enabled source (and the backup, if requested) succeeded, 1 when anything failed, 2 for a config or pricing error.
- `--quiet` prints one timestamped summary line plus one line per failure, for the scheduler log.
- Optional `"billing": "subscription" | "api"` per source (see *API vs subscription billing* above).

**`backup`** uses `sqlite3.Connection.backup()` on a read-only connection, so it is safe while a sync is writing.
- The copy is verified with `PRAGMA integrity_check` and switched to a single-file journal, so no `-wal` / `-shm` files sit beside it. It is then renamed to `tracker-YYYYMMDD-HHMMSS.db`.
- `--keep N` deletes all but the newest N files that match that name. Other files in the folder are never touched.

**`schedule render`** writes the nightly job for this computer's scheduler (or `--platform macos|linux|windows`) and prints the commands for you to run yourself. It only writes inside the project (`deploy/` by default) and never runs `launchctl`, `systemctl`, `crontab` or PowerShell, nor writes to `~/Library`, `~/.config` or the Task Scheduler.
- **macos**: a LaunchAgent plist (`plistlib`) plus `cp` / `launchctl bootstrap` / `bootout` commands. If the project is under `~/Documents` (or Desktop / Downloads), macOS privacy protection may block the background job; the command prints how to grant access.
- **linux**: a `systemd --user` pair, `<label>.service` (`Type=oneshot`, low priority) and `<label>.timer` (`OnCalendar=*-*-* HH:MM:00`, `Persistent=true` to catch up after downtime), plus `<label>.crontab` with the equivalent cron line (tagged `# lifelong-tracker:<label>` so the printed uninstall command removes exactly that line). Use either one. Arguments are quoted for systemd (`%` → `%%`, `$` → `$$`) and for the shell (`%` → `\%` in cron).
- **windows**: `<label>.ps1` registers a per-user Task Scheduler task (daily trigger, start when available, no overlap) that runs `tracker.exe sync-all` through `cmd.exe` and appends to the log; `<label>.uninstall.ps1` removes it. Both are UTF-8 with BOM and CRLF so Windows PowerShell 5.1 reads non-ASCII paths correctly. The default executable is `.venv\Scripts\tracker.exe`. Paths containing `"` or `%` are refused (cmd.exe would expand them).
- Every path is absolute. stdout and stderr go to `logs/sync.log`. By default the job runs `sync-all --quiet --backup-dir backups --keep 14`.
- **Tests on any OS**: the JavaScriptCore browser tests run where a `jsc` shell exists (built into macOS) and are skipped elsewhere, and the timezone pin works without `time.tzset`, so the suite passes on Linux and Windows CI too.

**`card`** and **`GET /api/card`** produce a 1200×630 dark SVG card. It shows totals, the token mix, cache hit %, the top 3 models, the longest streak, and a fun equivalence (~10 tokens per line of code, ~120K tokens per novel).
- The card is built only from aggregates and model names. It never queries project names, IDs, or paths.
- Model names are cut to their last path segment, stripped of characters that are invalid in XML, truncated, and XML-escaped.
- It uses no script, no `<style>`, and no external fonts or links.

## Antigravity collector

`--tool antigravity` (or `"tool": "antigravity"` in `sources.json`) takes the `brain` folder as its source, for example `~/.gemini/antigravity/brain`.

**Which files are read.**
- Only `<conversation-id>/.system_generated/logs/transcript.jsonl` is read.
- `transcript_full.jsonl` is never opened, which prevents double counting.
- Files at any other level are ignored, and symlinks are never followed.

**Which lines count.**
- A line becomes an event only if it has `source == "MODEL"` and at least one numeric token field.
- All-zero usage is skipped, the same as for Claude Code.
- `content`, `thinking`, `tool_calls`, and `media` are never read into an event.

**Field mapping.**

| Event field | Value |
|---|---|
| `tool` | `antigravity` |
| `session_id` = `request_id` | the conversation folder name |
| `message_id` | `step_<step_index>` |
| `timestamp` | `created_at` |
| `model` | the raw `model` value |
| `project_name` | `antigravity` |
| tokens | `input_tokens`, `output_tokens`, `cache_read_tokens`, `thinking_tokens`, `cache_write_tokens`; a missing field counts as 0 |

A step that is written again merges into one row through the MAX-per-field UPSERT.

`pricing.json` covers `gemini-4-neon`, `-hydrogen`, `-argon` and `-carbon` (each entry also covers its `-low/-high/-xhigh` variants). Any other Gemini model appears as **Unpriced**. Click **Set Price** in the dashboard, or **Edit** / **+ Add Model** in the **Pricing** table (tick *Apply to base model* to cover every `-low/-high/-xhigh` variant), or add their prices by hand and run `tracker reprice`.

## OpenCode collector

`--tool opencode` takes the `opencode.db` file itself, or the folder that directly contains it (no recursive search, symlinks are skipped). In `sources.json`, `"path"` may also point at the file.

**Read-only by construction** (shared with the Codex collector, `collectors/sqlite_source.py`).
- The database is opened with a `file:…?mode=ro` URI (the path is percent-encoded, so spaces, `?` and `#` are safe), then `PRAGMA query_only = ON` and `PRAGMA trusted_schema = OFF`.
- The tracker never writes to it and never leaves a `-journal` file next to it. It reads committed rows that are still in the `-wal` file while OpenCode is running.

**Which fields are read.**
- Only table `session_message` (`id`, `session_id`, `time_created`, `time_updated`, `data`) is queried.
- From `data`, SQLite's `json_extract` selects only `$.tokens`, `$.model`, `$.modelID` and `$.time.created`. The JSON document itself, `content`, `parts`, `path` and every other field never reach Python.
- A row whose `data` is not valid JSON counts as malformed and is skipped. A row with no `tokens` object, or with all five counts at zero, is ignored.

**Field mapping.**

| Event field | Value |
|---|---|
| `tool` | `opencode` |
| `session_id` | `session_id` |
| `request_id` = `message_id` | `id` |
| `timestamp` | `time_created` (Unix ms → UTC `…Z`); `data.time.created` if the column is unusable |
| `model` | `data.model.id`, or `data.model` if it is a string, else `data.modelID`, else `unknown` |
| `project_name` | `opencode` |
| tokens | `input`, `output`, `reasoning` → thinking, `cache.read`, `cache.write`; missing or non-numeric counts are 0 |

**Incremental sync.**
- Before opening the database, the tracker compares the inode, size and mtime of `opencode.db` and its `-wal` file. If nothing changed, the sync is skipped.
- Otherwise, only rows with `time_updated` at or after the last high-water mark are re-read. A message updated while streaming merges into one row through the UPSERT.
- A replaced file (new inode), or an older copy restored in place, triggers a full rescan. The rescan is safe because the UPSERT is idempotent.

## Codex collector

`--tool codex` takes the `state_5.sqlite` file itself, or the folder that directly contains it (no recursive search, symlinks are skipped; other `state_*.sqlite` versions are not picked up). In `sources.json`, `"path"` may also point at the file. It is opened read-only exactly like the OpenCode database, including committed rows still in the `-wal` file.

**Which columns are read.** Only `id`, `created_at_ms`, `created_at`, `updated_at_ms`, `model`, `cwd` and `tokens_used` from table `threads`. `title`, `first_user_message`, `preview`, `rollout_path` and every other column are never selected. A test traces every SQL statement to prove it.

> **Codex `state_5.sqlite` stores only a running total per thread.** There is no per-request record and no split into input / output / reasoning / cache. The tracker therefore records one event per thread with the whole total in `input_tokens` and the other four categories at 0. Billing the whole total at the full input rate would badly overstate cost, because most of a coding agent's context is cache reads. So these rows (`tool == "codex"`, `message_id == "thread_total"`, all other categories 0) are priced at a **90/8/2 blended rate**: `0.90 × cache_read + 0.08 × input + 0.02 × output` per 1M tokens. This is an estimate. Sync, `tracker reprice` and the dashboard's *Save & Reprice* all apply it; the raw token columns are not changed, and the other tools are priced exactly as before. These rows have no cache breakdown, so they are left out of Cache hit % (see below). The event keeps the thread's creation time, so a long thread's usage is counted on the day it started.

**Field mapping.**

| Event field | Value |
|---|---|
| `tool` | `codex` |
| `session_id` = `request_id` | `id` |
| `message_id` | `thread_total` |
| `timestamp` | `created_at_ms` (Unix ms → UTC `…Z`); `created_at` (Unix seconds) if that is empty |
| `model` | `model`, or `unknown` if missing or blank |
| `project_name` | last directory name of `cwd` (POSIX or Windows), or `codex` if missing |
| tokens | `input_tokens = tokens_used`; rows with `tokens_used` NULL or `<= 0` are skipped |

**Incremental sync.** The same signature check and `ms:<n>` high-water mark as OpenCode, using `COALESCE(updated_at_ms, created_at_ms, created_at * 1000)`. When a thread grows, its row is re-read and the MAX-per-field UPSERT (`ON CONFLICT DO UPDATE`) replaces the stored total. A total never shrinks.

## Contributing

Collectors for more agents (Cursor, Cline, Gemini CLI, ...) and fixes are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers:

- the privacy rules;
- the base classes;
- a worked collector example;
- the fabricated-fixture and canary tests every collector needs.

## License

[MIT](LICENSE)
