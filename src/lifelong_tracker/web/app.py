"""FastAPI app: JSON API + static dashboard.

All numbers come from `lifelong_tracker.analytics` / `storage`; this module only
validates parameters and shapes JSON. Every GET opens the database with
`connect_readonly` per request (PRAGMA query_only), so reads can never modify
the ledger.

`GET /api/pricing` lists the current price table (read-only). The single write
endpoint is `POST /api/pricing`: it atomically rewrites one entry of
pricing.json (stamping `_meta.updated` with today's date) and then reprices the
ledger, which only ever updates the
`events.cost_usd` column (raw token counts are never touched). Unsafe methods
are additionally guarded against cross-site requests (JSON-only, same-origin
`Origin` / `Sec-Fetch-Site`, small bodies) on top of the loopback Host check.

No `from __future__ import annotations` here: FastAPI must see the real
(closure-local) dependency annotations, not strings.
"""

import csv
import io
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Callable, Iterator, Literal, Optional, Union
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lifelong_tracker import __version__, analytics
from lifelong_tracker import sources as sources_mod
from lifelong_tracker.card import render_card
from lifelong_tracker.collectors import TOOL_ORDER, tool_label
from lifelong_tracker.pricing import (
    MODEL_KEY,
    PROVIDERS,
    PricingTable,
    base_model_name,
    model_family,
    normalize_model,
    set_model_price,
)
from lifelong_tracker.storage import connect, connect_readonly, pricing_drift, reprice
from lifelong_tracker.subscriptions import (
    DAYS_PER_MONTH,
    MAX_MONTHLY_USD,
    MAX_PERIODS,
    SubscriptionsError,
    api_spend,
    billing_rules,
    fee_on,
    load_subscriptions,
    save_subscriptions,
    subscription_roi,
)
from lifelong_tracker.web.security import SECURITY_HEADERS, host_header_name, is_loopback_host

STATIC_DIR = Path(__file__).parent / "static"

RangeName = Literal["today", "7d", "30d", "90d", "ytd", "last_year", "lifelong", "custom"]
CATEGORY_KEYS = ("input", "output", "thinking", "cache_read", "cache_write")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
MAX_WRITE_BODY = 4096
MAX_PRICE = 100_000.0  # USD per 1M tokens; anything above is certainly a typo
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    """Filename-safe fragment (also keeps Content-Disposition headers well-formed)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")[:60] or "x"


def _series_values(t) -> dict:
    return {
        "events": t.events,
        "input_tokens": t.input_tokens,
        "output_tokens": t.output_tokens,
        "thinking_tokens": t.thinking_tokens,
        "cache_read_tokens": t.cache_read_tokens,
        "cache_write_tokens": t.cache_write_tokens,
        "total_tokens": t.total_tokens,
        "cost_usd": t.cost_usd,
    }


def _point(bucket) -> dict:
    return {"start": bucket.start, "end": bucket.end, **_series_values(bucket.totals)}


# --------------------------------------------------------------------------- scope

@dataclass(frozen=True)
class Scope:
    """Optional exact tool / model filter shared by every statistics endpoint."""

    tool: Optional[str] = None
    model: Optional[str] = None

    @property
    def kw(self) -> dict:
        return {"tool": self.tool, "model": self.model}

    def as_dict(self) -> dict:
        return {
            "tool": self.tool,
            "tool_label": tool_label(self.tool) if self.tool else "All Tools",
            "model": self.model,
        }

    def file_part(self) -> str:
        parts = [_slug(p) for p in (self.tool, self.model) if p]
        return "".join(f"_{p}" for p in parts)


def get_scope(
    tool: Annotated[
        Optional[str],
        Query(max_length=64, pattern=r"^[A-Za-z0-9_-]*$",
              description="Tool id (e.g. claude-code, antigravity); omit or 'all' for every tool"),
    ] = None,
    model: Annotated[
        Optional[str],
        Query(max_length=256, description="Exact model id; omit or 'all' for every model"),
    ] = None,
) -> Scope:
    t = (tool or "").strip().lower().replace("_", "-")  # 'claude_code' == 'claude-code'
    m = (model or "").strip()
    if _CONTROL_CHARS.search(m):
        raise HTTPException(422, "model must not contain control characters")
    return Scope(tool=None if t in ("", "all") else t, model=None if m in ("", "all") else m)


ScopeDep = Annotated[Scope, Depends(get_scope)]


def get_range(
    range_name: Annotated[RangeName, Query(alias="range", description="Time range")] = "lifelong",
    start: Annotated[Optional[str], Query(max_length=10, description="range=custom: first day, YYYY-MM-DD")] = None,
    end: Annotated[Optional[str], Query(max_length=10, description="range=custom: last day, YYYY-MM-DD")] = None,
) -> Union[str, analytics.CustomRange]:
    """A preset range name, or a validated inclusive custom window (422 otherwise).

    `start` / `end` belong to `range=custom` only; sending them with a preset is
    rejected rather than silently ignored, so a stale link never shows the wrong span.
    """
    if range_name == analytics.CUSTOM:
        try:
            return analytics.CustomRange.parse(start, end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    if start is not None or end is not None:
        raise HTTPException(422, "start and end are only accepted with range=custom")
    return range_name


RangeDep = Annotated[Union[str, analytics.CustomRange], Depends(get_range)]


def _range_fields(rng) -> dict:
    out = {"range": analytics.range_name_of(rng), "range_label": analytics.range_label(rng)}
    if isinstance(rng, analytics.CustomRange):
        out["range_start"] = rng.start.isoformat()
        out["range_end"] = rng.end.isoformat()
    return out


def _comparison_dict(cmp: Optional["analytics.Comparison"]) -> Optional[dict]:
    if cmp is None:
        return None
    return {
        "prev_label": cmp.prev_label,
        "prev_start": cmp.prev_start.isoformat(),
        "prev_end": cmp.prev_end.isoformat(),
        "prev_cost_usd": cmp.prev_cost_usd,
        "prev_total_tokens": cmp.prev_total_tokens,
        "cost_pct_change": cmp.cost_pct_change,
        "tokens_pct_change": cmp.tokens_pct_change,
    }


# --------------------------------------------------------------------------- pricing

def _price_row(provider: str, key: str, price, segments, used: bool) -> dict:
    """One /api/pricing row: the latest price, when it took effect and the older prices."""
    latest = segments[-1].effective_from
    return {
        "model": key,
        "provider": provider,
        **{f: getattr(price, f) for f in CATEGORY_KEYS},
        "used": used,
        "effective_from": None if latest is None else latest.isoformat(),
        "history": [
            {"effective_from": None if s.effective_from is None else s.effective_from.isoformat(),
             **{f: getattr(s.price, f) for f in CATEGORY_KEYS}}
            for s in segments[:-1]
        ],
    }


class _PricingCache:
    """Reload pricing.json whenever its mtime changes (edits show up without restart)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._mtime: Optional[int] = None
        self._table: Optional[PricingTable] = None
        self._meta: dict = {}

    def _load(self) -> None:
        mtime = self.path.stat().st_mtime_ns
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        table = PricingTable.from_dict(raw)
        meta = raw.get("_meta") if isinstance(raw, dict) else None
        self._table, self._meta, self._mtime = table, meta if isinstance(meta, dict) else {}, mtime

    def get(self) -> PricingTable:
        try:
            if self._table is None or self.path.stat().st_mtime_ns != self._mtime:
                self._load()
        except (OSError, ValueError) as exc:
            raise HTTPException(500, f"Cannot load pricing file: {exc}") from exc
        return self._table

    def meta(self) -> dict:
        self.get()
        return self._meta

    def put(self, table: PricingTable) -> None:
        """Install a table we just wrote ourselves (no reliance on mtime granularity)."""
        try:
            self._load()  # same content we wrote; also picks up the new _meta
        except (OSError, ValueError):
            self._table, self._mtime = table, None


Price = Annotated[float, Field(ge=0, le=MAX_PRICE)]


class PriceIn(BaseModel):
    """Body of POST /api/pricing. Unknown fields, strings for numbers, negatives,
    NaN / Infinity and malformed model names are all rejected with 422."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    model: str = Field(min_length=1, max_length=200)
    input: Price
    output: Price
    thinking: Optional[Price] = None  # omitted -> same as output
    cache_read: Price
    cache_write: Price

    @field_validator("model")
    @classmethod
    def _valid_model(cls, value: str) -> str:
        if _CONTROL_CHARS.search(value) or ".." in value or "\\" in value:
            raise ValueError("invalid characters in model name")
        if value.strip().startswith("/") or value.strip().endswith("/"):
            raise ValueError("invalid model name")
        if not MODEL_KEY.match(normalize_model(value)):
            raise ValueError(
                "model name may only contain letters, digits and . _ : + [ ] - "
                "(provider prefixes like 'anthropic/' are stripped)"
            )
        return value.strip()


Fee = Annotated[float, Field(ge=0, le=MAX_MONTHLY_USD)]
ToolKey = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]


IsoDay = Annotated[str, Field(min_length=10, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")]


class PeriodIn(BaseModel):
    """One dated plan; `end` None / omitted = ongoing. Calendar and overlap checks are in
    `subscriptions.validate_plans` (422 too)."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    start: IsoDay
    end: Optional[IsoDay] = None
    monthly_usd: Fee


class SubscriptionsIn(BaseModel):
    """Body of POST /api/subscriptions: the complete plan set, replacing the file.

    `subscriptions`: {tool: [period, ...] | monthly USD (legacy undated plan)}.
    `fees`: the legacy {tool: monthly USD} body, still accepted. Exactly one of the two.
    """

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    subscriptions: Optional[Annotated[
        dict[ToolKey, Union[Fee, Annotated[list[PeriodIn], Field(max_length=MAX_PERIODS)]]],
        Field(max_length=32),
    ]] = None
    fees: Optional[Annotated[dict[ToolKey, Fee], Field(max_length=32)]] = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "SubscriptionsIn":
        if (self.subscriptions is None) == (self.fees is None):
            raise ValueError('send exactly one of "subscriptions" or "fees"')
        return self

    def raw(self) -> dict:
        if self.fees is not None:
            return dict(self.fees)
        return {
            tool: v if isinstance(v, (int, float)) else [p.model_dump() for p in v]
            for tool, v in self.subscriptions.items()
        }


def _day_record(r) -> Optional[dict]:
    if r is None:
        return None
    return {"date": r.date, "total_tokens": r.total_tokens, "cost_usd": r.cost_usd, "events": r.events}


def _records_dict(rec: "analytics.Records") -> dict:
    s = rec.heaviest_session
    st = rec.longest_streak
    return {
        "peak_day": _day_record(rec.peak_day),
        "peak_cost_day": _day_record(rec.peak_cost_day),
        # tool, day and totals only: never a session id, path, project or text
        "heaviest_session": None if s is None else {
            "tool": s.tool, "date": s.date, "total_tokens": s.total_tokens, "cost_usd": s.cost_usd,
        },
        "longest_streak": None if st is None else {"days": st.days, "start": st.start, "end": st.end},
    }


def _same_origin_violation(request: Request) -> Optional[str]:
    """Why an unsafe request looks cross-site, or None if it is acceptable."""
    ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        return "415:Content-Type must be application/json"
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        return "411:Content-Length required"
    if int(length) > MAX_WRITE_BODY:
        return "413:Request body too large"
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return "403:Cross-site requests are not allowed"
    origin = request.headers.get("origin")
    if origin is not None:
        parts = urlsplit(origin)
        if (
            parts.scheme not in ("http", "https")
            or not is_loopback_host(parts.hostname or "")
            or parts.netloc.lower() != request.headers.get("host", "").lower()
        ):
            return "403:Cross-origin requests are not allowed"
    return None


# --------------------------------------------------------------------------- app

def create_app(
    db_path: Path,
    pricing_path: Path,
    today: Callable[[], date] = date.today,
    *,
    subscriptions_path: Optional[Path] = None,
    sources_path: Optional[Path] = None,
) -> FastAPI:
    """`subscriptions_path` / `sources_path` = None disables editing fees / Sync Now."""
    app = FastAPI(
        title="Lifelong AI Tracker",
        version=__version__,
        docs_url=None,  # Swagger / ReDoc pull assets from a CDN - disabled (offline)
        redoc_url=None,
        openapi_url=None,
    )
    pricing_cache = _PricingCache(pricing_path)
    write_lock = threading.Lock()   # pricing.json / subscriptions.json writes
    sync_lock = threading.Lock()    # one Sync Now at a time

    def read_fees() -> tuple[dict, Optional[str]]:
        if subscriptions_path is None:
            return {}, None
        try:
            return load_subscriptions(subscriptions_path), None
        except SubscriptionsError as exc:
            return {}, str(exc)

    def read_sources() -> tuple[list, Optional[str]]:
        """sources.json specs (for their `billing` mode); none when absent or invalid."""
        if sources_path is None or not Path(sources_path).is_file():
            return [], None
        try:
            return sources_mod.load_sources(sources_path), None
        except sources_mod.SourcesConfigError as exc:
            return [], str(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo submitted values back (they may be NaN / Infinity, which are not
        # JSON-serialisable, or arbitrary junk): location + message only.
        detail = [
            {"loc": [str(x) for x in e.get("loc", ())], "msg": str(e.get("msg", "")),
             "type": str(e.get("type", ""))}
            for e in exc.errors()
        ]
        return JSONResponse({"detail": detail}, status_code=422)

    @app.middleware("http")
    async def loopback_guard(request: Request, call_next):
        if not is_loopback_host(host_header_name(request.headers.get("host", ""))):
            return PlainTextResponse("Forbidden: dashboard is served on loopback only.", 403)
        if request.method not in SAFE_METHODS:
            problem = _same_origin_violation(request)
            if problem:
                code, msg = problem.split(":", 1)
                response = PlainTextResponse(msg, int(code))
                response.headers.update(SECURITY_HEADERS)
                return response
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_conn() -> Iterator:
        with connect_readonly(db_path) as conn:
            yield conn

    Conn = Annotated[object, Depends(get_conn)]

    # ------------------------------------------------------------------ pages

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ------------------------------------------------------------------ api

    @app.get("/api/tools")
    def tools(conn: Conn) -> dict:
        rows = analytics.tools_summary(conn)
        rank = {t: i for i, t in enumerate(TOOL_ORDER)}
        rows.sort(key=lambda r: (rank.get(r.tool, len(rank)), r.tool))
        return {
            "tools": [
                {
                    "tool": r.tool,
                    "label": tool_label(r.tool),
                    "events": r.events,
                    "tokens": r.tokens,
                    "first_seen": r.first_seen,
                    "last_seen": r.last_seen,
                }
                for r in rows
            ]
        }

    @app.get("/api/overview")
    def overview(conn: Conn, scope: ScopeDep, range_name: RangeDep) -> dict:
        pricing = pricing_cache.get()
        d = today()
        tot = analytics.summarize(conn, range_name, d, **scope.kw)
        st = analytics.streak(conn, d, **scope.kw)
        unpriced = analytics.unpriced_models(conn, pricing, **scope.kw)
        saved = analytics.cache_savings(conn, pricing, range_name, d, **scope.kw)
        recs = analytics.records(conn, range_name, d, **scope.kw)
        fees, fees_error = read_fees()
        specs, _sources_error = read_sources()
        roi = (subscription_roi(conn, fees, range_name, d, tool=scope.tool, sources=specs)
               if fees else None)
        drift = pricing_drift(conn, pricing)
        start, end = analytics.range_bounds(range_name, d)
        rules = billing_rules(specs)
        direct = api_spend(conn, rules, start, end, tool=scope.tool) if rules else None
        cmp = analytics.comparison(conn, range_name, d, current=tot, **scope.kw)
        messages = []
        if unpriced:
            messages.append(
                f"{len(unpriced)} model(s) have no price in pricing.json: "
                + ", ".join(u.model for u in unpriced)
                + ". Click Set Price, or add them to pricing.json and run `tracker reprice`."
            )
        if drift:
            messages.append(
                "Stored costs are out of date with pricing.json for: "
                + ", ".join(drift)
                + ". Run `tracker reprice`."
            )
        return {
            **_range_fields(range_name),
            "filter": scope.as_dict(),
            "today": d.isoformat(),
            "start": start.isoformat() if start else None,
            "end": end.isoformat(),
            "totals": tot.as_dict(),
            # vs. the period right before (same tool / model scope); null for lifelong
            "comparison": _comparison_dict(cmp),
            "categories": [
                {"key": key, "label": label, "tokens": tokens, "share": share}
                for key, (label, tokens, share) in zip(CATEGORY_KEYS, tot.categories())
            ],
            "streak": {
                "current": st.current,
                "longest": st.longest,
                "longest_start": st.longest_start,
                "longest_end": st.longest_end,
                "active_days": st.active_days,
            },
            "unpriced_models": [
                {
                    "model": u.model,
                    "events": u.events,
                    "tokens": u.tokens,
                    "price_key": normalize_model(u.model),
                    "base_model": base_model_name(u.model),
                }
                for u in unpriced
            ],
            "pricing_drift": drift,
            "cache_savings": {
                "usd": saved.usd,
                "cache_read_tokens": saved.cache_read_tokens,
                "events": saved.events,
                "unpriced_cache_read_tokens": saved.unpriced_cache_read_tokens,
            },
            "records": _records_dict(recs),
            "subscription": None if roi is None else {
                "monthly_usd": roi.monthly_usd,
                "start": roi.start.isoformat(),
                "end": roi.end.isoformat(),
                "days": roi.days,
                "plan_cost_usd": roi.plan_cost_usd,
                "value_usd": roi.value_usd,
                "multiple": roi.multiple,
                "unpriced_events": roi.unpriced_events,
                "api_direct_spend_usd": roi.api_direct_spend_usd,
                "basis": "tool",  # the model filter does not apply to plans
                "tools": [
                    {"tool": v.tool, "label": tool_label(v.tool), "monthly_usd": v.monthly_usd,
                     "paid_days": v.paid_days, "plan_cost_usd": v.plan_cost_usd,
                     "value_usd": v.value_usd, "unpriced_events": v.unpriced_events,
                     "api_spend_usd": v.api_spend_usd}
                    for v in roi.tools
                ],
            },
            "subscription_error": fees_error,
            # pay-as-you-go usage from sources marked "billing": "api"; null when none is
            "billing": None if direct is None else {
                "api_sources": sum(1 for s in specs if s.billing == "api"),
                "api_direct_spend_usd": direct.usd,
                "api_events": direct.events,
                "api_unpriced_events": direct.unpriced_events,
                "api_tokens": direct.tokens,
                "subscription_value_usd": None if roi is None else roi.value_usd,
                "basis": "tool",
                "tools": [
                    {"tool": t.tool, "label": tool_label(t.tool), "api_spend_usd": t.usd,
                     "events": t.events, "unpriced_events": t.unpriced_events, "tokens": t.tokens}
                    for t in direct.tools
                ],
            },
            "alerts": {
                "needs_pricing": bool(unpriced),
                "needs_reprice": bool(drift),
                "messages": messages,
            },
            "generated_at": _now_iso(),
        }

    @app.get("/api/timeseries")
    def timeseries(
        conn: Conn,
        scope: ScopeDep,
        range_name: RangeDep,
        granularity: Annotated[Literal["auto", "day", "week", "month"], Query()] = "auto",
        split_by: Annotated[Literal["none", "tool"], Query()] = "none",
    ) -> dict:
        """Daily `days` (unchanged, backward compatible) plus `points` bucketed at the
        requested / auto granularity. With `split_by=tool`, `series` holds one aligned
        point list per tool (identical bucket boundaries, zero-filled)."""
        d = today()
        tr = analytics.trend(conn, range_name, d, granularity, split_by, **scope.kw)
        body = {
            **_range_fields(range_name),
            "filter": scope.as_dict(),
            "granularity": tr.granularity,
            "granularity_requested": granularity,
            "split_by": split_by,
            "days": [{"date": day, **_series_values(t)} for day, t in tr.days],
            "points": [_point(b) for b in tr.buckets],
        }
        if tr.series is not None:
            rank = {t: i for i, t in enumerate(TOOL_ORDER)}
            ordered = sorted(tr.series, key=lambda s: (rank.get(s[0], len(rank)), s[0]))
            body["series"] = [
                {
                    "tool": tool_id,
                    "label": tool_label(tool_id),
                    "totals": _series_values(sum((b.totals for b in buckets), analytics.Totals())),
                    "points": [_point(b) for b in buckets],
                }
                for tool_id, buckets in ordered
            ]
        return body

    @app.get("/api/breakdown")
    def breakdown(
        conn: Conn,
        scope: ScopeDep,
        range_name: RangeDep,
        by: Annotated[Literal["model", "tool", "project"], Query()] = "model",
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        group_variants: Annotated[bool, Query()] = False,
    ) -> dict:
        d = today()
        total = analytics.summarize(conn, range_name, d, **scope.kw)
        grouped = by == "model" and group_variants
        variants: dict[str, list[str]] = {}
        if grouped:
            merged: dict[str, analytics.Totals] = {}
            for key, t in analytics.breakdown(conn, "model", range_name, d, None, **scope.kw):
                fam = model_family(key)
                merged[fam] = merged.get(fam, analytics.Totals()) + t
                variants.setdefault(fam, []).append(key)
            rows = sorted(merged.items(), key=lambda kv: (-kv[1].total_tokens, kv[0]))[:limit]
        else:
            rows = analytics.breakdown(conn, by, range_name, d, limit, **scope.kw)
        out = []
        for key, t in rows:
            row = {
                "key": key,
                **t.as_dict(),
                "token_share": analytics.safe_ratio(t.total_tokens, total.total_tokens),
                "cost_share": analytics.safe_ratio(t.cost_usd, total.cost_usd),
            }
            if grouped:
                row["variants"] = sorted(variants[key])
                row["variant_count"] = len(variants[key])
                row["price_key"] = normalize_model(key)
                row["base_model"] = None
            elif by == "model":
                row["price_key"] = normalize_model(key)
                row["base_model"] = base_model_name(key)
            elif by == "tool":
                row["label"] = tool_label(key)
            out.append(row)
        return {
            "by": by,
            "group_variants": grouped,
            **_range_fields(range_name),
            "filter": scope.as_dict(),
            "total": total.as_dict(),
            "rows": out,
        }

    @app.get("/api/heatmap")
    def heatmap(
        conn: Conn,
        scope: ScopeDep,
        year: Annotated[Optional[int], Query(ge=1970, le=9999)] = None,
    ) -> dict:
        d = today()
        y = year or d.year
        cells = analytics.heatmap(conn, y, **scope.kw)
        years = sorted(set(analytics.active_years(conn, **scope.kw)) | {d.year})
        return {
            "year": y,
            "years": years,
            "today": d.isoformat(),
            "filter": scope.as_dict(),
            "max_tokens": max((c.tokens for c in cells), default=0),
            "total_tokens": sum(c.tokens for c in cells),
            "active_days": sum(1 for c in cells if c.events),
            "days": [
                {"date": c.day, "events": c.events, "tokens": c.tokens, "cost_usd": c.cost_usd}
                for c in cells
            ],
        }

    @app.get("/api/hourly")
    def hourly(conn: Conn, scope: ScopeDep, range_name: RangeDep) -> dict:
        hours = analytics.hourly_distribution(conn, range_name, today(), **scope.kw)
        return {
            **_range_fields(range_name),
            "filter": scope.as_dict(),
            "hours": [{"hour": h.hour, "events": h.events, "tokens": h.tokens} for h in hours],
        }

    @app.get("/api/export")
    def export(
        conn: Conn,
        scope: ScopeDep,
        range_name: RangeDep,
        fmt: Annotated[Literal["csv", "json"], Query(alias="format")] = "csv",
    ) -> Response:
        d = today()
        rows = list(analytics.export_rows(conn, range_name, d, **scope.kw))
        stem = f"lifelong-ai-tracker_{analytics.range_slug(range_name)}{scope.file_part()}_{d.isoformat()}"
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=analytics.EXPORT_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            return Response(
                buf.getvalue(),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'},
            )
        payload = {
            "meta": {
                **_range_fields(range_name),
                "filter": scope.as_dict(),
                "today": d.isoformat(),
                "generated_at": _now_iso(),
                "count": len(rows),
                "columns": list(analytics.EXPORT_COLUMNS),
                "totals": analytics.summarize(conn, range_name, d, **scope.kw).as_dict(),
                "notes": "timestamp_utc is UTC; local_date uses the server's local timezone. "
                         "cost_usd is the equivalent cost from pricing.json (null = unpriced).",
            },
            "events": rows,
        }
        return JSONResponse(
            payload,
            headers={"Content-Disposition": f'attachment; filename="{stem}.json"'},
        )

    @app.get("/api/card")
    def card(
        conn: Conn,
        scope: ScopeDep,
        range_name: RangeDep,
        download: Annotated[bool, Query()] = False,
    ) -> Response:
        d = today()
        svg = render_card(conn, range_name, d, **scope.kw)
        disposition = "attachment" if download else "inline"
        name = (f"lifelong-ai-tracker_card_{analytics.range_slug(range_name)}"
                f"{scope.file_part()}_{d.isoformat()}.svg")
        return Response(
            svg,
            media_type="image/svg+xml",
            headers={"Content-Disposition": f'{disposition}; filename="{name}"'},
        )

    @app.get("/api/pricing")
    def pricing_list(conn: Conn) -> dict:
        table = pricing_cache.get()
        # price rows the ledger actually uses (a '-high' variant counts for its base row)
        used_keys = set()
        for (model,) in conn.execute("SELECT DISTINCT model FROM events"):
            hit = table.lookup(model)
            if hit is not None:
                used_keys.add(hit[0])
        meta = pricing_cache.meta()
        updated = meta.get("updated")
        if not (isinstance(updated, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", updated)):
            try:  # no usable _meta.updated: fall back to the file's modification day
                updated = date.fromtimestamp(pricing_path.stat().st_mtime).isoformat()
            except OSError:
                updated = None
        entries = table.entries()
        counts = {p: 0 for p in PROVIDERS}
        for provider, _, _ in entries:
            counts[provider] += 1
        currency = meta.get("currency") if isinstance(meta.get("currency"), str) else "USD"
        return {
            "currency": currency,
            "unit": "per_million_tokens",
            "unit_label": f"{currency} per 1M tokens",
            "updated": updated,
            "count": len(entries),
            "used_count": sum(1 for _, key, _ in entries if key in used_keys),
            "providers": [{"provider": p, "count": counts[p]} for p in PROVIDERS],
            "models": [_price_row(provider, key, price, table.segments(key), key in used_keys)
                       for provider, key, price in entries],
        }

    @app.post("/api/pricing")
    def set_price(body: PriceIn) -> dict:
        prices = body.model_dump(exclude={"model"}, exclude_none=True)
        with write_lock:
            try:
                update = set_model_price(pricing_path, body.model, prices, updated=today().isoformat())
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            except OSError as exc:
                raise HTTPException(500, f"Cannot write pricing file: {exc}") from exc
            pricing_cache.put(update.table)
            result, matched, still_unpriced = None, [], []
            db = Path(db_path)
            if db.is_file() and db.stat().st_size > 0:
                with connect(db) as conn:
                    result = reprice(conn, update.table)
                    for (model,) in conn.execute("SELECT DISTINCT model FROM events ORDER BY model"):
                        hit = update.table.lookup(model)
                        if hit is None:
                            still_unpriced.append(model)
                        elif hit[0] == update.key:
                            matched.append(model)
        _, price = update.table.lookup(update.key)
        return {
            "status": "ok",
            "model": update.key,
            "replaced": update.replaced,
            "price": {f: getattr(price, f) for f in CATEGORY_KEYS},
            "matched_models": matched,
            "unpriced_models": still_unpriced,
            "reprice": None if result is None else {
                "events_priced": result.events_priced,
                "events_unpriced": result.events_unpriced,
                "models_priced": result.models_priced,
                "models_unpriced": result.models_unpriced,
                "cost_before": result.cost_before,
                "cost_after": result.cost_after,
            },
        }

    # ------------------------------------------------------------------ subscriptions

    def subscriptions_body(plans: dict, error: Optional[str]) -> dict:
        d = today()
        return {
            "currency": "USD",
            "period": "month",
            "days_per_month": DAYS_PER_MONTH,
            "max_monthly_usd": MAX_MONTHLY_USD,
            "max_periods": MAX_PERIODS,
            "today": d.isoformat(),
            "writable": subscriptions_path is not None,
            "configured": subscriptions_path is not None and Path(subscriptions_path).is_file(),
            "error": error,
            "tools": [
                {
                    "tool": t,
                    "label": tool_label(t),
                    "monthly_usd": fee_on(plans.get(t, []), d),  # in force today
                    "legacy": any(p.legacy for p in plans.get(t, [])),
                    "periods": [
                        {
                            "start": None if p.start is None else p.start.isoformat(),
                            "end": None if p.end is None else p.end.isoformat(),
                            "monthly_usd": p.monthly_usd,
                            "ongoing": p.end is None,
                        }
                        for p in plans.get(t, [])
                    ],
                }
                for t in TOOL_ORDER
            ],
        }

    @app.get("/api/subscriptions")
    def subscriptions_get() -> dict:
        plans, error = read_fees()
        return subscriptions_body(plans, error)

    @app.post("/api/subscriptions")
    def subscriptions_post(body: SubscriptionsIn) -> dict:
        if subscriptions_path is None:
            raise HTTPException(503, "Subscriptions are not enabled for this server.")
        with write_lock:
            try:
                stored = save_subscriptions(subscriptions_path, body.raw())
            except SubscriptionsError as exc:
                raise HTTPException(422, str(exc)) from exc
            except OSError as exc:
                raise HTTPException(500, f"Cannot write subscriptions file: {exc.strerror or exc}") from exc
        return {"status": "ok", **subscriptions_body(stored, None)}

    # ------------------------------------------------------------------ sync now

    @app.post("/api/sync")
    def sync_now() -> dict:
        if sources_path is None or not Path(sources_path).is_file():
            return {
                "status": "not_configured",
                "message": "No sources.json found. Copy sources.example.json to sources.json, "
                           "set each tool's path, then press Sync Now again.",
                "sources": [], "by_tool": [], "totals": {"inserted": 0, "updated": 0},
            }
        if not sync_lock.acquire(blocking=False):
            raise HTTPException(409, "A sync is already running - try again in a moment.")
        try:
            try:
                specs = sources_mod.load_sources(sources_path)
            except sources_mod.SourcesConfigError as exc:
                raise HTTPException(422, f"sources.json is invalid: {exc}") from exc
            pricing = pricing_cache.get()
            started = time.monotonic()
            with connect(db_path) as conn:
                results = sources_mod.sync_all(conn, specs, pricing)
            elapsed = int((time.monotonic() - started) * 1000)
        finally:
            sync_lock.release()

        rows, by_tool = [], {}
        for res in results:
            rep = res.report
            ins = rep.upsert.inserted if rep else 0
            upd = rep.upsert.updated if rep else 0
            rows.append({
                "name": res.spec.name, "tool": res.spec.tool, "label": tool_label(res.spec.tool),
                "status": res.status, "error": res.error,
                "inserted": ins, "updated": upd,
                "unchanged": rep.upsert.unchanged if rep else 0,
                "events_parsed": rep.events_parsed if rep else 0,
                "files_scanned": rep.files_scanned if rep else 0,
            })
            if res.status == "ok":
                agg = by_tool.setdefault(res.spec.tool, {"tool": res.spec.tool, "label": tool_label(res.spec.tool),
                                                         "inserted": 0, "updated": 0})
                agg["inserted"] += ins
                agg["updated"] += upd
        enabled = [r for r in rows if r["status"] != "disabled"]
        failed = [r for r in enabled if r["status"] == "failed"]
        status = "ok" if not failed else ("failed" if len(failed) == len(enabled) else "partial")
        ordered = [by_tool[t] for t in TOOL_ORDER if t in by_tool] + [
            v for k, v in by_tool.items() if k not in TOOL_ORDER]
        return {
            "status": status,
            "duration_ms": elapsed,
            "sources": rows,
            "by_tool": ordered,
            "totals": {"inserted": sum(r["inserted"] for r in rows),
                       "updated": sum(r["updated"] for r in rows),
                       "failed": len(failed), "ok": len(enabled) - len(failed)},
        }

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__, "db_exists": Path(db_path).is_file()}

    return app
