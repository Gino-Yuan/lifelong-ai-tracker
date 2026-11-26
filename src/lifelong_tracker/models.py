"""Standard data structures shared by every layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def format_utc(ts: datetime) -> str:
    """Normalise to the canonical stored form 'YYYY-MM-DDTHH:MM:SS.fffZ'."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (with 'Z' or offset) into an aware UTC datetime."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One model response's usage metadata. Never carries conversation text."""

    timestamp: datetime
    tool: str
    model: str
    project_name: str = ""
    session_id: str = ""
    request_id: str = ""
    message_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not (self.request_id or self.message_id):
            raise ValueError("UsageEvent needs request_id or message_id for dedup")
        for name in TOKEN_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")

    @property
    def dedup_key(self) -> tuple[str, str, str]:
        return (self.tool, self.request_id, self.message_id)

    @property
    def total_tokens(self) -> int:
        return sum(getattr(self, name) for name in TOKEN_FIELDS)

    @property
    def timestamp_utc(self) -> str:
        return format_utc(self.timestamp)


@dataclass(frozen=True, slots=True)
class FileState:
    """Incremental-scan state of one source file (persisted in sync_checkpoints)."""

    inode: int
    size: int
    mtime_ns: int
    offset: int  # byte offset just past the last complete line consumed
    head_len: int = 0  # leading bytes fingerprinted to detect in-place rewrites
    head_hash: str = ""
    lines_read: int = 0  # cumulative complete lines consumed
    resets: int = 0  # times truncation / rewrite / replacement was detected
