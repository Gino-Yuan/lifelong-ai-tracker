"""Loopback-only guard and response hardening for the local dashboard.

Kept free of FastAPI imports so the CLI can validate --host cheaply.
"""

from __future__ import annotations

import ipaddress

LOOPBACK_HOSTNAMES = ("localhost",)

# Every asset is same-origin; the page may not contact any other host.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def is_loopback_host(host: str) -> bool:
    """True only for 127.0.0.0/8, ::1 and the literal name 'localhost'."""
    text = (host or "").strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if text in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def host_header_name(host_header: str) -> str:
    """Hostname part of a Host header ('127.0.0.1:8765' -> '127.0.0.1', '[::1]:80' -> '::1')."""
    text = (host_header or "").strip()
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end != -1 else text
    if text.count(":") == 1:
        return text.split(":", 1)[0]
    return text
