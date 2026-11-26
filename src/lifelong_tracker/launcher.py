"""`tracker open`: find a browser and show the local dashboard, optionally as an app window.

`--app` looks for an installed Chromium-family browser (Chrome, Edge, Brave, Arc,
Chromium) and starts it with `--app=<url>`, which opens a window without tabs or
an address bar. Only fixed install locations and executables on PATH are checked;
nothing is installed or configured. When none is found - or it fails to start -
the default browser is used via `webbrowser.open()`.

Only loopback URLs are ever opened.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from lifelong_tracker.web.security import is_loopback_host

# (display name, macOS .app bundle, Linux executables, Windows install paths under Program Files /
#  %LOCALAPPDATA%, Windows executables on PATH)
_BROWSERS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    ("Google Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome",
     ("google-chrome", "google-chrome-stable"),
     (r"Google\Chrome\Application\chrome.exe",), ("chrome",)),
    ("Microsoft Edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
     ("microsoft-edge", "microsoft-edge-stable"),
     (r"Microsoft\Edge\Application\msedge.exe",), ("msedge",)),
    ("Brave", "Brave Browser.app/Contents/MacOS/Brave Browser",
     ("brave-browser", "brave"),
     (r"BraveSoftware\Brave-Browser\Application\brave.exe",), ("brave",)),
    ("Arc", "Arc.app/Contents/MacOS/Arc", (), (), ("arc",)),
    ("Chromium", "Chromium.app/Contents/MacOS/Chromium",
     ("chromium", "chromium-browser"), (r"Chromium\Application\chrome.exe",), ("chromium",)),
)


@dataclass(frozen=True)
class Browser:
    name: str
    path: Path


def browser_candidates(platform: str, home: Path) -> list[tuple[str, list[Path], list[str]]]:
    """Per browser: fixed install paths to check, then executable names to look up on PATH."""
    out = []
    for name, mac, linux, win, win_exe in _BROWSERS:
        if platform == "macos":
            out.append((name, [Path("/Applications") / mac, home / "Applications" / mac], []))
        elif platform == "windows":
            roots = [Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)"), home / "AppData" / "Local"]
            out.append((name, [r / rel for rel in win for r in roots], list(win_exe)))
        else:
            out.append((name, [], list(linux)))
    return out


def find_chromium(
    platform: str,
    *,
    home: Optional[Path] = None,
    is_file: Callable[[Path], bool] = Path.is_file,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Optional[Browser]:
    """The first installed Chromium-family browser, in the order Chrome, Edge, Brave, Arc, Chromium."""
    home = Path.home() if home is None else home
    for name, paths, exes in browser_candidates(platform, home):
        for p in paths:
            if is_file(p):
                return Browser(name, p)
        for exe in exes:
            found = which(exe)
            if found:
                return Browser(name, Path(found))
    return None


def dashboard_url(bind: str, port: int) -> str:
    host = f"[{bind}]" if ":" in bind else bind
    return f"http://{host}:{port}"


def _require_loopback(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "http" or not is_loopback_host(parts.hostname or ""):
        raise ValueError(f"refusing to open a non-loopback URL: {url}")


def app_window_command(browser: Browser, url: str) -> list[str]:
    _require_loopback(url)
    return [str(browser.path), f"--app={url}"]


def _popen_detached(cmd: list[str]) -> None:
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if flags:  # Windows
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)  # noqa: S603 - fixed browser path + a loopback URL


def open_dashboard(
    url: str,
    browser: Optional[Browser],
    *,
    launch: Optional[Callable[[list[str]], None]] = None,
    web_open: Optional[Callable[[str], bool]] = None,
) -> str:
    """Open `url` as an app window in `browser`, else in the default browser.

    Returns how it was opened: "app:<name>", "browser" or "failed".
    """
    _require_loopback(url)
    launch = launch or _popen_detached     # looked up per call (tests replace them)
    web_open = web_open or webbrowser.open
    if browser is not None:
        try:
            launch(app_window_command(browser, url))
            return f"app:{browser.name}"
        except OSError:
            pass  # e.g. removed since detection: fall back to the default browser
    try:
        return "browser" if web_open(url) else "failed"
    except webbrowser.Error:
        return "failed"


def port_accepts(host: str, port: int, timeout: float = 0.25) -> bool:
    """True when something accepts TCP connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, *, stop: threading.Event, timeout: float = 15.0,
                  interval: float = 0.1) -> bool:
    """Poll until the server accepts connections, `stop` is set or `timeout` passes."""
    deadline = time.monotonic() + timeout
    while not stop.is_set() and time.monotonic() < deadline:
        if port_accepts(host, port):
            return True
        stop.wait(interval)
    return False


def open_when_ready(
    url: str,
    host: str,
    port: int,
    browser: Optional[Browser],
    *,
    stop: threading.Event,
    on_done: Callable[[str], None] = lambda how: None,
    timeout: float = 15.0,
) -> threading.Thread:
    """Start a daemon thread that opens the dashboard once the server is up."""
    _require_loopback(url)

    def run() -> None:
        if wait_for_port(host, port, stop=stop, timeout=timeout):
            on_done(open_dashboard(url, browser))
        else:
            on_done("not-ready")

    t = threading.Thread(target=run, name="tracker-open-browser", daemon=True)
    t.start()
    return t
