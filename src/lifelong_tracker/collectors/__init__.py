"""Collector registry."""

from __future__ import annotations

from lifelong_tracker.collectors.antigravity import AntigravityCollector
from lifelong_tracker.collectors.base import BaseCollector, FileScan, JsonlCollector
from lifelong_tracker.collectors.claude_code import ClaudeCodeCollector
from lifelong_tracker.collectors.codex import CodexCollector
from lifelong_tracker.collectors.opencode import OpenCodeCollector
from lifelong_tracker.collectors.placeholders import T3Collector

COLLECTORS: dict[str, type[BaseCollector]] = {
    cls.tool: cls
    for cls in (ClaudeCodeCollector, AntigravityCollector, OpenCodeCollector, CodexCollector, T3Collector)
}


TOOL_ORDER: tuple[str, ...] = tuple(COLLECTORS)


def tool_label(tool: str) -> str:
    """Human-readable name of a tool id ('claude-code' -> 'Claude Code')."""
    cls = COLLECTORS.get(tool)
    return cls.display_name if cls else tool


def get_collector(tool: str) -> BaseCollector:
    try:
        return COLLECTORS[tool]()
    except KeyError:
        known = ", ".join(sorted(COLLECTORS))
        raise ValueError(f"unknown tool {tool!r}; known: {known}") from None


__all__ = [
    "AntigravityCollector",
    "COLLECTORS",
    "BaseCollector",
    "ClaudeCodeCollector",
    "CodexCollector",
    "FileScan",
    "JsonlCollector",
    "OpenCodeCollector",
    "T3Collector",
    "TOOL_ORDER",
    "get_collector",
    "tool_label",
]
