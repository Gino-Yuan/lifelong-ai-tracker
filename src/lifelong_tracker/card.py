"""Shareable dark SVG report card, generated in pure Python.

Privacy by construction: the card's data is built only from aggregate totals,
the per-model breakdown and day-level activity. It never queries project names,
session / request IDs or any path, and model labels are reduced to their last
path segment, stripped of XML-invalid characters, truncated and XML-escaped.

The SVG uses presentation attributes only (no <style>, no style="", no script,
no external fonts or links), so it renders the same in browsers, image viewers
and under the dashboard's strict Content-Security-Policy.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from xml.sax.saxutils import escape, quoteattr

from lifelong_tracker import analytics
from lifelong_tracker.analytics import Totals
from lifelong_tracker.collectors import tool_label

CARD_RANGES: tuple[str, ...] = analytics.WEB_RANGES
WIDTH, HEIGHT = 1200, 630

# Same palette as the dashboard (app.css); category hues in fixed order.
BG, PANEL, BORDER, TRACK = "#0b0d10", "#111418", "#22272e", "#1d2228"
TEXT, TEXT_2, MUTED, FAINT, ACCENT = "#e6e8eb", "#b4bac2", "#8a929c", "#5c636d", "#7aa2ff"
CATEGORY_COLORS = ("#6aa1ff", "#f2a65a", "#b98cff", "#4cc9a6", "#ef6f8f")
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

TOKENS_PER_LINE_OF_CODE = 10
TOKENS_PER_NOVEL = 120_000
MAX_MODEL_LABEL = 28

_XML_INVALID = re.compile("[^\u0009\u000a\u000d -퟿-�\U00010000-\U0010ffff]")


@dataclass(frozen=True)
class CardData:
    range_name: str
    title: str
    period: str
    totals: Totals
    top_models: list[tuple[str, int, float]]  # (label, tokens, share of tokens)
    longest_streak: int
    active_days: int
    generated: date
    scope: str | None = None  # e.g. "Antigravity · gemini-4-neon-low" when filtered


# --------------------------------------------------------------------------- text helpers

def clean_text(value: object) -> str:
    """Drop characters that are illegal in XML 1.0 and collapse whitespace."""
    return " ".join(_XML_INVALID.sub("", str(value)).split())


def model_label(raw: str | None) -> str:
    """Public label for a model id: last path segment only, bounded length."""
    s = clean_text(raw or "")
    s = re.split(r"[/\\]", s)[-1].strip()
    if not s:
        return "(unknown model)"
    return s if len(s) <= MAX_MODEL_LABEL else s[: MAX_MODEL_LABEL - 1].rstrip() + "…"


def _fmt_period(start: date | None, end: date) -> str:
    if start is None:
        return "No activity yet"
    return analytics.fmt_span(start, end)


def fmt_compact(n: float) -> str:
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            v = n / div
            return f"{v:.2f}{unit}" if v < 100 else f"{v:.1f}{unit}" if v < 1000 else f"{v:.0f}{unit}"
    return f"{int(n):,}"


def fmt_usd(x: float) -> str:
    if x >= 100_000:
        return "$" + fmt_compact(x)
    return f"${x:,.2f}"


def fmt_amount(x: float) -> str:
    if x >= 1_000_000:
        return fmt_compact(x)
    if x >= 10:
        return f"{round(x):,}"
    if x >= 1:
        return f"{x:.1f}"
    return f"{x:.2f}"


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def card_title(range_name: "str | analytics.CustomRange", today: date) -> str:
    if isinstance(range_name, analytics.CustomRange):
        return analytics.fmt_span(range_name.start, range_name.end)
    return {
        "today": "Today's AI Usage",
        "7d": "Last 7 Days of AI Usage",
        "30d": "Last 30 Days of AI Usage",
        "90d": "Last 90 Days of AI Usage",
        "ytd": f"{today.year} AI Usage",
        "last_year": f"{today.year - 1} AI Usage",
        "lifelong": "Lifelong AI Usage",
    }[range_name]


def fun_facts(t: Totals) -> tuple[str, str]:
    lines = t.output_tokens / TOKENS_PER_LINE_OF_CODE
    novels = (t.input_tokens + t.cache_read_tokens + t.cache_write_tokens) / TOKENS_PER_NOVEL
    return (
        f"≈ {fmt_amount(lines)} lines of code written",
        f"≈ {fmt_amount(novels)} novels read",
    )


# --------------------------------------------------------------------------- data

def scope_label(tool: str | None, model: str | None) -> str | None:
    """Public label for a dashboard filter: tool display name and/or model label."""
    parts = []
    if tool:
        parts.append(clean_text(tool_label(tool))[:MAX_MODEL_LABEL])
    if model:
        parts.append(model_label(model))
    return " · ".join(parts) or None


def build_card_data(
    conn: sqlite3.Connection,
    range_name: "str | analytics.CustomRange",
    today: date | None = None,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> CardData:
    custom = isinstance(range_name, analytics.CustomRange)
    if not custom and range_name not in CARD_RANGES:
        raise ValueError(f"unknown range {range_name!r}; choose from {', '.join(CARD_RANGES)}")
    today = today or date.today()
    scope = {"tool": tool, "model": model}
    totals = analytics.summarize(conn, range_name, today, **scope)
    active = [
        date.fromisoformat(d)
        for d, t in analytics.daily_series(conn, range_name, today, **scope) if t.events
    ]
    start, end = analytics.range_bounds(range_name, today)
    st = analytics.compute_streak(active, end)
    if range_name == "lifelong":
        start = min(active) if active else None
    models = [
        (model_label(key), t.total_tokens, analytics.safe_ratio(t.total_tokens, totals.total_tokens))
        for key, t in analytics.breakdown(conn, "model", range_name, today, 3, **scope)
    ]
    return CardData(
        range_name=analytics.range_name_of(range_name),
        title=card_title(range_name, today),
        # a custom card's title is already its dates; the header says how long it is
        period=f"Custom range · {_plural(range_name.days, 'day')}" if custom else _fmt_period(start, end),
        totals=totals,
        top_models=models,
        longest_streak=st.longest,
        active_days=st.active_days,
        generated=today,
        scope=scope_label(tool, model),
    )


# --------------------------------------------------------------------------- render

def _text(x: float, y: float, content: str, *, size: int, fill: str = TEXT,
          weight: int | None = None, anchor: str | None = None, spacing: float | None = None) -> str:
    attrs = [f'x="{x:g}"', f'y="{y:g}"', f'font-size="{size}"', f"fill={quoteattr(fill)}"]
    if weight:
        attrs.append(f'font-weight="{weight}"')
    if anchor:
        attrs.append(f'text-anchor="{anchor}"')
    if spacing:
        attrs.append(f'letter-spacing="{spacing:g}"')
    return f"<text {' '.join(attrs)}>{escape(clean_text(content))}</text>"


def _label(x: float, y: float, content: str) -> str:
    return _text(x, y, content.upper(), size=13, fill=MUTED, weight=600, spacing=1.6)


def _mix_bar(t: Totals, x: float, y: float, w: float, h: float) -> list[str]:
    out = [
        f'<clipPath id="mix-clip"><rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{h / 2:g}"/></clipPath>',
        f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{h / 2:g}" fill="{TRACK}"/>',
    ]
    cats = [(c, v) for c, (_, v, _) in zip(CATEGORY_COLORS, t.categories()) if v > 0]
    total = t.total_tokens
    if not total:
        return out
    gap = 2.0
    usable = w - gap * (len(cats) - 1)
    cx = x
    segs = []
    for i, (color, value) in enumerate(cats):
        sw = usable * value / total
        if i == len(cats) - 1:
            sw = x + w - cx  # absorb rounding
        segs.append(f'<rect x="{cx:.2f}" y="{y:g}" width="{max(sw, 0):.2f}" height="{h:g}" fill="{color}"/>')
        cx += sw + gap
    out.append(f'<g clip-path="url(#mix-clip)">{"".join(segs)}</g>')
    return out


def render_svg(data: CardData) -> str:
    t = data.totals
    left, right = 72, WIDTH - 72
    parts: list[str] = []
    add = parts.append

    rate = t.cache_hit_rate
    hit = "—" if rate is None else f"{rate * 100:.1f}%"
    hit_desc = "no cache breakdown" if rate is None else f"{hit} cache hit"
    desc = (
        f"{data.title} ({data.period}): {fmt_usd(t.cost_usd)} equivalent cost, "
        f"{fmt_compact(t.total_tokens)} tokens, {hit_desc}, "
        f"longest streak {_plural(data.longest_streak, 'day')}."
    )
    add(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="card-title card-desc" '
        f"font-family={quoteattr(FONT)}>"
    )
    add(f'<title id="card-title">{escape(clean_text(data.title))}</title>')
    add(f'<desc id="card-desc">{escape(clean_text(desc))}</desc>')
    add(f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{BG}"/>')
    add(f'<rect x="24" y="24" width="{WIDTH - 48}" height="{HEIGHT - 48}" rx="20" '
        f'fill="{PANEL}" stroke="{BORDER}" stroke-width="1"/>')

    # header: brand mark + name, period on the right
    add(f'<g fill="{ACCENT}">'
        f'<rect x="{left}" y="74" width="5" height="10" rx="1.5"/>'
        f'<rect x="{left + 8}" y="68" width="5" height="16" rx="1.5"/>'
        f'<rect x="{left + 16}" y="62" width="5" height="22" rx="1.5"/></g>')
    add(_text(left + 32, 84, "LIFELONG AI TRACKER", size=14, fill=MUTED, weight=600, spacing=2))
    add(_text(right, 84, data.period, size=16, fill=TEXT_2, anchor="end"))
    add(_text(left, 152, data.title, size=44, weight=700, spacing=-0.5))
    if data.scope:
        add(_text(right, 150, data.scope, size=18, fill=ACCENT, weight=600, anchor="end"))

    # headline stats
    stats = [
        (left, "Total Cost", fmt_usd(t.cost_usd), 52, f"{fmt_usd(t.cost_per_mtok)} per 1M tokens"),
        (left + 330, "Total Tokens", fmt_compact(t.total_tokens), 52, _plural(t.events, "request")),
        (left + 630, "Cache Hit", hit, 40,
         "No cache breakdown" if rate is None else "of input-side tokens"),
        (left + 840, "Longest Streak", _plural(data.longest_streak, "day"), 40,
         f"{_plural(data.active_days, 'active day')}"),
    ]
    for x, label, value, size, sub in stats:
        add(_label(x, 214, label))
        add(_text(x, 270, value, size=size, weight=700, spacing=-1))
        add(_text(x, 300, sub, size=14, fill=FAINT))

    # token mix
    add(_label(left, 350, "Token Mix"))
    parts.extend(_mix_bar(t, left, 366, right - left, 14))
    col_w = (right - left) / 5
    for i, ((name, _, share), color) in enumerate(zip(t.categories(), CATEGORY_COLORS)):
        x = left + i * col_w
        add(f'<rect x="{x:g}" y="402" width="10" height="10" rx="2" fill="{color}"/>')
        add(f'<text x="{x + 18:g}" y="412" font-size="15" fill="{TEXT_2}">{escape(name)}'
            f'<tspan dx="8" font-weight="600" fill="{TEXT}">{share * 100:.1f}%</tspan></text>')

    # top models
    add(_label(left, 464, "Top Models"))
    if data.top_models:
        for i, (name, tokens, share) in enumerate(data.top_models):
            y = 496 + i * 32
            add(_text(left, y, str(i + 1), size=16, fill=FAINT, weight=600))
            add(_text(left + 26, y, name, size=18))
            add(_text(left + 540, y, fmt_compact(tokens), size=16, fill=TEXT_2, anchor="end"))
            add(_text(left + 610, y, f"{share * 100:.1f}%", size=16, fill=MUTED, anchor="end"))
    else:
        add(_text(left, 496, "No usage recorded in this period", size=16, fill=FAINT))

    # fun equivalence
    fx = left + 700
    written, read = fun_facts(t)
    add(_label(fx, 464, "Equivalent To"))
    add(_text(fx, 500, written, size=20, fill=TEXT))
    add(_text(fx, 532, read, size=20, fill=TEXT))
    add(_text(fx, 560, f"~{TOKENS_PER_LINE_OF_CODE} tokens per line · ~120K tokens per novel",
              size=12, fill=FAINT))

    foot = f"Generated locally on {analytics.fmt_span(data.generated, data.generated)} · equivalent API cost"
    if t.unpriced_events:
        foot += f" · excludes {_plural(t.unpriced_events, 'unpriced request')}"
    add(_text(left, 590, foot, size=12, fill=FAINT))
    add(_text(right, 590, "Lifelong AI Tracker", size=12, fill=FAINT, anchor="end"))
    add("</svg>")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(parts) + "\n"


def render_card(
    conn: sqlite3.Connection,
    range_name: "str | analytics.CustomRange",
    today: date | None = None,
    *,
    tool: str | None = None,
    model: str | None = None,
) -> str:
    return render_svg(build_card_data(conn, range_name, today, tool=tool, model=model))
