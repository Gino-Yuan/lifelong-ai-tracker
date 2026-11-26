"""Pricing engine: per-model USD per million tokens, loaded from pricing.json.

Lookup order for a raw model name:
  1. normalise (lower-case, drop provider prefixes such as 'anthropic/',
     'us.anthropic.', and Vertex-style '@version' suffixes);
  2. exact key match;
  3. longest key that is a prefix of the name at a segment boundary
     ('claude-sonnet-4-5-20250929' -> 'claude-sonnet-4-5').
     A remainder that starts with a short number ('claude-opus-4' vs
     'claude-opus-4-9') is a *different minor version*, not a date suffix,
     and is deliberately NOT matched, so an unknown new model is reported as
     unpriced instead of silently borrowing an older model's price.
  4. if still unmatched, strip one trailing reasoning-tier / variant suffix
     ('-xhigh', '-high', '-medium', '-low', '-tiered-high', '-tiered-low', ...)
     and retry steps 2-3 on the base name, so 'gemini-4-neon-xhigh',
     '-high' and '-low' all share one 'gemini-4-neon' entry. The minor-version
     guard of step 3 still applies to the base name ('gemini-4-5-high' never
     borrows 'gemini-4').

An explicit entry always wins over the fallbacks: if pricing.json has both
'gemini-4-neon-high' and 'gemini-4-neon', the '-high' variant uses its own row.

Dated prices (optional). A model entry is either one price object (valid for
every date, the original format) or a list of price segments, each with an
`effective_from` ISO date::

    "claude-x": [
      {"effective_from": "2026-01-01", "input": 2.0, "output": 10.0, ...},
      {"effective_from": "2026-12-01", "input": 4.0, "output": 20.0, ...}
    ]

Segments are sorted by date and must not repeat a date; only the first may omit
`effective_from`. An event is priced by the segment in force on its UTC date
(the latest segment whose `effective_from` is on or before that day); the first
segment also covers any day before its own date. Without a date (lookups that
ask "what does this model cost now?") the latest segment applies.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence

PRICE_FIELDS = ("input", "output", "thinking", "cache_read", "cache_write")

# Codex only logs one cumulative `tokens_used` per thread (stored raw in input_tokens).
# Billing all of it at the full input rate wildly overstates cost, because a coding
# agent's context is mostly cache reads. Such rows are priced at a blended rate:
# 90% cache read + 8% input + 2% output. Raw token columns are never changed.
CODEX_TOOL = "codex"
CODEX_THREAD_TOTAL = "thread_total"
CODEX_BLEND = {"cache_read": 0.90, "input": 0.08, "output": 0.02}


def is_codex_thread_total(tool: str, message_id: str, output_tokens: int = 0,
                          thinking_tokens: int = 0, cache_read_tokens: int = 0,
                          cache_write_tokens: int = 0, **_: int) -> bool:
    """True for a Codex per-thread total that carries only input_tokens."""
    return (tool == CODEX_TOOL and message_id == CODEX_THREAD_TOTAL
            and not output_tokens and not thinking_tokens
            and not cache_read_tokens and not cache_write_tokens)
_PROVIDER_PREFIX = re.compile(r"^(?:[a-z]{2,4}\.)?anthropic\.")
_BOUNDARY_CHARS = "-_.:[@ "
TIER_SUFFIX = re.compile(r"-(?:tiered-)?(?:xhigh|high|medium|low)$")
# A pricing.json key as written by `set_model_price` (after normalize_model).
MODEL_KEY = re.compile(r"^[a-z0-9][a-z0-9._:+\[\]-]{0,127}$")
# Display grouping for the dashboard price list, in this order.
PROVIDERS = ("Claude", "Gemini", "GPT", "Other")
_PROVIDER_PATTERNS = (
    ("Claude", re.compile(r"^claude")),
    ("Gemini", re.compile(r"^gemini")),
    ("GPT", re.compile(r"^(?:gpt|chatgpt|codex|o\d)")),
)
_DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def provider_of(model: str) -> str:
    """'Claude' / 'Gemini' / 'GPT' / 'Other', inferred from the normalised name's prefix."""
    norm = normalize_model(model)
    for name, pattern in _PROVIDER_PATTERNS:
        if pattern.match(norm):
            return name
    return "Other"


def _natural_key(name: str) -> tuple:
    """'gpt-6-sol' < 'gpt-6.1-sol' < 'gpt-10' (digit runs compare as numbers)."""
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                 for part in re.split(r"(\d+)", name) if part)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input: float
    output: float
    thinking: float
    cache_read: float
    cache_write: float

    def cost(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        thinking_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        return (
            input_tokens * self.input
            + output_tokens * self.output
            + thinking_tokens * self.thinking
            + cache_read_tokens * self.cache_read
            + cache_write_tokens * self.cache_write
        ) / 1_000_000

    @property
    def codex_blended_rate(self) -> float:
        """USD per 1M tokens for a Codex thread total (see CODEX_BLEND)."""
        return sum(w * getattr(self, f) for f, w in CODEX_BLEND.items())


@dataclass(frozen=True, slots=True)
class PriceSegment:
    """One dated price. `effective_from` is None only for an undated first segment."""

    effective_from: date | None
    price: ModelPrice


@dataclass(frozen=True, slots=True)
class PriceWindow:
    """The UTC days a segment prices: [lo, hi) as ISO strings; None = unbounded."""

    lo: str | None
    hi: str | None
    price: ModelPrice


def _as_day(day: date | str) -> date:
    return day if isinstance(day, date) else date.fromisoformat(str(day)[:10])


def price_on(segments: Sequence[PriceSegment], day: date | str | None) -> ModelPrice:
    """The price in force on `day` (latest segment when `day` is None)."""
    if day is None or len(segments) == 1:
        return segments[-1].price
    d = _as_day(day)
    current = segments[0].price  # the first segment also covers earlier days
    for seg in segments[1:]:
        if seg.effective_from is not None and seg.effective_from <= d:
            current = seg.price
        else:
            break
    return current


def price_windows(segments: Sequence[PriceSegment]) -> list[PriceWindow]:
    """Non-overlapping UTC-day windows covering every date, one per segment."""
    out = []
    for i, seg in enumerate(segments):
        lo = None if i == 0 else segments[i].effective_from.isoformat()
        nxt = segments[i + 1].effective_from if i + 1 < len(segments) else None
        out.append(PriceWindow(lo, None if nxt is None else nxt.isoformat(), seg.price))
    return out


def normalize_model(name: str) -> str:
    text = (name or "").strip().lower()
    if "/" in text:  # 'anthropic/claude-x', 'openrouter/anthropic/claude-x'
        text = text.rsplit("/", 1)[1]
    text = _PROVIDER_PREFIX.sub("", text)  # 'us.anthropic.claude-x', 'anthropic.claude-x'
    if "@" in text:  # Vertex 'claude-opus-4-5@20251101'
        text = text.split("@", 1)[0]
    return text


def strip_tier_suffix(name: str) -> str:
    """'gemini-4-neon-xhigh' -> 'gemini-4-neon'; names without a tier suffix are returned as is."""
    base = TIER_SUFFIX.sub("", name)
    return base if base and base != name else name


_DATE_SUFFIX = re.compile(r"(?:-\d{8}|-\d{4}-\d{2}-\d{2})$")


def model_family(model: str) -> str:
    """Display family of a model id: normalised, minus one date stamp and one tier suffix.

    'claude-sonnet-4-5-20250929' -> 'claude-sonnet-4-5', 'gemini-4-neon-xhigh' ->
    'gemini-4-neon', 'anthropic/claude-opus-4-5@20251101' -> 'claude-opus-4-5'.
    Used only to *group* ledger rows; price lookup keeps its own stricter rules.
    """
    norm = normalize_model(model)
    if not norm:
        return model
    base = _DATE_SUFFIX.sub("", norm) or norm
    base = strip_tier_suffix(base)
    return _DATE_SUFFIX.sub("", base) or base


def base_model_name(model: str) -> str | None:
    """Normalised base name for a tiered model id, or None if it has no tier suffix."""
    norm = normalize_model(model)
    base = strip_tier_suffix(norm)
    return base if base != norm else None


def _is_version_continuation(remainder: str) -> bool:
    """True when `remainder` (text after a matched key) starts a new minor version."""
    m = re.match(r"^-(\d+)(?:$|[^\d])", remainder)
    return bool(m) and len(m.group(1)) <= 2


def _parse_price(where: str, spec: dict) -> ModelPrice:
    values: dict[str, float] = {}
    for fld in PRICE_FIELDS:
        if fld == "thinking" and fld not in spec:
            continue
        if fld not in spec:
            raise ValueError(f"pricing.json: {where} missing {fld!r}")
        raw = spec[fld]
        if isinstance(raw, bool):
            raise ValueError(f"pricing.json: {where}.{fld} must be a number")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"pricing.json: {where}.{fld} must be a number") from None
        if not math.isfinite(val):
            raise ValueError(f"pricing.json: {where}.{fld} must be a finite number")
        if val < 0:
            raise ValueError(f"pricing.json: {where}.{fld} must be >= 0")
        values[fld] = val
    values.setdefault("thinking", values["output"])
    return ModelPrice(**values)


def _parse_effective(where: str, value: Any) -> date:
    if not isinstance(value, str) or not _DATE_ISO.match(value):
        raise ValueError(f"pricing.json: {where}.effective_from must be a date in YYYY-MM-DD form")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"pricing.json: {where}.effective_from {value!r} is not a real calendar date") from None


def _parse_entry(name: str, spec: Any) -> tuple[PriceSegment, ...]:
    """One pricing.json model entry -> its segments (a plain object is one undated segment)."""
    if isinstance(spec, dict):
        eff = spec.get("effective_from")
        return (PriceSegment(None if eff is None else _parse_effective(repr(name), eff),
                             _parse_price(repr(name), spec)),)
    if not isinstance(spec, list):
        raise ValueError(f"pricing.json: entry {name!r} must be an object or a list of dated prices")
    if not spec:
        raise ValueError(f"pricing.json: entry {name!r} has an empty price list")
    segments: list[PriceSegment] = []
    for i, item in enumerate(spec):
        where = f"{name!r}[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"pricing.json: {where} must be an object")
        eff = item.get("effective_from")
        if eff is None and i > 0:
            raise ValueError(f"pricing.json: {where} needs an effective_from date "
                             "(only the first price may leave it out)")
        segments.append(PriceSegment(None if eff is None else _parse_effective(where, eff),
                                     _parse_price(where, item)))
    for prev, nxt in zip(segments, segments[1:]):
        if prev.effective_from is not None and nxt.effective_from <= prev.effective_from:
            raise ValueError(f"pricing.json: {name!r} effective_from dates must be strictly increasing "
                             f"({prev.effective_from} then {nxt.effective_from})")
    return tuple(segments)


class PricingTable:
    def __init__(self, models: dict[str, ModelPrice | Sequence[PriceSegment]]):
        self._segments: dict[str, tuple[PriceSegment, ...]] = {}
        for k, v in models.items():
            segs = (PriceSegment(None, v),) if isinstance(v, ModelPrice) else tuple(v)
            if not segs:
                raise ValueError(f"model {k!r} has no price")
            self._segments[normalize_model(k)] = segs
        # the latest price of every model (what an undated lookup returns)
        self._models = {k: segs[-1].price for k, segs in self._segments.items()}
        # longest keys first so the first boundary-prefix hit is the best one
        self._keys_by_len = sorted(self._models, key=len, reverse=True)
        self._cache: dict[str, tuple[str, ModelPrice] | None] = {}

    @classmethod
    def load(cls, path: Path) -> "PricingTable":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, raw: Any) -> "PricingTable":
        if not isinstance(raw, dict):
            raise ValueError("pricing.json: top level must be an object")
        models_raw = raw.get("models", {})
        if not isinstance(models_raw, dict):
            raise ValueError("pricing.json: 'models' must be an object")
        return cls({name: _parse_entry(name, spec) for name, spec in models_raw.items()})

    def __len__(self) -> int:
        return len(self._models)

    @property
    def has_dated_prices(self) -> bool:
        return any(len(s) > 1 for s in self._segments.values())

    def entries(self) -> list[tuple[str, str, "ModelPrice"]]:
        """(provider, key, latest price) for every entry, grouped by PROVIDERS, then by name."""
        rank = {p: i for i, p in enumerate(PROVIDERS)}
        rows = [(provider_of(k), k, v) for k, v in self._models.items()]
        rows.sort(key=lambda r: (rank[r[0]], _natural_key(r[1])))
        return rows

    def segments(self, key: str) -> tuple[PriceSegment, ...]:
        """Every dated price of a pricing.json key (as returned by `lookup`), oldest first."""
        return self._segments[normalize_model(key)]

    def lookup(self, model: str) -> tuple[str, ModelPrice] | None:
        """(matched key, latest price) for a ledger model name, or None when unpriced."""
        if model not in self._cache:
            self._cache[model] = self._lookup(model)
        return self._cache[model]

    def lookup_at(self, model: str, day: date | str | None) -> tuple[str, ModelPrice] | None:
        """Like `lookup`, but the price in force on `day` (an event's UTC date)."""
        hit = self.lookup(model)
        if hit is None or day is None:
            return hit
        return hit[0], price_on(self._segments[hit[0]], day)

    def windows(self, model: str) -> list[PriceWindow] | None:
        """The dated price windows that apply to a ledger model name (None when unpriced)."""
        hit = self.lookup(model)
        return None if hit is None else price_windows(self._segments[hit[0]])

    def _match(self, norm: str) -> tuple[str, ModelPrice] | None:
        if norm in self._models:
            return norm, self._models[norm]
        for key in self._keys_by_len:
            if norm.startswith(key) and len(norm) > len(key):
                remainder = norm[len(key):]
                if remainder[0] in _BOUNDARY_CHARS and not _is_version_continuation(remainder):
                    return key, self._models[key]
        return None

    def _lookup(self, model: str) -> tuple[str, ModelPrice] | None:
        norm = normalize_model(model)
        if not norm:
            return None
        hit = self._match(norm)
        if hit is None:
            base = strip_tier_suffix(norm)
            if base != norm:
                hit = self._match(base)
        return hit

    def cost_for(self, model: str, *, tool: str = "", message_id: str = "",
                 day: date | str | None = None, **tokens: int) -> float | None:
        """USD cost of one event; `day` (its UTC date) picks the dated price in force."""
        hit = self.lookup_at(model, day)
        if hit is None:
            return None
        price = hit[1]
        if is_codex_thread_total(tool, message_id, **tokens):
            return tokens.get("input_tokens", 0) * price.codex_blended_rate / 1_000_000
        return price.cost(**tokens)


# --------------------------------------------------------------------------- writing

def _format_pricing(raw: dict[str, Any]) -> str:
    """Stable, diff-friendly layout: one line per model, other sections indented."""
    lines = ["{"]
    items = list(raw.items())
    for i, (key, value) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        if key == "models" and isinstance(value, dict):
            lines.append('  "models": {')
            models = list(value.items())
            width = max((len(json.dumps(k)) for k, _ in models), default=0) + 1
            for j, (name, spec) in enumerate(models):
                head = (json.dumps(name) + ":").ljust(width)
                tail = "," if j < len(models) - 1 else ""
                lines.append(f"    {head} {json.dumps(spec)}{tail}")
            lines.append("  }" + comma)
        else:
            body = json.dumps(value, indent=2, ensure_ascii=False).replace("\n", "\n  ")
            lines.append(f"  {json.dumps(key)}: {body}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file in the same directory + fsync + os.replace (never half-written)."""
    path = Path(path)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class PriceUpdate:
    key: str
    replaced: bool
    table: PricingTable


def set_model_price(
    path: Path, model: str, prices: dict[str, float], *, updated: str | None = None
) -> PriceUpdate:
    """Add or replace one model's price in pricing.json, atomically.

    `model` is normalised exactly like lookups normalise ledger models; the
    resulting key must match MODEL_KEY. Every other entry and section of the file
    (e.g. `_meta`) is preserved, except that `updated` (an ISO date), when given,
    is stored as `_meta.updated`. The new file is validated as a whole before it
    replaces the old one, so a bad value can never leave pricing.json broken.
    """
    if updated is not None and not _DATE_ISO.match(updated):
        raise ValueError(f"updated must be an ISO date, got {updated!r}")
    key = normalize_model(model)
    if not MODEL_KEY.match(key):
        raise ValueError(f"invalid model name {model!r}")
    values: dict[str, float] = {}
    for fld in PRICE_FIELDS:
        if fld not in prices or prices[fld] is None:
            if fld == "thinking":
                continue
            raise ValueError(f"missing price {fld!r}")
        val = float(prices[fld])
        if not math.isfinite(val) or val < 0:
            raise ValueError(f"price {fld!r} must be a finite number >= 0")
        values[fld] = val
    unknown = set(prices) - set(PRICE_FIELDS)
    if unknown:
        raise ValueError(f"unknown price field(s): {', '.join(sorted(unknown))}")

    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(raw, dict):
        raise ValueError("pricing.json: top level must be an object")
    models = raw.setdefault("models", {})
    if not isinstance(models, dict):
        raise ValueError("pricing.json: 'models' must be an object")
    existing = next((k for k in models if normalize_model(k) == key), None)
    if existing is not None and isinstance(models[existing], list) and models[existing]:
        # dated prices: edit the latest segment in place, keep its date and the history
        latest = models[existing][-1]
        eff = latest.get("effective_from") if isinstance(latest, dict) else None
        models[existing] = [*models[existing][:-1],
                            values if eff is None else {"effective_from": eff, **values}]
    elif existing is not None:
        old = models[existing]
        eff = old.get("effective_from") if isinstance(old, dict) else None
        # keep the original spelling / position (and a single segment's date)
        models[existing] = values if eff is None else {"effective_from": eff, **values}
    else:
        models[key] = values
    if updated is not None:
        meta = raw.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
            raw = {"_meta": meta, **raw}  # keep _meta first in the file
        meta["updated"] = updated
    text = _format_pricing(raw)
    table = PricingTable.from_dict(json.loads(text))  # validate the whole file first
    _atomic_write_text(path, text)
    return PriceUpdate(key=key, replaced=existing is not None, table=table)
