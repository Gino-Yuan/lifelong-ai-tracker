"""Optional `subscriptions.json`: each tool's subscription plans as dated periods.

Current shape (keys starting with `_` at the top level are comments, kept on save)::

    {
      "_comment": "...",
      "subscriptions": {
        "claude-code": [
          {"start": "2026-03-15", "end": "2026-07-31", "monthly_usd": 20.0},
          {"start": "2026-08-01", "end": null, "monthly_usd": 100.0}
        ],
        "codex": [{"start": "2026-08-01", "end": "2026-11-10", "monthly_usd": 20.0}]
      }
    }

* `start` is a required `YYYY-MM-DD`; `end` is `YYYY-MM-DD` (inclusive, >= start)
  or `null` / omitted for an ongoing plan, which runs up to today.
* `monthly_usd` is a finite number in [0, MAX_MONTHLY_USD].
* A tool's periods must not overlap (the next start is after the previous end;
  an ongoing period must be the last one).

Legacy shapes still load: a bare number per tool (``"claude-code": 20.0``), either
at the top level (the old flat file) or inside `subscriptions`, is one undated
plan that covers every day. Saving writes the current shape; a legacy number is
kept as a number.

Subscription ROI for a range = API-equivalent value / plan cost, where for each
paid period (fee > 0) of each tool in scope, with ``overlap`` = the days of
``[start, min(end or today, today)]`` inside the range (inclusive):

    plan cost = sum(overlap_days / DAYS_PER_MONTH * monthly_usd)
    value     = priced cost_usd of that tool's events on those overlap days

Days after a plan ends cost $0 and add no value. An unbounded (lifelong) range
starts at the earliest of the first active day of the paid tools in scope and
their earliest plan start. The model filter is deliberately ignored: a plan pays
for the whole tool, not for one model.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from lifelong_tracker import analytics
from lifelong_tracker.collectors import COLLECTORS, TOOL_ORDER
from lifelong_tracker.pricing import _atomic_write_text

MAX_MONTHLY_USD = 10_000.0
MAX_PERIODS = 48  # per tool
DAYS_PER_MONTH = 365.2425 / 12  # mean Gregorian month, 30.436875 days
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PERIOD_KEYS = {"start", "end", "monthly_usd"}


class SubscriptionsError(ValueError):
    """subscriptions.json (or a submitted plan list) is unreadable or invalid."""


@dataclass(frozen=True)
class Period:
    """One plan. `start` is None only for a legacy undated plan (then `end` is None too)."""

    start: date | None
    end: date | None  # None = ongoing
    monthly_usd: float

    @property
    def legacy(self) -> bool:
        return self.start is None

    def covers(self, day: date) -> bool:
        return (self.start is None or self.start <= day) and (self.end is None or day <= self.end)

    def as_json(self) -> Any:
        if self.legacy:
            return self.monthly_usd
        return {
            "start": self.start.isoformat(),
            "end": None if self.end is None else self.end.isoformat(),
            "monthly_usd": self.monthly_usd,
        }


Plans = dict[str, list[Period]]


# --------------------------------------------------------------------------- validation

def _fee(tool: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SubscriptionsError(f"{tool}: monthly fee must be a number")
    fee = float(value)
    if not math.isfinite(fee) or fee < 0 or fee > MAX_MONTHLY_USD:
        raise SubscriptionsError(f"{tool}: monthly fee must be between 0 and {MAX_MONTHLY_USD:g} USD")
    return fee


def _day(tool: str, field: str, value: Any) -> date:
    if not isinstance(value, str) or not _DATE.match(value):
        raise SubscriptionsError(f"{tool}: {field} must be a date in YYYY-MM-DD form")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SubscriptionsError(f"{tool}: {field} {value!r} is not a real calendar date") from None


def _period(tool: str, raw: Any) -> Period:
    if not isinstance(raw, dict):
        raise SubscriptionsError(f"{tool}: each period must be an object with start, end and monthly_usd")
    extra = set(raw) - _PERIOD_KEYS
    if extra:
        raise SubscriptionsError(f"{tool}: unknown period field(s): {', '.join(sorted(map(str, extra)))}")
    if "start" not in raw:
        raise SubscriptionsError(f"{tool}: every period needs a start date")
    if "monthly_usd" not in raw:
        raise SubscriptionsError(f"{tool}: every period needs monthly_usd")
    start = _day(tool, "start", raw["start"])
    end_raw = raw.get("end")
    end = None if end_raw is None else _day(tool, "end", end_raw)
    if end is not None and end < start:
        raise SubscriptionsError(f"{tool}: end {end} is before start {start}")
    return Period(start, end, _fee(tool, raw["monthly_usd"]))


def _periods(tool: str, raw: Any) -> list[Period]:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return [Period(None, None, _fee(tool, raw))]
    if not isinstance(raw, list):
        raise SubscriptionsError(f"{tool}: monthly fee must be a number, or a list of periods")
    if len(raw) > MAX_PERIODS:
        raise SubscriptionsError(f"{tool}: at most {MAX_PERIODS} periods")
    periods = sorted((_period(tool, p) for p in raw), key=lambda p: p.start)
    for prev, nxt in zip(periods, periods[1:]):
        if prev.end is None:
            raise SubscriptionsError(
                f"{tool}: the ongoing period from {prev.start} overlaps the period from {nxt.start}; "
                "give it an end date")
        if nxt.start <= prev.end:
            raise SubscriptionsError(
                f"{tool}: periods overlap ({prev.start}..{prev.end} and {nxt.start}..); "
                f"the next one must start on or after {prev.end + timedelta(days=1)}")
    return periods


def validate_plans(raw: Any) -> Plans:
    """{tool: [Period]} from a mapping of tool -> number | [period, ...]; `_...` keys are skipped."""
    if not isinstance(raw, dict):
        raise SubscriptionsError("subscriptions must be a JSON object of {tool: [periods]}")
    plans: Plans = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise SubscriptionsError("tool ids must be strings")
        if key.startswith("_"):
            continue
        if key not in COLLECTORS:
            raise SubscriptionsError(f"unknown tool {key!r}; known: {', '.join(TOOL_ORDER)}")
        plans[key] = _periods(key, value)
    return plans


def validate_file(raw: Any) -> Plans:
    """Plans from a parsed subscriptions.json, current (`subscriptions`) or legacy flat shape."""
    if not isinstance(raw, dict):
        raise SubscriptionsError("subscriptions file must contain a JSON object")
    if "subscriptions" in raw:
        extra = [k for k in raw if isinstance(k, str) and k != "subscriptions" and not k.startswith("_")]
        if extra:
            raise SubscriptionsError(
                f"unexpected top-level key(s) next to \"subscriptions\": {', '.join(sorted(extra))}")
        return validate_plans(raw["subscriptions"])
    return validate_plans(raw)


def as_plans(value: Mapping[str, Any]) -> Plans:
    """Accept Plans, or a legacy {tool: fee} / raw {tool: [period dicts]} mapping."""
    if all(isinstance(v, list) and all(isinstance(p, Period) for p in v) for v in value.values()):
        return {k: list(v) for k, v in value.items()}
    return validate_plans(dict(value))


# --------------------------------------------------------------------------- file io

def _read_raw(path: Path) -> dict[str, Any] | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SubscriptionsError(f"cannot read subscriptions file: {exc.strerror or exc}") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise SubscriptionsError(f"subscriptions file is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SubscriptionsError("subscriptions file must contain a JSON object")
    return raw


def load_subscriptions(path: Path) -> Plans:
    """Plans from `path`; an absent file is an empty mapping."""
    raw = _read_raw(path)
    return {} if raw is None else validate_file(raw)


def plans_json(plans: Plans) -> dict[str, Any]:
    """The `subscriptions` object as written to disk (tools in TOOL_ORDER, periods by start)."""
    out: dict[str, Any] = {}
    for tool in TOOL_ORDER:
        if tool not in plans or not plans[tool]:
            continue
        periods = plans[tool]
        out[tool] = periods[0].as_json() if len(periods) == 1 and periods[0].legacy else [
            p.as_json() for p in periods]
    return out


def save_subscriptions(path: Path, plans: Mapping[str, Any]) -> Plans:
    """Replace the plans in `path` atomically (top-level comments survive). Returns what was stored."""
    clean = as_plans(plans)
    path = Path(path)
    try:
        old = _read_raw(path) or {}
    except SubscriptionsError:
        old = {}  # an unreadable / broken file is replaced by a valid one
    comments = {k: v for k, v in old.items() if isinstance(k, str) and k.startswith("_")}
    body = {**comments, "subscriptions": plans_json(clean)}
    text = json.dumps(body, indent=2, ensure_ascii=False) + "\n"
    stored = validate_file(json.loads(text))  # never write what would not load back
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, text)
    return stored


def fee_on(periods: list[Period], day: date) -> float:
    """Monthly fee in force on `day` (0.0 when no period covers it)."""
    return sum(p.monthly_usd for p in periods if p.covers(day))


# --------------------------------------------------------------------------- ROI

def _overlap(p: Period, lo: date, hi: date, today: date) -> tuple[date, date] | None:
    """Inclusive [first, last] days of `p` (ended at today at the latest) inside [lo, hi]."""
    first = max(lo, p.start) if p.start is not None else lo
    last = min(hi, today, p.end) if p.end is not None else min(hi, today)
    return (first, last) if first <= last else None


@dataclass(frozen=True)
class ToolValue:
    tool: str
    monthly_usd: float       # fee in force on the range's last day
    paid_days: int           # days of the range covered by a paid period
    plan_cost_usd: float
    value_usd: float
    unpriced_events: int


@dataclass(frozen=True)
class Roi:
    monthly_usd: float
    start: date
    end: date
    days: int
    plan_cost_usd: float
    value_usd: float
    multiple: float | None   # None when no paid plan overlaps the range
    unpriced_events: int
    tools: list[ToolValue]


def subscription_roi(
    conn: sqlite3.Connection,
    plans: Mapping[str, Any],
    range_name: str,
    today: date,
    *,
    tool: str | None = None,
) -> Roi | None:
    """ROI for the range, or None when no tool in scope has a paid (fee > 0) period at all."""
    all_plans = as_plans(plans)
    paid = {
        t: [p for p in ps if p.monthly_usd > 0]
        for t, ps in all_plans.items() if tool is None or t == tool
    }
    paid = {t: ps for t, ps in paid.items() if ps}
    if not paid:
        return None
    start, end = analytics.range_bounds(range_name, today)
    if start is None:
        marks = ",".join("?" * len(paid))
        first = conn.execute(
            f"SELECT MIN(day) FROM daily_stats WHERE tool IN ({marks})", list(paid)
        ).fetchone()[0]
        candidates = [date.fromisoformat(first)] if first else []
        candidates += [p.start for ps in paid.values() for p in ps if p.start is not None]
        start = min(min(candidates), end) if candidates else end
    days = max(1, (end - start).days + 1)

    rows: list[ToolValue] = []
    for t in [x for x in TOOL_ORDER if x in paid]:
        cost = value = 0.0
        covered = unpriced = 0
        for p in paid[t]:
            span = _overlap(p, start, end, today)
            if span is None:
                continue
            n = (span[1] - span[0]).days + 1
            covered += n
            cost += n / DAYS_PER_MONTH * p.monthly_usd
            tot = analytics.summarize_span(conn, span[0], span[1], tool=t)
            value += tot.cost_usd
            unpriced += tot.unpriced_events
        rows.append(ToolValue(t, fee_on(paid[t], end), covered, cost, value, unpriced))

    plan = sum(r.plan_cost_usd for r in rows)
    value = sum(r.value_usd for r in rows)
    return Roi(
        monthly_usd=sum(r.monthly_usd for r in rows), start=start, end=end, days=days,
        plan_cost_usd=plan, value_usd=value,
        multiple=(value / plan) if plan > 0 else None,
        unpriced_events=sum(r.unpriced_events for r in rows), tools=rows,
    )
