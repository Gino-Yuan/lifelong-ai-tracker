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
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


class PricingTable:
    def __init__(self, models: dict[str, ModelPrice]):
        self._models = {normalize_model(k): v for k, v in models.items()}
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
        models: dict[str, ModelPrice] = {}
        for name, spec in models_raw.items():
            if not isinstance(spec, dict):
                raise ValueError(f"pricing.json: entry {name!r} must be an object")
            values: dict[str, float] = {}
            for fld in PRICE_FIELDS:
                if fld == "thinking" and fld not in spec:
                    continue
                if fld not in spec:
                    raise ValueError(f"pricing.json: {name!r} missing {fld!r}")
                val = float(spec[fld])
                if not math.isfinite(val):
                    raise ValueError(f"pricing.json: {name!r}.{fld} must be a finite number")
                if val < 0:
                    raise ValueError(f"pricing.json: {name!r}.{fld} must be >= 0")
                values[fld] = val
            values.setdefault("thinking", values["output"])
            models[name] = ModelPrice(**values)
        return cls(models)

    def __len__(self) -> int:
        return len(self._models)

    def entries(self) -> list[tuple[str, str, "ModelPrice"]]:
        """(provider, key, price) for every entry, grouped by PROVIDERS, then by name."""
        rank = {p: i for i, p in enumerate(PROVIDERS)}
        rows = [(provider_of(k), k, v) for k, v in self._models.items()]
        rows.sort(key=lambda r: (rank[r[0]], _natural_key(r[1])))
        return rows

    def lookup(self, model: str) -> tuple[str, ModelPrice] | None:
        if model not in self._cache:
            self._cache[model] = self._lookup(model)
        return self._cache[model]

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
                 **tokens: int) -> float | None:
        hit = self.lookup(model)
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
    if existing is not None:
        models[existing] = values  # keep the original spelling / position
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
