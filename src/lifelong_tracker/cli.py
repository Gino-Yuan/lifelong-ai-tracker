"""Terminal entry point.

`tracker init | open | sync | sync-all | stats | reprice | serve | card | backup | schedule render |
collectors`
"""

from __future__ import annotations

import plistlib
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from lifelong_tracker import analytics
from lifelong_tracker.analytics import RANGES, Totals
from lifelong_tracker.collectors import COLLECTORS, get_collector
from lifelong_tracker.config import (
    DEFAULT_BACKUP_DIR,
    DEFAULT_DB_PATH,
    DEFAULT_LOG_PATH,
    DEFAULT_PLIST_PATH,
    DEFAULT_PRICING_PATH,
    DEFAULT_SOURCES_PATH,
    DEFAULT_SUBSCRIPTIONS_PATH,
    DEFAULT_TRACKER_BIN,
    IS_WINDOWS,
    PROJECT_ROOT,
    pricing_read_path,
    pricing_seed,
)
from lifelong_tracker.pricing import PricingTable
from lifelong_tracker.storage import connect, connect_readonly, pricing_drift, reprice
from lifelong_tracker.sync import sync_source

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Lifelong AI Tracker - local, offline token ledger for AI coding tools.",
)
schedule_app = typer.Typer(
    no_args_is_help=True,
    help="Generate the macOS launchd job for sync-all (writes files only; installs nothing).",
)
app.add_typer(schedule_app, name="schedule")
console = Console()

ConfigOpt = typer.Option(
    DEFAULT_SOURCES_PATH, "--config", "-c", help="Sources config (see sources.example.json).",
    dir_okay=False,
)
KeepOpt = typer.Option(14, "--keep", min=1, help="Number of newest backups to keep.")
CARD_RANGE_ALIASES = {"all": "lifelong", "year": "ytd"}

DbOpt = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite ledger path.", dir_okay=False)
PricingOpt = typer.Option(
    DEFAULT_PRICING_PATH, "--pricing", dir_okay=False,
    help="pricing.json path (default: the project's; the bundled table until one exists).",
)


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_compact(n: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.2f}{unit}"
    return f"{int(n)}"


def _fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _fmt_usd(x: float) -> str:
    return f"${x:,.2f}" if x >= 1 or x == 0 else f"${x:,.4f}"


def _load_pricing(path: Path) -> PricingTable:
    """Load --pricing; the default location falls back to the bundled table until it exists."""
    try:
        return PricingTable.load(pricing_read_path(path))
    except (OSError, ValueError) as exc:
        console.print(f"[red]Cannot load pricing file {path}: {exc}[/red]")
        raise typer.Exit(2) from exc


# --------------------------------------------------------------------------- sync

@app.command()
def sync(
    source: Path = typer.Option(
        ...,
        "--source",
        "-s",
        help="Directory containing the tool's log files, or for opencode / codex the database file "
        "(required; nothing is scanned by default).",
        exists=True,
        file_okay=True,
        dir_okay=True,
        resolve_path=True,
    ),
    tool: str = typer.Option("claude-code", "--tool", "-t", help="Collector to use."),
    db: Path = DbOpt,
    pricing_path: Path = PricingOpt,
) -> None:
    """Incrementally import usage metadata from SOURCE into the ledger."""
    try:
        collector = get_collector(tool)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    if not collector.implemented:
        try:
            collector.discover(source)
        except NotImplementedError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(3)

    if source.is_file() and not collector.accepts_file:
        console.print(f"[red]--source must be a directory for --tool {collector.tool}.[/red]")
        raise typer.Exit(2)

    pricing = _load_pricing(pricing_path)
    try:
        collector.discover(source)  # validate the source before the ledger is created
        with connect(db) as conn:
            report = sync_source(conn, collector, source, pricing)
    except (ValueError, sqlite3.DatabaseError) as exc:
        console.print(f"[red]Cannot read source: {exc}[/red]")
        raise typer.Exit(2) from exc

    t = Table(title=f"Sync - {collector.display_name}", show_header=False, box=None)
    t.add_row("Source", str(report.source))
    t.add_row("Database", str(Path(db).resolve()))
    t.add_row("Files found", _fmt_int(report.files_seen))
    t.add_row("  scanned / unchanged", f"{report.files_scanned} / {report.files_unchanged}")
    t.add_row("  reset (truncated/rewritten/replaced)", _fmt_int(report.files_reset))
    t.add_row("Lines read", _fmt_int(report.lines_read))
    t.add_row("Malformed lines skipped", _fmt_int(report.bad_lines))
    t.add_row("Usage records parsed", _fmt_int(report.events_parsed))
    t.add_row(
        "Events inserted / updated / unchanged",
        f"{report.upsert.inserted} / {report.upsert.updated} / {report.upsert.unchanged}",
    )
    console.print(t)
    for path, reason in report.resets[:10]:
        console.print(f"  [yellow]reset[/yellow] ({reason}): {path}")


# --------------------------------------------------------------------------- stats

def _totals_table(title: str, rows: list[tuple[str, Totals]], key_label: str) -> Table:
    show_thinking = any(tot.thinking_tokens for _, tot in rows)
    t = Table(
        title=title, title_justify="left", header_style="bold", box=box.SIMPLE_HEAD,
        padding=(0, 1), pad_edge=False, collapse_padding=True,
    )
    t.add_column(key_label, overflow="fold", min_width=12, ratio=1)
    cols = ["Events", "Input", "Output"] + (["Think"] if show_thinking else [])
    cols += ["CacheR", "CacheW", "Total", "Hit%", "Cost"]
    for col in cols:
        t.add_column(col, justify="right", no_wrap=True)
    for key, tot in rows:
        cells = [key, _fmt_int(tot.events), _fmt_compact(tot.input_tokens), _fmt_compact(tot.output_tokens)]
        if show_thinking:
            cells.append(_fmt_compact(tot.thinking_tokens))
        cells += [
            _fmt_compact(tot.cache_read_tokens),
            _fmt_compact(tot.cache_write_tokens),
            _fmt_compact(tot.total_tokens),
            _fmt_pct(tot.cache_hit_rate),
            _fmt_usd(tot.cost_usd) + ("*" if tot.unpriced_events else ""),
        ]
        t.add_row(*cells)
    if any(tot.unpriced_events for _, tot in rows):
        t.caption = "* includes events from unpriced models (counted as $0)"
        t.caption_justify = "left"
    return t


@app.command()
def stats(
    range_name: str = typer.Option(
        "all", "--range", "-r", help="today | 7d | 30d | 90d | ytd | lifelong (aliases: year, all)."
    ),
    top: int = typer.Option(10, "--top", help="Rows per breakdown table."),
    db: Path = DbOpt,
    pricing_path: Path = PricingOpt,
) -> None:
    """Show the terminal summary for a time range."""
    if range_name not in RANGES:
        console.print(f"[red]--range must be one of: {', '.join(RANGES)}[/red]")
        raise typer.Exit(2)
    if not Path(db).exists():
        console.print(f"[yellow]No ledger at {db}. Run `tracker sync --source <dir>` first.[/yellow]")
        raise typer.Exit(1)
    pricing = _load_pricing(pricing_path)
    today = date.today()

    with connect(db) as conn:
        tot = analytics.summarize(conn, range_name, today)
        by_model = analytics.breakdown(conn, "model", range_name, today, top)
        by_tool = analytics.breakdown(conn, "tool", range_name, today, top)
        by_project = analytics.breakdown(conn, "project", range_name, today, top)
        hours = analytics.hourly_distribution(conn, range_name, today)
        st = analytics.streak(conn, today)
        unpriced = analytics.unpriced_models(conn, pricing)
        drift = pricing_drift(conn, pricing)

    summary = Table.grid(padding=(0, 3))
    summary.add_column(style="dim")
    summary.add_column(justify="right", style="bold")
    summary.add_row("Events", _fmt_int(tot.events))
    summary.add_row("Total tokens", f"{_fmt_int(tot.total_tokens)}  ({_fmt_compact(tot.total_tokens)})")
    summary.add_row("Equivalent cost", _fmt_usd(tot.cost_usd))
    summary.add_row("Avg $ / M tokens", _fmt_usd(tot.cost_per_mtok))
    summary.add_row("Cache hit %", _fmt_pct(tot.cache_hit_rate))
    summary.add_row("Output share", _fmt_pct(tot.output_ratio))
    summary.add_row("Current streak", f"{st.current} day(s)")
    longest = f"{st.longest} day(s)"
    if st.longest_start:
        longest += f"  {st.longest_start} -> {st.longest_end}"
    summary.add_row("Longest streak", longest)
    summary.add_row("Active days (lifelong)", _fmt_int(st.active_days))
    console.print(Panel(summary, title=f"Lifelong AI Tracker - {RANGES[range_name]}", expand=False))

    cats = Table(title="Token categories", title_justify="left", header_style="bold")
    cats.add_column("Category")
    cats.add_column("Tokens", justify="right")
    cats.add_column("Share", justify="right")
    for label, value, share in tot.categories():
        cats.add_row(label, _fmt_int(value), _fmt_pct(share))
    console.print(cats)

    console.print(_totals_table("By model", by_model, "Model"))
    console.print(_totals_table("By tool", by_tool, "Tool"))
    console.print(_totals_table(f"By project (top {top})", by_project, "Project"))

    peak = max((h.tokens for h in hours), default=0)
    ht = Table(title="24h activity (local time)", title_justify="left", header_style="bold")
    ht.add_column("Hour", justify="right")
    ht.add_column("Tokens", justify="right")
    ht.add_column("Events", justify="right")
    ht.add_column("")
    for h in hours:
        bar = "█" * round(30 * h.tokens / peak) if peak else ""
        ht.add_row(f"{h.hour:02d}", _fmt_compact(h.tokens), _fmt_int(h.events), f"[cyan]{bar}[/cyan]")
    console.print(ht)

    if drift:
        console.print(
            f"[yellow]Stored costs differ from pricing.json for: {', '.join(drift)}. "
            f"Run `tracker reprice` to refresh.[/yellow]"
        )
    if unpriced:
        ut = Table(
            title="Unpriced models - add them to pricing.json, then run `tracker reprice`",
            title_justify="left",
            title_style="bold red",
            header_style="bold red",
            border_style="red",
        )
        ut.add_column("Model", style="bold yellow")
        ut.add_column("Events", justify="right")
        ut.add_column("Tokens (lifelong)", justify="right")
        for u in unpriced:
            ut.add_row(u.model, _fmt_int(u.events), _fmt_int(u.tokens))
        console.print(ut)
    else:
        console.print("[green]All models are priced.[/green]")


# --------------------------------------------------------------------------- reprice

@app.command("reprice")
def reprice_cmd(db: Path = DbOpt, pricing_path: Path = PricingOpt) -> None:
    """Recompute equivalent cost of every event from pricing.json (tokens untouched)."""
    pricing = _load_pricing(pricing_path)
    if not Path(db).exists():
        console.print(f"[yellow]No ledger at {db}.[/yellow]")
        raise typer.Exit(1)
    with connect(db) as conn:
        res = reprice(conn, pricing)
        unpriced = analytics.unpriced_models(conn, pricing)
    console.print(
        f"Repriced {res.events_priced:,} events across {res.models_priced} model(s); "
        f"{res.events_unpriced:,} events in {res.models_unpriced} unpriced model(s).\n"
        f"Total equivalent cost: {_fmt_usd(res.cost_before)} -> {_fmt_usd(res.cost_after)}"
    )
    for u in unpriced:
        console.print(f"  [bold yellow]unpriced:[/bold yellow] {u.model} ({u.events:,} events)")


# --------------------------------------------------------------------------- serve

@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (loopback only)."),
    port: int = typer.Option(8765, "--port", min=1, max=65535, help="TCP port."),
    db: Path = DbOpt,
    pricing_path: Path = PricingOpt,
    sources: Path = typer.Option(
        DEFAULT_SOURCES_PATH, "--sources", dir_okay=False,
        help="sources.json used by the dashboard's Sync Now button.",
    ),
    subscriptions: Path = typer.Option(
        DEFAULT_SUBSCRIPTIONS_PATH, "--subscriptions", dir_okay=False,
        help="subscriptions.json holding monthly plan fees (created on first save).",
    ),
) -> None:
    """Serve the dashboard on http://127.0.0.1:PORT (foreground; Ctrl+C to stop)."""
    _refuse_non_loopback(host)
    _load_pricing(pricing_path)  # fail fast on a missing or broken --pricing file
    if not Path(db).exists():
        console.print(f"[yellow]No ledger at {db} yet - the dashboard will be empty until you sync.[/yellow]")

    bind = _bind_address(host)
    console.print(f"Dashboard: [bold cyan]{_dashboard_url(bind, port)}[/bold cyan]  (Ctrl+C to stop)")
    _run_server(db, pricing_path, subscriptions, sources, bind, port)


def _refuse_non_loopback(host: str) -> None:
    from lifelong_tracker.web.security import is_loopback_host

    if not is_loopback_host(host):
        console.print(
            f"[red]Refusing to bind to {host!r}: the dashboard only listens on loopback "
            f"(127.0.0.1, ::1 or localhost).[/red]"
        )
        raise typer.Exit(2)


def _bind_address(host: str) -> str:
    return "127.0.0.1" if host.strip().lower() == "localhost" else host.strip().strip("[]")


def _dashboard_url(bind: str, port: int) -> str:
    from lifelong_tracker.launcher import dashboard_url

    return dashboard_url(bind, port)


def _run_server(db: Path, pricing_path: Path, subscriptions: Path, sources: Path, bind: str, port: int) -> None:
    import uvicorn

    from lifelong_tracker.web.app import create_app

    uvicorn.run(
        create_app(Path(db), Path(pricing_path), subscriptions_path=Path(subscriptions),
                   sources_path=Path(sources), pricing_seed=pricing_seed(Path(pricing_path))),
        host=bind,
        port=port,
        log_level="warning",
        access_log=False,
        server_header=False,
        proxy_headers=False,
    )


# --------------------------------------------------------------------------- init

@app.command("init")
def init_cmd(
    out: Path = typer.Option(DEFAULT_SOURCES_PATH, "--out", "-o", dir_okay=False,
                             help="Config file to create."),
    force: bool = typer.Option(False, "--force", help="Replace an existing config file."),
) -> None:
    """Create sources.json by answering a few questions (nothing is scanned or read)."""
    from lifelong_tracker import setup_wizard as wiz
    from lifelong_tracker.collectors import tool_label
    from lifelong_tracker.schedule import detect_platform
    from lifelong_tracker.sources import BILLING_MODES, BILLING_SUBSCRIPTION

    out = Path(out)
    if out.exists() and not force:
        console.print(f"[yellow]{out} already exists - nothing was changed. "
                      f"Use --force to replace it, or --out to write somewhere else.[/yellow]", soft_wrap=True)
        raise typer.Exit(1)

    platform = detect_platform()
    console.print(Panel.fit(
        "Answer a few questions to create [bold]sources.json[/bold].\n"
        "Press Enter to accept the value in \\[brackets].\n"
        "Paths are only written to the file, not opened or checked now;\n"
        "`tracker sync-all` reports any that are missing.",
        title="Lifelong AI Tracker setup", border_style="cyan",
    ))
    choices = []
    defaults = wiz.tool_defaults(platform)
    for i, d in enumerate(defaults, start=1):
        label = tool_label(d.tool)
        console.print(f"\n[bold][{i}/{len(defaults)}] {label}[/bold]  [dim]default location: {d.hint}[/dim]",
                      highlight=False)
        if not typer.confirm(f"  Do you use {label}?", default=False):
            choices.append(wiz.Choice(d.tool, False, d.path))
            continue
        while True:
            raw = typer.prompt("  Path (Enter = default)", default=d.path)
            try:
                path = wiz.normalize_path(raw)
                break
            except ValueError as exc:
                console.print(f"  [red]{exc}[/red]")
        while True:
            billing = typer.prompt("  Billing: subscription (flat plan) or api (pay-as-you-go key)",
                                   default=BILLING_SUBSCRIPTION).strip().lower()
            if billing in BILLING_MODES:
                break
            console.print(f"  [red]Type {' or '.join(BILLING_MODES)} (got {billing!r}).[/red]", highlight=False)
        choices.append(wiz.Choice(d.tool, True, path, billing))

    text = wiz.render_config(wiz.build_config(choices))
    try:
        wiz.write_config(out, text, force=force)
    except FileExistsError:
        console.print(f"[yellow]{out} was created meanwhile - nothing was changed.[/yellow]", soft_wrap=True)
        raise typer.Exit(1) from None
    except OSError as exc:
        console.print(f"[red]Cannot write {out}: {exc}[/red]", soft_wrap=True)
        raise typer.Exit(2) from exc

    console.print(f"\n[green]Wrote {out}[/green]", soft_wrap=True, highlight=False)
    t = Table(header_style="bold", box=box.SIMPLE_HEAD)
    t.add_column("Tool")
    t.add_column("Status")
    t.add_column("Path")
    t.add_column("Billing")
    for c in choices:
        t.add_row(tool_label(c.tool), "[green]enabled[/green]" if c.enabled else "[dim]skipped[/dim]",
                  c.path, c.billing if c.enabled else "-")
    console.print(t)
    if any(c.enabled for c in choices):
        console.print("Next: [bold]tracker open[/bold] syncs these sources and opens the dashboard.")
    else:
        console.print("[yellow]No tool enabled - edit the file or run `tracker init --force` later.[/yellow]")


# --------------------------------------------------------------------------- open

def _quiet_sync(sources: Path, db: Path, pricing: PricingTable) -> str:
    """Sync every enabled source; return a one-line summary (never raises for source errors)."""
    from lifelong_tracker.sources import SourcesConfigError, load_sources, sync_all

    try:
        specs = load_sources(sources)
    except SourcesConfigError as exc:
        return f"[yellow]Sync skipped - sources config error: {exc}[/yellow]"
    try:
        with connect(db) as conn:
            results = sync_all(conn, specs, pricing)
    except sqlite3.Error as exc:
        return f"[red]Sync skipped - cannot open ledger {db}: {exc}[/red]"
    ok = [r for r in results if r.status == "ok"]
    failed = [r for r in results if r.status == "failed"]
    if not ok and not failed:
        return "[yellow]Sync: no enabled sources in the config.[/yellow]"
    new = sum(r.report.upsert.inserted for r in ok if r.report)
    upd = sum(r.report.upsert.updated for r in ok if r.report)
    line = (f"Synced {len(ok)} source{'s' if len(ok) != 1 else ''}: "
            f"{new:,} new / {upd:,} updated events")
    if failed:
        names = ", ".join(r.spec.name for r in failed)
        return f"[yellow]{line}; {len(failed)} failed ({names}) - run `tracker sync-all` for details[/yellow]"
    return f"[green]{line}[/green]"


def _event_count(db: Path) -> int:
    try:
        with connect_readonly(db) as conn:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    except sqlite3.Error:
        return 0


@app.command("open")
def open_cmd(
    sources: Path = typer.Option(DEFAULT_SOURCES_PATH, "--sources", dir_okay=False,
                                 help="sources.json to sync first (skipped if it does not exist)."),
    db: Path = DbOpt,
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (loopback only)."),
    port: int = typer.Option(8765, "--port", min=1, max=65535, help="TCP port."),
    app_window: bool = typer.Option(False, "--app",
                                    help="Open a standalone app window (Chrome / Edge / Brave / Arc)."),
    no_sync: bool = typer.Option(False, "--no-sync", help="Do not sync before starting."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser window."),
    pricing_path: Path = PricingOpt,
    subscriptions: Path = typer.Option(
        DEFAULT_SUBSCRIPTIONS_PATH, "--subscriptions", dir_okay=False,
        help="subscriptions.json holding monthly plan fees (created on first save).",
    ),
) -> None:
    """Sync, start the dashboard and open it in a browser (Ctrl+C to stop)."""
    import threading

    from lifelong_tracker import launcher
    from lifelong_tracker.schedule import detect_platform

    _refuse_non_loopback(host)
    bind = _bind_address(host)
    url = launcher.dashboard_url(bind, port)
    pricing = _load_pricing(pricing_path)
    if launcher.port_accepts(bind, port):
        console.print(f"[red]Port {port} on {bind} is already in use (is the dashboard already open?). "
                      f"Visit {url} or choose another --port.[/red]", soft_wrap=True)
        raise typer.Exit(2)

    if not no_sync:
        if Path(sources).is_file():
            console.print(_quiet_sync(Path(sources), Path(db), pricing), soft_wrap=True, highlight=False)
        else:
            console.print(f"[dim]No {Path(sources).name} - run `tracker init` to choose what to track.[/dim]",
                          highlight=False)

    browser = launcher.find_chromium(detect_platform()) if (app_window and not no_browser) else None
    if no_browser:
        opens = "not opened (--no-browser)"
    elif browser is not None:
        opens = f"{browser.name} app window"
    elif app_window:
        opens = "default browser (no Chrome / Edge / Brave / Arc found)"
    else:
        opens = "default browser"
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold")
    body.add_column()
    body.add_row("Dashboard", f"[bold cyan]{url}[/bold cyan]")
    body.add_row("Events", f"{_event_count(Path(db)):,}")
    body.add_row("Opens in", opens)
    body.add_row("Stop", "Press Ctrl+C")
    console.print(Panel(body, title="Lifelong AI Tracker", border_style="cyan", expand=False))

    stop = threading.Event()
    opener = None
    if not no_browser:
        def report(how: str) -> None:
            if how == "failed":
                console.print(f"[yellow]Could not open a browser - visit {url}[/yellow]", highlight=False)
            elif how == "not-ready":
                console.print(f"[yellow]The server did not come up; no browser was opened.[/yellow]")

        opener = launcher.open_when_ready(url, bind, port, browser, stop=stop, on_done=report)
    try:
        _run_server(db, pricing_path, subscriptions, sources, bind, port)
    finally:
        stop.set()
        if opener is not None:
            opener.join(timeout=2)


# --------------------------------------------------------------------------- sync-all

def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _run_backup(db: Path, out_dir: Path, keep: int, quiet: bool) -> bool:
    from lifelong_tracker.backup import backup_database

    try:
        res = backup_database(db, out_dir, keep)
    except (OSError, ValueError, sqlite3.Error) as exc:
        console.print(f"[red]{_stamp()} backup FAILED: {exc}[/red]", soft_wrap=True)
        return False
    msg = (
        f"backup {res.path.name} ({res.size_bytes / 1_048_576:.2f} MiB, {res.events:,} events, "
        f"integrity {res.integrity}); kept {len(res.kept)}, removed {len(res.removed)} old"
    )
    console.print(f"{_stamp()} {msg}" if quiet else f"[green]{msg}[/green]", soft_wrap=True)
    return True


@app.command("sync-all")
def sync_all_cmd(
    config: Path = ConfigOpt,
    db: Path = DbOpt,
    pricing_path: Path = PricingOpt,
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Timestamped one-line summary plus errors (for logs)."),
    backup_dir: Optional[Path] = typer.Option(
        None, "--backup-dir", file_okay=False, help="After syncing, write an online backup here."
    ),
    keep: int = KeepOpt,
) -> None:
    """Sync every enabled source in the config; a failing source never stops the others.

    Exit code: 0 = all enabled sources (and the backup, if requested) succeeded,
    1 = at least one source or the backup failed, 2 = config / pricing error.
    """
    from lifelong_tracker.sources import SourcesConfigError, load_sources, sync_all

    try:
        specs = load_sources(config)
    except SourcesConfigError as exc:
        console.print(f"[red]{_stamp() + ' ' if quiet else ''}sources config error: {exc}[/red]", soft_wrap=True)
        raise typer.Exit(2) from exc
    pricing = _load_pricing(pricing_path)

    try:
        with connect(db) as conn:
            results = sync_all(conn, specs, pricing)
    except sqlite3.Error as exc:
        console.print(f"[red]{_stamp()} cannot open ledger {db}: {exc}[/red]", soft_wrap=True)
        raise typer.Exit(1) from exc

    ok = [r for r in results if r.status == "ok"]
    failed = [r for r in results if r.status == "failed"]
    disabled = [r for r in results if r.status == "disabled"]
    inserted = sum(r.report.upsert.inserted for r in ok if r.report)
    updated = sum(r.report.upsert.updated for r in ok if r.report)

    if quiet:
        console.print(
            f"{_stamp()} sync-all: {len(ok)} ok, {len(failed)} failed, {len(disabled)} disabled; "
            f"{inserted:,} new / {updated:,} updated events",
            soft_wrap=True, highlight=False,
        )
    else:
        t = Table(title="Sync all sources", title_justify="left", header_style="bold", box=box.SIMPLE_HEAD)
        t.add_column("Source")
        t.add_column("Tool")
        t.add_column("Status")
        t.add_column("Files scanned / unchanged", justify="right")
        t.add_column("New / updated", justify="right")
        style = {"ok": "[green]ok[/green]", "failed": "[red]failed[/red]", "disabled": "[dim]disabled[/dim]"}
        for r in results:
            rep = r.report
            t.add_row(
                r.spec.name, r.spec.tool, style[r.status],
                f"{rep.files_scanned} / {rep.files_unchanged}" if rep else "-",
                f"{rep.upsert.inserted:,} / {rep.upsert.updated:,}" if rep else "-",
            )
        console.print(t)
    for r in failed:
        prefix = f"{_stamp()} " if quiet else ""
        console.print(f"[red]{prefix}FAILED {r.spec.name} ({r.spec.tool}): {r.error}[/red]", soft_wrap=True)
    if not ok and not failed:
        console.print("[yellow]No enabled sources in the config - nothing was synced.[/yellow]")

    backup_ok = True
    if backup_dir is not None:
        backup_ok = _run_backup(db, backup_dir, keep, quiet)
    if failed or not backup_ok:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- backup

@app.command()
def backup(
    db: Path = DbOpt,
    out_dir: Path = typer.Option(DEFAULT_BACKUP_DIR, "--out-dir", "-o", file_okay=False, help="Backup folder."),
    keep: int = KeepOpt,
) -> None:
    """Online (WAL-safe) backup to OUT_DIR/tracker-YYYYMMDD-HHMMSS.db, keeping the newest N."""
    if not Path(db).is_file():
        console.print(f"[yellow]No ledger at {db}.[/yellow]")
        raise typer.Exit(1)
    if not _run_backup(Path(db), Path(out_dir), keep, quiet=False):
        raise typer.Exit(1)


# --------------------------------------------------------------------------- card

@app.command()
def card(
    range_name: str = typer.Option("lifelong", "--range", "-r", help="lifelong | last_year | ytd | 90d | 30d | 7d | today."),
    out: Path = typer.Option(Path("report.svg"), "--out", "-o", dir_okay=False, help="SVG file to write."),
    db: Path = DbOpt,
) -> None:
    """Write a shareable SVG report card (no paths or project names on it)."""
    from lifelong_tracker.card import CARD_RANGES, render_card

    range_name = CARD_RANGE_ALIASES.get(range_name, range_name)
    if range_name not in CARD_RANGES:
        console.print(f"[red]--range must be one of: {', '.join(CARD_RANGES)}[/red]")
        raise typer.Exit(2)
    if not Path(db).is_file():
        console.print(f"[yellow]No ledger at {db}. Run `tracker sync-all` first.[/yellow]")
        raise typer.Exit(1)
    with connect_readonly(db) as conn:
        svg = render_card(conn, range_name)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg, encoding="utf-8", newline="\n")  # LF on every OS
    console.print(f"Report card ({RANGES[range_name]}) written to [bold]{out.resolve()}[/bold]", soft_wrap=True)


# --------------------------------------------------------------------------- schedule

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@schedule_app.command("render")
def schedule_render(
    config: Path = ConfigOpt,
    platform: Optional[str] = typer.Option(
        None, "--platform", case_sensitive=False,
        help="macos (launchd plist), linux (systemd --user timer + crontab line) or windows "
             "(PowerShell Task Scheduler scripts). Default: this computer's platform.",
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", dir_okay=False,
        help="Main file to write (default: deploy/<label>.plist | .service | .ps1).",
    ),
    hour: int = typer.Option(23, "--hour", min=0, max=23, help="Local hour to run (0-23)."),
    minute: int = typer.Option(30, "--minute", min=0, max=59, help="Minute (0-59)."),
    db: Path = DbOpt,
    pricing_path: Path = PricingOpt,
    backup_dir: Path = typer.Option(DEFAULT_BACKUP_DIR, "--backup-dir", file_okay=False, help="Nightly backup folder."),
    keep: int = KeepOpt,
    no_backup: bool = typer.Option(False, "--no-backup", help="Do not back up after each sync."),
    log_file: Path = typer.Option(DEFAULT_LOG_PATH, "--log-file", dir_okay=False, help="stdout/stderr log."),
    label: str = typer.Option("com.lifelong-tracker.sync", "--label",
                              help="Job label (launchd label / systemd unit / Windows task name)."),
    tracker_bin: Optional[Path] = typer.Option(
        None, "--tracker-bin", dir_okay=False,
        help="tracker executable (default: the project's .venv entry point for the platform).",
    ),
) -> None:
    """Write the nightly sync-all job for launchd, systemd/cron or Task Scheduler, and print
    the commands to install it yourself (nothing is installed)."""
    from lifelong_tracker import schedule as sched
    from lifelong_tracker.sources import SourcesConfigError, load_sources

    target = (platform or sched.detect_platform()).lower()
    if target not in sched.PLATFORMS:
        console.print(f"[red]--platform must be one of: {', '.join(sched.PLATFORMS)}.[/red]")
        raise typer.Exit(2)
    if not _LABEL_RE.match(label):
        console.print("[red]--label may contain only letters, digits, '.', '_' and '-'.[/red]")
        raise typer.Exit(2)
    try:
        specs = load_sources(config)
    except SourcesConfigError as exc:
        console.print(f"[red]sources config error: {exc}[/red]", soft_wrap=True)
        raise typer.Exit(2) from exc

    project_root = PROJECT_ROOT
    _load_pricing(pricing_path)  # the job must not point at a missing or broken price table
    if tracker_bin is None:  # same OS family as here: the default; else that OS's .venv layout
        tracker_bin = (DEFAULT_TRACKER_BIN if (target == "windows") == IS_WINDOWS
                       else sched.default_tracker_bin(project_root, target))
    if out is None:
        out = sched.default_out(DEFAULT_PLIST_PATH.parent, label, target)
    spec = sched.ScheduleSpec(
        label=label,
        tracker_bin=Path(tracker_bin).absolute(),
        project_root=project_root,
        config=Path(config).resolve(),
        db=Path(db).resolve(),
        pricing=Path(pricing_path).resolve(),
        log_file=Path(log_file).resolve(),
        hour=hour,
        minute=minute,
        backup_dir=None if no_backup else Path(backup_dir).resolve(),
        keep=keep,
    )
    out = Path(out).resolve()
    try:
        files = sched.render(spec, target, out)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True, highlight=False)
        raise typer.Exit(2) from exc
    if target == "macos":
        plistlib.loads(files[0][1])  # round-trip sanity check
    for path, data in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    spec.log_file.parent.mkdir(parents=True, exist_ok=True)  # schedulers do not create it

    enabled = sum(1 for s in specs if s.enabled)
    scheduler = {"macos": "launchd", "linux": "systemd --user / cron",
                 "windows": "Windows Task Scheduler"}[target]
    console.print(f"Platform: [bold]{target}[/bold] ({scheduler})", highlight=False)
    for path, _ in files:
        console.print(f"[green]Wrote[/green] {path}", soft_wrap=True, highlight=False)
    console.print(
        f"Job [bold]{label}[/bold] runs `sync-all` daily at {hour:02d}:{minute:02d} (local time) "
        f"for {enabled} enabled source(s)"
        + ("" if no_backup else f", then backs up to {spec.backup_dir} (keep {keep})")
        + f".\nLog: {spec.log_file}",
        soft_wrap=True, highlight=False,
    )
    if not spec.tracker_bin.exists():
        console.print(f"[yellow]Warning: {spec.tracker_bin} does not exist yet.[/yellow]", soft_wrap=True)
    if enabled == 0:
        console.print("[yellow]Warning: the config has no enabled sources.[/yellow]")
    if target == "macos" and out.name != f"{label}.plist":
        console.print(f"[yellow]Note: launchd convention is to name the file {label}.plist.[/yellow]")

    shell = "PowerShell" if target == "windows" else "Terminal"
    console.print(f"\n[bold]Run these yourself in {shell} (nothing has been installed):[/bold]")
    for section, cmds in sched.commands_for(target, [p for p, _ in files], spec).items():
        console.print(f"\n[bold cyan]# {section}[/bold cyan]", highlight=False)
        for cmd in cmds:
            console.print(cmd, markup=False, highlight=False, soft_wrap=True)
    if target == "linux":
        console.print("\nTo run while you are logged out: loginctl enable-linger $USER",
                      markup=False, highlight=False)

    if target == "macos" and sched.is_privacy_protected(project_root, Path.home()):
        console.print(
            "\n[yellow]macOS privacy note: this project is inside a protected folder "
            "(Documents/Desktop/Downloads). Background jobs may be denied access there. If the "
            "log shows 'Operation not permitted' (or stays empty), grant Full Disk Access to "
            f"the interpreter {Path(spec.tracker_bin).parent.joinpath('python').resolve()} in "
            "System Settings > Privacy & Security, or move the project elsewhere.[/yellow]",
            soft_wrap=True,
        )


# --------------------------------------------------------------------------- collectors

@app.command("collectors")
def list_collectors() -> None:
    """List available collector plug-ins."""
    t = Table(header_style="bold")
    t.add_column("--tool")
    t.add_column("Name")
    t.add_column("Status")
    for name, cls in sorted(COLLECTORS.items()):
        status = "[green]ready[/green]" if cls.implemented else "[yellow]placeholder[/yellow]"
        t.add_row(name, cls.display_name, status)
    console.print(t)


if __name__ == "__main__":
    app()
