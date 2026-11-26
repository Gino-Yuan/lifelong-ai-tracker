"""Read-only analytics over the ledger.

Day / hour buckets use SQLite's 'localtime' modifier, i.e. the process local
timezone, consistently with the `daily_stats` view.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

from lifelong_tracker.pricing import PricingTable

RANGES: dict[str, str] = {
    "today": "Today",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "ytd": "Year to date",
    "last_year": "Last year",
    "lifelong": "Lifelong",
    # Phase 1 CLI spellings, kept as aliases
    "year": "Year to date",
    "all": "Lifelong",
}
WEB_RANGES: tuple[str, ...] = ("today", "7d", "30d", "90d", "ytd", "last_year", "lifelong")
CUSTOM = "custom"
MIN_CUSTOM_YEAR = 1970
MAX_CUSTOM_YEAR = 9999
MAX_CUSTOM_DAYS = 36_600  # ~100 years: keeps zero-filled daily series bounded
GRANULARITIES: tuple[str, ...] = ("day", "week", "month")
AUTO_DAY_MAX_DAYS = 120   # auto granularity: <= 120 days -> daily
AUTO_WEEK_MAX_DAYS = 730  # <= 2 years -> weekly, longer -> monthly
DIMENSIONS: dict[str, str] = {"model": "model", "tool": "tool", "project": "project_name"}

_LOCAL_DAY = "date(timestamp, 'localtime')"
_LOCAL_HOUR = "CAST(strftime('%H', timestamp, 'localtime') AS INTEGER)"
_TOTAL = (
    "(input_tokens + output_tokens + thinking_tokens + cache_read_tokens + cache_write_tokens)"
)
# Rows that carry only a grand total and no cache breakdown: Codex logs one
# cumulative `tokens_used` per thread (stored as input_tokens). Counting them in
# the cache hit rate would read as a 0% hit and drag every blend down, so the
# hit rate leaves them out. Every other metric still includes them.
_NO_CACHE_BREAKDOWN = (
    "(tool = 'codex' AND message_id = 'thread_total' AND cache_read_tokens = 0)"
)
_AGG = f"""
    COUNT(*)                                              AS events,
    COALESCE(SUM(input_tokens), 0)                        AS input_tokens,
    COALESCE(SUM(output_tokens), 0)                       AS output_tokens,
    COALESCE(SUM(thinking_tokens), 0)                     AS thinking_tokens,
    COALESCE(SUM(cache_read_tokens), 0)                   AS cache_read_tokens,
    COALESCE(SUM(cache_write_tokens), 0)                  AS cache_write_tokens,
    COALESCE(SUM(cost_usd), 0.0)                          AS cost_usd,
    COALESCE(SUM(CASE WHEN cost_usd IS NOT NULL THEN {_TOTAL} END), 0) AS priced_tokens,
    COALESCE(SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END), 0)     AS unpriced_events,
    COALESCE(SUM(CASE WHEN {_NO_CACHE_BREAKDOWN} THEN 1 ELSE 0 END), 0) AS no_breakdown_events,
    COALESCE(SUM(CASE WHEN {_NO_CACHE_BREAKDOWN} THEN input_tokens ELSE 0 END), 0)
                                                          AS no_breakdown_input_tokens,
    COALESCE(SUM(CASE WHEN {_NO_CACHE_BREAKDOWN} THEN cache_write_tokens ELSE 0 END), 0)
                                                          AS no_breakdown_cache_write_tokens
"""


# --------------------------------------------------------------------------- metrics

def safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def cache_hit_rate(input_tokens: int, cache_read_tokens: int, cache_write_tokens: int) -> float:
    """Cache Read / (Input + Cache Read + Cache Write); 0.0 when the denominator is 0."""
    return safe_ratio(cache_read_tokens, input_tokens + cache_read_tokens + cache_write_tokens)


@dataclass(frozen=True)
class Totals:
    events: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    priced_tokens: int = 0
    unpriced_events: int = 0
    # Part of the above that has no cache breakdown (see _NO_CACHE_BREAKDOWN).
    no_breakdown_events: int = 0
    no_breakdown_input_tokens: int = 0
    no_breakdown_cache_write_tokens: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Totals":
        keys = set(row.keys())
        return cls(**{k: row[k] for k in cls.__dataclass_fields__ if k in keys})

    def __add__(self, other: "Totals") -> "Totals":
        return Totals(**{k: getattr(self, k) + getattr(other, k) for k in self.__dataclass_fields__})

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens + self.output_tokens + self.thinking_tokens
            + self.cache_read_tokens + self.cache_write_tokens
        )

    @property
    def cache_hit_rate(self) -> float | None:
        """Hit rate over rows that have a cache breakdown.

        None when every row in scope lacks one (nothing to measure); 0.0 when
        there are no rows at all, as before.
        """
        if self.events and self.no_breakdown_events >= self.events:
            return None
        return cache_hit_rate(
            self.input_tokens - self.no_breakdown_input_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens - self.no_breakdown_cache_write_tokens,
        )

    @property
    def output_ratio(self) -> float:
        """Output share of all tokens: Output / Total; 0.0 when Total is 0."""
        return safe_ratio(self.output_tokens, self.total_tokens)

    @property
    def cost_per_mtok(self) -> float:
        """Blended equivalent USD per million tokens, over priced tokens only."""
        return safe_ratio(self.cost_usd * 1_000_000, self.priced_tokens)

    def as_dict(self) -> dict[str, float | int]:
        """JSON-ready view: raw sums plus every derived metric."""
        return {
            "events": self.events,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "priced_tokens": self.priced_tokens,
            "unpriced_events": self.unpriced_events,
            "cache_hit_rate": self.cache_hit_rate,
            "output_ratio": self.output_ratio,
            "cost_per_mtok": self.cost_per_mtok,
        }

    def categories(self) -> list[tuple[str, int, float]]:
        """The five token categories as (label, tokens, share of total)."""
        total = self.total_tokens
        return [
            (label, value, safe_ratio(value, total))
            for label, value in (
                ("Input", self.input_tokens),
                ("Output", self.output_tokens),
                ("Thinking", self.thinking_tokens),
                ("Cache Read", self.cache_read_tokens),
                ("Cache Write", self.cache_write_tokens),
            )
        ]


# --------------------------------------------------------------------------- ranges

@dataclass(frozen=True)
class CustomRange:
    """An explicit inclusive local-day window, `range=custom&start=...&end=...`.

    Accepted anywhere a range name is: every query resolves it through
    `range_bounds`, so totals, series, records, ROI, export and the card all follow it.
    """

    start: date
    end: date

    name = CUSTOM

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("end must be on or after start")
        for d in (self.start, self.end):
            if not MIN_CUSTOM_YEAR <= d.year <= MAX_CUSTOM_YEAR:
                raise ValueError(f"dates must fall in {MIN_CUSTOM_YEAR}-{MAX_CUSTOM_YEAR}")
        if self.days > MAX_CUSTOM_DAYS:
            raise ValueError(f"a custom range may span at most {MAX_CUSTOM_DAYS:,} days")

    @classmethod
    def parse(cls, start: str | None, end: str | None) -> "CustomRange":
        """From two YYYY-MM-DD strings; ValueError for missing, malformed or impossible dates."""
        if not start or not end:
            raise ValueError("range=custom needs both start and end (YYYY-MM-DD)")
        out = []
        for label, raw in (("start", start), ("end", end)):
            if len(raw) != 10 or raw[4] != "-" or raw[7] != "-" or not raw.replace("-", "").isdigit():
                raise ValueError(f"{label} must be a date in YYYY-MM-DD form")
            try:
                out.append(date.fromisoformat(raw))
            except ValueError:
                raise ValueError(f"{label} is not a real calendar date: {raw}") from None
        return cls(out[0], out[1])

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def range_name_of(rng: str | CustomRange) -> str:
    return rng.name if isinstance(rng, CustomRange) else rng


def _fmt_day(d: date, with_year: bool = True) -> str:
    s = f"{d:%b} {d.day}"
    return f"{s}, {d.year}" if with_year else s


def fmt_span(start: date, end: date) -> str:
    """'Oct 1 – Oct 31, 2026', 'Dec 15, 2025 – Jan 14, 2026' or 'Oct 1, 2026'."""
    if start == end:
        return _fmt_day(end)
    if start.year == end.year:
        return f"{_fmt_day(start, False)} – {_fmt_day(end)}"
    return f"{_fmt_day(start)} – {_fmt_day(end)}"


def range_label(rng: str | CustomRange) -> str:
    if isinstance(rng, CustomRange):
        return fmt_span(rng.start, rng.end)
    return RANGES[rng]


def range_slug(rng: str | CustomRange) -> str:
    """Filename fragment: the range name, or custom_<start>_<end>."""
    if isinstance(rng, CustomRange):
        return f"{CUSTOM}_{rng.start.isoformat()}_{rng.end.isoformat()}"
    return rng


def range_start(range_name: str | CustomRange, today: date) -> date | None:
    if isinstance(range_name, CustomRange):
        return range_name.start
    if range_name not in RANGES:
        raise ValueError(f"unknown range {range_name!r}; choose from {', '.join(RANGES)}")
    return {
        "today": today,
        "7d": today - timedelta(days=6),
        "30d": today - timedelta(days=29),
        "90d": today - timedelta(days=89),
        "ytd": date(today.year, 1, 1),
        "year": date(today.year, 1, 1),
        "last_year": date(today.year - 1, 1, 1),
        "lifelong": None,
        "all": None,
    }[range_name]


def range_bounds(range_name: str | CustomRange, today: date) -> tuple[date | None, date]:
    """(first day or None for unbounded, last day) of a range, both inclusive.

    Every range ends today except `last_year`, the whole previous calendar year,
    and a custom range, which ends on its own `end`.
    """
    if isinstance(range_name, CustomRange):
        return range_name.start, range_name.end
    start = range_start(range_name, today)
    if range_name == "last_year":
        return start, date(today.year - 1, 12, 31)
    return start, today


def _same_day_last_year(d: date) -> date:
    """d one year earlier; Feb 29 maps to Feb 28."""
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


@dataclass(frozen=True)
class PreviousPeriod:
    start: date
    end: date
    label: str  # e.g. "prev 30d", "yesterday", "prior YTD", "2024"


def previous_period(range_name: str | CustomRange, today: date) -> PreviousPeriod | None:
    """The window a range is compared against, or None (lifelong has no predecessor).

    today -> yesterday; 7d / 30d / 90d / custom (N days) -> the N days right before;
    ytd -> Jan 1 .. the same month/day last year (Feb 29 -> Feb 28);
    last_year -> the calendar year before it.
    """
    if isinstance(range_name, CustomRange):
        n = range_name.days
        end = range_name.start - timedelta(days=1)
        return PreviousPeriod(end - timedelta(days=n - 1), end, "prev day" if n == 1 else f"prev {n}d")
    name = {"year": "ytd", "all": "lifelong"}.get(range_name, range_name)
    if name not in WEB_RANGES:
        raise ValueError(f"unknown range {range_name!r}; choose from {', '.join(RANGES)}")
    if name == "lifelong":
        return None
    if name == "today":
        y = today - timedelta(days=1)
        return PreviousPeriod(y, y, "yesterday")
    if name in ("7d", "30d", "90d"):
        n = int(name[:-1])
        end = today - timedelta(days=n)
        return PreviousPeriod(end - timedelta(days=n - 1), end, f"prev {name}")
    if name == "ytd":
        return PreviousPeriod(date(today.year - 1, 1, 1), _same_day_last_year(today), "prior YTD")
    y = today.year - 2  # last_year
    return PreviousPeriod(date(y, 1, 1), date(y, 12, 31), str(y))


def pct_change(current: float, previous: float) -> float | None:
    """Percent change from previous to current; None when previous is 0 (no base)."""
    if not previous:
        return None
    return (current - previous) / previous * 100.0


def _conditions(
    start: date | None,
    end: date,
    day_expr: str,
    tool: str | None,
    model: str | None,
) -> tuple[str, dict[str, str]]:
    """WHERE clause for an optional day window plus optional exact tool / model scope.

    `day_expr` is one of two fixed SQL expressions chosen by the caller (never user
    input); tool / model values are always bound parameters.
    """
    conds: list[str] = []
    params: dict[str, str] = {}
    if start is not None:
        conds.append(f"{day_expr} BETWEEN :start AND :end")
        params.update(start=start.isoformat(), end=end.isoformat())
    if tool:
        conds.append("tool = :f_tool")
        params["f_tool"] = tool
    if model:
        conds.append("model = :f_model")
        params["f_model"] = model
    return ("WHERE " + " AND ".join(conds)) if conds else "", params


def _where(
    range_name: str, today: date, tool: str | None = None, model: str | None = None
) -> tuple[str, dict[str, str]]:
    """Filter over the `events` table (local-day window)."""
    return _conditions(*range_bounds(range_name, today), _LOCAL_DAY, tool, model)


def _where_daily(
    range_name: str, today: date, tool: str | None = None, model: str | None = None
) -> tuple[str, dict[str, str]]:
    """Filter over the `daily_stats` view (its `day` column is already local)."""
    return _conditions(*range_bounds(range_name, today), "day", tool, model)


# --------------------------------------------------------------------------- queries
#
# Every query accepts an optional exact `tool` / `model` scope (None = all). The
# scope composes with the time range: e.g. "Antigravity, last 30 days, one model".

def summarize(
    conn: sqlite3.Connection,
    range_name: str = "all",
    today: date | None = None,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> Totals:
    today = today or date.today()
    where, params = _where(range_name, today, tool, model)
    row = conn.execute(f"SELECT {_AGG} FROM events {where}", params).fetchone()
    return Totals.from_row(row)


def summarize_span(
    conn: sqlite3.Connection,
    first: date,
    last: date,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> Totals:
    """Totals for an explicit inclusive local-day window [first, last]."""
    where, params = _conditions(first, last, _LOCAL_DAY, tool, model)
    row = conn.execute(f"SELECT {_AGG} FROM events {where}", params).fetchone()
    return Totals.from_row(row)


def breakdown(
    conn: sqlite3.Connection,
    dimension: str,
    range_name: str = "all",
    today: date | None = None,
    limit: int | None = None,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> list[tuple[str, Totals]]:
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension {dimension!r}; choose from {', '.join(DIMENSIONS)}")
    column = DIMENSIONS[dimension]  # whitelisted, safe to interpolate
    today = today or date.today()
    where, params = _where(range_name, today, tool, model)
    sql = (
        f"SELECT {column} AS key, {_AGG} FROM events {where} GROUP BY {column} "
        f"ORDER BY SUM({_TOTAL}) DESC, key"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [(row["key"] or "(none)", Totals.from_row(row)) for row in conn.execute(sql, params)]


@dataclass(frozen=True)
class HourBucket:
    hour: int
    events: int
    tokens: int


def hourly_distribution(
    conn: sqlite3.Connection,
    range_name: str = "all",
    today: date | None = None,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> list[HourBucket]:
    """24 buckets (local hour 0-23), always complete, zero-filled."""
    today = today or date.today()
    where, params = _where(range_name, today, tool, model)
    found = {
        row["hour"]: row
        for row in conn.execute(
            f"SELECT {_LOCAL_HOUR} AS hour, COUNT(*) AS events, SUM({_TOTAL}) AS tokens "
            f"FROM events {where} GROUP BY hour",
            params,
        )
    }
    return [
        HourBucket(h, found[h]["events"] if h in found else 0, found[h]["tokens"] if h in found else 0)
        for h in range(24)
    ]


def daily_series(
    conn: sqlite3.Connection,
    range_name: str = "all",
    today: date | None = None,
    fill: bool = False,
    *,
    tool: str | None = None,
    model: str | None = None,
    span: tuple[date, date] | None = None,
) -> list[tuple[str, Totals]]:
    """Per-day totals, read from the `daily_stats` view.

    With `fill=True` every calendar day from the range start (or, for lifelong,
    the first active day) through the range end is present, zero-filled. An
    explicit `span` (first, last) overrides those bounds, so several scoped
    series can share exactly the same day axis.
    """
    today = today or date.today()
    start, end = range_bounds(range_name, today)
    where, params = _where_daily(range_name, today, tool, model)
    rows = conn.execute(
        f"""
        SELECT day,
               SUM(events) AS events, SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens, SUM(thinking_tokens) AS thinking_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
               0 AS priced_tokens, 0 AS unpriced_events
        FROM daily_stats {where} GROUP BY day ORDER BY day
        """,
        params,
    )
    found = [(row["day"], Totals.from_row(row)) for row in rows]
    if not fill:
        return found
    by_day = dict(found)
    if span is not None:
        first, last = span
    else:
        first = start or (date.fromisoformat(found[0][0]) if found else end)
        last = max([end] + [date.fromisoformat(d) for d in by_day])
    out = []
    cur = first
    while cur <= last:
        key = cur.isoformat()
        out.append((key, by_day.get(key, Totals())))
        cur += timedelta(days=1)
    return out


@dataclass(frozen=True)
class Comparison:
    """Current range vs. the period right before it (see `previous_period`)."""

    prev_label: str
    prev_start: date
    prev_end: date
    prev_cost_usd: float
    prev_total_tokens: int
    cost_pct_change: float | None    # percent; None when the previous cost is 0
    tokens_pct_change: float | None  # percent; None when the previous total is 0


def comparison(
    conn: sqlite3.Connection,
    range_name: str | CustomRange,
    today: date | None = None,
    *,
    tool: str | None = None,
    model: str | None = None,
    current: Totals | None = None,
) -> Comparison | None:
    """Period-over-period figures under the same tool / model scope; None for lifelong."""
    today = today or date.today()
    prev = previous_period(range_name, today)
    if prev is None:
        return None
    cur = current if current is not None else summarize(conn, range_name, today, tool=tool, model=model)
    before = summarize_span(conn, prev.start, prev.end, tool=tool, model=model)
    return Comparison(
        prev_label=prev.label,
        prev_start=prev.start,
        prev_end=prev.end,
        prev_cost_usd=before.cost_usd,
        prev_total_tokens=before.total_tokens,
        cost_pct_change=pct_change(cur.cost_usd, before.cost_usd),
        tokens_pct_change=pct_change(cur.total_tokens, before.total_tokens),
    )


# --------------------------------------------------------------------------- trend buckets

def resolve_granularity(granularity: str, n_days: int) -> str:
    """'auto' -> day / week / month by span length; explicit values pass through."""
    if granularity == "auto":
        if n_days <= AUTO_DAY_MAX_DAYS:
            return "day"
        return "week" if n_days <= AUTO_WEEK_MAX_DAYS else "month"
    if granularity not in GRANULARITIES:
        raise ValueError(f"unknown granularity {granularity!r}; choose from auto, {', '.join(GRANULARITIES)}")
    return granularity


def bucket_start(d: date, granularity: str) -> date:
    """Calendar bucket containing `d`: the day, its Monday (ISO week), or the 1st of its month."""
    if granularity == "day":
        return d
    if granularity == "week":
        return d - timedelta(days=d.weekday())
    return d.replace(day=1)


def bucket_end(start: date, granularity: str) -> date:
    if granularity == "day":
        return start
    if granularity == "week":
        return start + timedelta(days=6)
    nxt = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


@dataclass(frozen=True)
class Bucket:
    start: str  # calendar bucket start (a Monday for weeks, the 1st for months)
    end: str    # calendar bucket end (may extend past the range; no data is counted there)
    totals: Totals


def bucketize(days: list[tuple[str, Totals]], granularity: str) -> list[Bucket]:
    """Sum a contiguous zero-filled day series into calendar buckets (also zero-filled)."""
    acc: dict[date, Totals] = {}
    for iso, t in days:
        key = bucket_start(date.fromisoformat(iso), granularity)
        acc[key] = acc[key] + t if key in acc else t
    return [
        Bucket(k.isoformat(), bucket_end(k, granularity).isoformat(), acc[k])
        for k in sorted(acc)
    ]


@dataclass(frozen=True)
class Trend:
    granularity: str                     # resolved: day | week | month
    days: list[tuple[str, Totals]]       # combined, daily, zero-filled
    buckets: list[Bucket]                # combined, at `granularity`
    series: list[tuple[str, list[Bucket]]] | None  # (tool, buckets) on the same axis, or None


def trend(
    conn: sqlite3.Connection,
    range_name: str = "lifelong",
    today: date | None = None,
    granularity: str = "auto",
    split_by: str | None = None,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> Trend:
    """Trend data at a fixed or automatic granularity, optionally split per tool.

    Every split series is built over the exact day span of the combined series,
    so all series share identical bucket boundaries and sum to the combined one.
    """
    if split_by not in (None, "none", "tool"):
        raise ValueError(f"unknown split_by {split_by!r}; choose from none, tool")
    today = today or date.today()
    days = daily_series(conn, range_name, today, fill=True, tool=tool, model=model)
    gran = resolve_granularity(granularity, len(days))
    buckets = bucketize(days, gran)
    series = None
    if split_by == "tool":
        span = (date.fromisoformat(days[0][0]), date.fromisoformat(days[-1][0]))
        tools = [k for k, _ in breakdown(conn, "tool", range_name, today, tool=tool, model=model)]
        series = [
            (t, bucketize(daily_series(conn, range_name, today, fill=True, tool=t, model=model, span=span), gran))
            for t in tools
        ]
    return Trend(gran, days, buckets, series)


@dataclass(frozen=True)
class DayCell:
    day: str
    events: int
    tokens: int
    cost_usd: float


def heatmap(
    conn: sqlite3.Connection,
    year: int,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> list[DayCell]:
    """Every calendar day of `year` (local time), zero-filled, from `daily_stats`."""
    where, params = _conditions(date(year, 1, 1), date(year, 12, 31), "day", tool, model)
    rows = {
        r["day"]: r
        for r in conn.execute(
            "SELECT day, SUM(events) AS events, SUM(total_tokens) AS tokens, "
            f"COALESCE(SUM(cost_usd), 0.0) AS cost FROM daily_stats {where} GROUP BY day",
            params,
        )
    }
    out = []
    cur, end = date(year, 1, 1), date(year, 12, 31)
    while cur <= end:
        r = rows.get(cur.isoformat())
        out.append(
            DayCell(cur.isoformat(), r["events"], r["tokens"], r["cost"])
            if r else DayCell(cur.isoformat(), 0, 0, 0.0)
        )
        cur += timedelta(days=1)
    return out


def active_years(
    conn: sqlite3.Connection, *, tool: str | None = None, model: str | None = None
) -> list[int]:
    where, params = _conditions(None, date.today(), "day", tool, model)
    where = f"{where} AND day IS NOT NULL" if where else "WHERE day IS NOT NULL"
    return [
        int(r[0]) for r in conn.execute(
            f"SELECT DISTINCT substr(day, 1, 4) FROM daily_stats {where} ORDER BY 1", params
        )
    ]


@dataclass(frozen=True)
class ToolSummary:
    tool: str
    events: int
    tokens: int
    first_seen: str
    last_seen: str


def tools_summary(conn: sqlite3.Connection) -> list[ToolSummary]:
    """Tools that have at least one event in the ledger, most tokens first."""
    return [
        ToolSummary(r["tool"], r["events"], r["tokens"] or 0, r["first_seen"], r["last_seen"])
        for r in conn.execute(
            f"SELECT tool, COUNT(*) AS events, SUM({_TOTAL}) AS tokens, "
            "MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen "
            "FROM events GROUP BY tool ORDER BY tokens DESC, tool"
        )
    ]


EXPORT_COLUMNS: tuple[str, ...] = (
    "timestamp_utc", "local_date", "tool", "model", "project_name", "session_id",
    "request_id", "message_id", "input_tokens", "output_tokens", "thinking_tokens",
    "cache_read_tokens", "cache_write_tokens", "total_tokens", "cost_usd",
)


def export_rows(
    conn: sqlite3.Connection,
    range_name: str = "all",
    today: date | None = None,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> Iterator[dict[str, object]]:
    """Per-event ledger rows (metadata only) for CSV / JSON export, oldest first."""
    today = today or date.today()
    where, params = _where(range_name, today, tool, model)
    cur = conn.execute(
        f"""
        SELECT timestamp AS timestamp_utc, {_LOCAL_DAY} AS local_date, tool, model,
               project_name, session_id, request_id, message_id,
               input_tokens, output_tokens, thinking_tokens, cache_read_tokens,
               cache_write_tokens, {_TOTAL} AS total_tokens, cost_usd
        FROM events {where} ORDER BY timestamp, id
        """,
        params,
    )
    for row in cur:
        yield {k: row[k] for k in EXPORT_COLUMNS}


@dataclass(frozen=True)
class Streak:
    active_days: int = 0
    longest: int = 0
    longest_start: str | None = None
    longest_end: str | None = None
    current: int = 0


def compute_streak(days: list[date], today: date) -> Streak:
    """Longest run of consecutive active days, plus the run still alive today.

    The current streak is alive if the last active day is today or yesterday.
    """
    uniq = sorted(set(days))
    if not uniq:
        return Streak()
    best_len, best_start, best_end = 1, uniq[0], uniq[0]
    run_len, run_start = 1, uniq[0]
    for prev, cur in zip(uniq, uniq[1:]):
        if cur - prev == timedelta(days=1):
            run_len += 1
        else:
            run_len, run_start = 1, cur
        if run_len > best_len:
            best_len, best_start, best_end = run_len, run_start, cur
    current = 0
    if today - uniq[-1] <= timedelta(days=1) and uniq[-1] <= today:
        current = run_len
    return Streak(
        active_days=len(uniq),
        longest=best_len,
        longest_start=best_start.isoformat(),
        longest_end=best_end.isoformat(),
        current=current,
    )


def streak(
    conn: sqlite3.Connection,
    today: date | None = None,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> Streak:
    today = today or date.today()
    where, params = _conditions(None, today, "day", tool, model)
    days = [
        date.fromisoformat(r[0])
        for r in conn.execute(f"SELECT DISTINCT day FROM daily_stats {where}", params)
    ]
    return compute_streak(days, today)


@dataclass(frozen=True)
class UnpricedModel:
    model: str
    events: int
    tokens: int


def unpriced_models(
    conn: sqlite3.Connection,
    pricing: PricingTable,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> list[UnpricedModel]:
    """Models in the ledger (optionally scoped) that the *current* pricing.json cannot price."""
    out = []
    where, params = _conditions(None, date.today(), _LOCAL_DAY, tool, model)
    for row in conn.execute(
        f"SELECT model, COUNT(*) AS events, SUM({_TOTAL}) AS tokens FROM events {where} "
        f"GROUP BY model ORDER BY tokens DESC",
        params,
    ):
        if pricing.lookup(row["model"]) is None:
            out.append(UnpricedModel(row["model"], row["events"], row["tokens"]))
    return out


# --------------------------------------------------------------------------- cache savings

@dataclass(frozen=True)
class CacheSavings:
    """What prompt caching saved versus paying the full input rate for the same tokens."""

    usd: float = 0.0
    cache_read_tokens: int = 0  # cache-read tokens of priced models (the basis of `usd`)
    events: int = 0             # priced events that read from the cache
    unpriced_cache_read_tokens: int = 0


def cache_savings(
    conn: sqlite3.Connection,
    pricing: PricingTable,
    range_name: str = "all",
    today: date | None = None,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> CacheSavings:
    """sum(cache_read_tokens / 1e6 * max(0, input_rate - cache_read_rate)) over priced events.

    Rates come from the *current* pricing table (the same one Set Price writes), so
    the figure follows pricing.json even before a reprice.
    """
    today = today or date.today()
    where, params = _where(range_name, today, tool, model)
    usd = 0.0
    tokens = events = unpriced = 0
    for row in conn.execute(
        f"SELECT model, COALESCE(SUM(cache_read_tokens), 0) AS cr, "
        f"COALESCE(SUM(CASE WHEN cache_read_tokens > 0 THEN 1 ELSE 0 END), 0) AS ev "
        f"FROM events {where} GROUP BY model",
        params,
    ):
        if not row["cr"]:
            continue
        hit = pricing.lookup(row["model"])
        if hit is None:
            unpriced += row["cr"]
            continue
        price = hit[1]
        usd += row["cr"] / 1_000_000 * max(0.0, price.input - price.cache_read)
        tokens += row["cr"]
        events += row["ev"]
    return CacheSavings(usd=usd, cache_read_tokens=tokens, events=events, unpriced_cache_read_tokens=unpriced)


# --------------------------------------------------------------------------- personal records

@dataclass(frozen=True)
class DayRecord:
    date: str
    total_tokens: int
    cost_usd: float | None
    events: int


@dataclass(frozen=True)
class SessionRecord:
    """The heaviest single session. Deliberately carries no ids, paths or project names."""

    tool: str
    date: str  # local day the session started (within the range)
    total_tokens: int
    cost_usd: float | None


@dataclass(frozen=True)
class StreakRecord:
    days: int
    start: str
    end: str


@dataclass(frozen=True)
class Records:
    peak_day: DayRecord | None = None       # most tokens in one local day
    peak_cost_day: DayRecord | None = None  # highest priced cost in one local day
    heaviest_session: SessionRecord | None = None
    longest_streak: StreakRecord | None = None


def _and(where: str, cond: str) -> str:
    return f"{where} AND {cond}" if where else f"WHERE {cond}"


def records(
    conn: sqlite3.Connection,
    range_name: str = "all",
    today: date | None = None,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> Records:
    """Personal bests inside the range / tool / model scope. Ties go to the earliest."""
    today = today or date.today()
    dwhere, dparams = _where_daily(range_name, today, tool, model)
    per_day = (
        f"SELECT day, SUM(total_tokens) AS t, SUM(cost_usd) AS c, SUM(events) AS e "
        f"FROM daily_stats {dwhere} GROUP BY day"
    )

    def day_record(row) -> DayRecord | None:
        if row is None:
            return None
        return DayRecord(date=row["day"], total_tokens=row["t"] or 0, cost_usd=row["c"], events=row["e"] or 0)

    peak = conn.execute(f"{per_day} ORDER BY t DESC, day ASC LIMIT 1", dparams).fetchone()
    peak_cost = conn.execute(
        f"SELECT * FROM ({per_day}) WHERE c > 0 ORDER BY c DESC, day ASC LIMIT 1", dparams
    ).fetchone()

    ewhere, eparams = _where(range_name, today, tool, model)
    swhere = _and(ewhere, "session_id <> ''")
    sess = conn.execute(
        f"SELECT tool, MIN(timestamp) AS first_ts, SUM({_TOTAL}) AS t, SUM(cost_usd) AS c "
        f"FROM events {swhere} GROUP BY tool, session_id "
        f"ORDER BY t DESC, first_ts ASC LIMIT 1",
        eparams,
    ).fetchone()
    heaviest = None
    if sess is not None and sess["t"]:
        local_day = conn.execute("SELECT date(?, 'localtime')", (sess["first_ts"],)).fetchone()[0]
        heaviest = SessionRecord(tool=sess["tool"], date=local_day, total_tokens=sess["t"], cost_usd=sess["c"])

    days = [date.fromisoformat(r[0]) for r in conn.execute(
        f"SELECT DISTINCT day FROM daily_stats {dwhere}", dparams)]
    st = compute_streak(days, today)
    longest = StreakRecord(st.longest, st.longest_start, st.longest_end) if st.longest else None

    return Records(
        peak_day=day_record(peak) if peak is not None and peak["t"] else None,
        peak_cost_day=day_record(peak_cost),
        heaviest_session=heaviest,
        longest_streak=longest,
    )
