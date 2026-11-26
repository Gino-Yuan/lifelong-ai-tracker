"""Render the nightly `sync-all` job for the platform's own scheduler.

* macos   - a launchd LaunchAgent plist;
* linux   - a `systemd --user` service + timer pair, plus an equivalent crontab line;
* windows - PowerShell scripts that register / unregister a Task Scheduler task.

This module only *renders text* (the CLI writes it inside the workspace) and
*formats* install commands for the user to run by hand. It never runs launchctl,
systemctl, crontab, schtasks or PowerShell, and never writes outside the folder
it is given.
"""

from __future__ import annotations

import plistlib
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LABEL = "com.lifelong-tracker.sync"
PLATFORMS = ("macos", "linux", "windows")
CRON_MARKER = "# lifelong-tracker:"


def detect_platform(sys_platform: str | None = None) -> str:
    """'macos' / 'windows' / 'linux' for a `sys.platform` value (other Unixes -> 'linux')."""
    p = (sys_platform if sys_platform is not None else sys.platform).lower()
    if p == "darwin":
        return "macos"
    if p.startswith(("win32", "cygwin", "msys")):
        return "windows"
    return "linux"


def default_tracker_bin(project_root: Path, platform: str) -> Path:
    """The project's virtualenv entry point on `platform`."""
    from lifelong_tracker.config import venv_tracker_bin

    return venv_tracker_bin(project_root, windows=platform == "windows")


def default_out(deploy_dir: Path, label: str, platform: str) -> Path:
    """The primary file `schedule render` writes when --out is not given."""
    suffix = {"macos": ".plist", "linux": ".service", "windows": ".ps1"}[platform]
    return deploy_dir / f"{label}{suffix}"


@dataclass(frozen=True)
class ScheduleSpec:
    label: str
    tracker_bin: Path
    project_root: Path
    config: Path
    db: Path
    pricing: Path
    log_file: Path
    hour: int = 23
    minute: int = 30
    backup_dir: Path | None = None
    keep: int = 14

    def program_arguments(self) -> list[str]:
        args = [
            str(self.tracker_bin), "sync-all",
            "--config", str(self.config),
            "--db", str(self.db),
            "--pricing", str(self.pricing),
            "--quiet",
        ]
        if self.backup_dir is not None:
            args += ["--backup-dir", str(self.backup_dir), "--keep", str(self.keep)]
        return args


_PATH_FIELDS = ("tracker_bin", "project_root", "config", "db", "pricing", "log_file")


def _validate(spec: ScheduleSpec, scheduler: str) -> None:
    if not (0 <= spec.hour <= 23 and 0 <= spec.minute <= 59):
        raise ValueError("hour must be 0-23 and minute 0-59")
    for name in _PATH_FIELDS:
        if not getattr(spec, name).is_absolute():
            raise ValueError(f"{name} must be an absolute path ({scheduler} has no working-dir context)")
    for value in [*spec.program_arguments(), str(spec.project_root), str(spec.log_file)]:
        if any(c in value for c in "\r\n\0"):
            raise ValueError("paths must not contain line breaks or NUL characters")


def build_plist(spec: ScheduleSpec) -> dict:
    _validate(spec, "launchd")
    return {
        "Label": spec.label,
        "ProgramArguments": spec.program_arguments(),
        "WorkingDirectory": str(spec.project_root),
        "StartCalendarInterval": {"Hour": spec.hour, "Minute": spec.minute},
        "StandardOutPath": str(spec.log_file),
        "StandardErrorPath": str(spec.log_file),
        "RunAtLoad": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
    }


def render_plist(spec: ScheduleSpec) -> bytes:
    return plistlib.dumps(build_plist(spec), fmt=plistlib.FMT_XML, sort_keys=True)


def install_commands(plist_path: Path, label: str, log_file: Path) -> dict[str, list[str]]:
    """Shell commands for the user to run by hand (absolute, quoted paths)."""
    agent = f"~/Library/LaunchAgents/{plist_path.name}"
    q = shlex.quote
    return {
        "Validate": [f"plutil -lint {q(str(plist_path))}"],
        "Install": [
            "mkdir -p ~/Library/LaunchAgents",
            f"cp {q(str(plist_path))} {agent}",
            f"launchctl bootstrap gui/$(id -u) {agent}",
        ],
        "Run once now (test)": [
            f"launchctl kickstart -k gui/$(id -u)/{label}",
            f"tail -n 40 {q(str(log_file))}",
            f"launchctl print gui/$(id -u)/{label} | grep -E 'state|last exit code'",
        ],
        "Uninstall": [
            f"launchctl bootout gui/$(id -u)/{label}",
            f"rm {agent}",
        ],
    }


def is_privacy_protected(path: Path, home: Path) -> bool:
    """True if `path` sits in a macOS TCC-protected user folder (Documents/Desktop/Downloads)."""
    try:
        rel = path.resolve().relative_to(home.resolve())
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] in {"Documents", "Desktop", "Downloads"}


# --------------------------------------------------------------------------- linux

def _systemd_arg(value: str) -> str:
    """One ExecStart= argument: double-quoted, with systemd's `%` / `$` / `\\` escapes."""
    esc = (value.replace("\\", "\\\\").replace('"', '\\"')
           .replace("%", "%%").replace("$", "$$"))
    return f'"{esc}"'


def _systemd_path(path: Path) -> str:
    return str(path).replace("%", "%%")  # specifiers are expanded in path settings too


def render_systemd_service(spec: ScheduleSpec) -> str:
    _validate(spec, "systemd")
    log = _systemd_path(spec.log_file)
    return "\n".join([
        "# Lifelong AI Tracker - nightly sync-all (systemd --user service).",
        "# Generated by `tracker schedule render --platform linux`. Review before installing.",
        "[Unit]",
        f"Description=Lifelong AI Tracker sync-all ({spec.label})",
        "",
        "[Service]",
        "Type=oneshot",
        f"WorkingDirectory={_systemd_path(spec.project_root)}",
        "ExecStart=" + " ".join(_systemd_arg(a) for a in spec.program_arguments()),
        f"StandardOutput=append:{log}",
        f"StandardError=append:{log}",
        "Nice=10",
        "IOSchedulingClass=idle",
        "",
    ])


def render_systemd_timer(spec: ScheduleSpec, unit: str) -> str:
    _validate(spec, "systemd")
    hhmm = f"{spec.hour:02d}:{spec.minute:02d}"
    return "\n".join([
        "# Lifelong AI Tracker - runs the matching .service once a day (systemd --user timer).",
        "[Unit]",
        f"Description=Run {unit}.service daily at {hhmm}",
        "",
        "[Timer]",
        f"OnCalendar=*-*-* {hhmm}:00",
        "Persistent=true",  # catch up after the machine was off at that time
        f"Unit={unit}.service",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])


def render_crontab(spec: ScheduleSpec) -> str:
    """A crontab line (alternative to systemd); `%` is escaped as cron requires."""
    _validate(spec, "cron")
    q = shlex.quote
    cmd = (f"cd {q(str(spec.project_root))} && "
           + " ".join(q(a) for a in spec.program_arguments())
           + f" >> {q(str(spec.log_file))} 2>&1").replace("%", "\\%")
    return "\n".join([
        "# Lifelong AI Tracker - nightly sync-all (crontab line; local time).",
        f"{spec.minute} {spec.hour} * * * {cmd} {CRON_MARKER}{spec.label}",
        "",
    ])


# --------------------------------------------------------------------------- windows

_PS_QUOTES = "'‘’‚‛"  # PowerShell treats all of these as single quotes


def ps_quote(value: str) -> str:
    """A PowerShell single-quoted literal (no variable or escape expansion)."""
    return "'" + "".join(c * 2 if c in _PS_QUOTES else c for c in value) + "'"


def _cmd_arg(value: str) -> str:
    if any(c in value for c in '"%'):
        raise ValueError(f"Windows task paths and arguments must not contain '\"' or '%': {value!r}")
    return f'"{value}"'


def cmd_arguments(spec: ScheduleSpec) -> str:
    """cmd.exe arguments that run sync-all and append its output to the log file."""
    inner = " ".join(_cmd_arg(a) for a in spec.program_arguments())
    return f'/d /s /c "{inner} >> {_cmd_arg(str(spec.log_file))} 2>&1"'


def _ps1(lines: list[str]) -> bytes:
    # UTF-8 with BOM + CRLF: Windows PowerShell 5.1 reads BOM-less files as ANSI
    return ("\r\n".join(lines) + "\r\n").encode("utf-8-sig")


def render_windows_install(spec: ScheduleSpec) -> bytes:
    _validate(spec, "Task Scheduler")
    hhmm = f"{spec.hour:02d}:{spec.minute:02d}"
    return _ps1([
        "# Lifelong AI Tracker - register the nightly sync-all scheduled task (current user).",
        "# Generated by `tracker schedule render --platform windows`. Review before running.",
        "$ErrorActionPreference = 'Stop'",
        f"$TaskName = {ps_quote(spec.label)}",
        f"$LogFile = {ps_quote(str(spec.log_file))}",
        f"$WorkDir = {ps_quote(str(spec.project_root))}",
        f"$Arguments = {ps_quote(cmd_arguments(spec))}",
        "New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null",
        "$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $Arguments -WorkingDirectory $WorkDir",
        f"$trigger = New-ScheduledTaskTrigger -Daily -At {ps_quote(hhmm)}",
        "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)",
        "$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
        "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited",
        "Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings "
        "-Principal $principal -Description 'Lifelong AI Tracker nightly sync-all' -Force | Out-Null",
        f"Write-Host \"Registered scheduled task '$TaskName' (daily at {hhmm}). Log: $LogFile\"",
    ])


def render_windows_uninstall(spec: ScheduleSpec) -> bytes:
    return _ps1([
        "# Lifelong AI Tracker - remove the nightly sync-all scheduled task.",
        "$ErrorActionPreference = 'Stop'",
        f"$TaskName = {ps_quote(spec.label)}",
        "if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {",
        "    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false",
        "    Write-Host \"Removed scheduled task '$TaskName'.\"",
        "} else {",
        "    Write-Host \"No scheduled task named '$TaskName' - nothing to remove.\"",
        "}",
    ])


# --------------------------------------------------------------------------- all platforms

def _with_suffix(path: Path, suffix: str) -> Path:
    return path if path.suffix == suffix else path.with_name(path.name + suffix)


def render(spec: ScheduleSpec, platform: str, out: Path) -> list[tuple[Path, bytes]]:
    """(path, content) of every file for `platform`; `out` names the primary one."""
    if platform == "macos":
        return [(out, render_plist(spec))]
    if platform == "linux":
        service = _with_suffix(out, ".service")
        unit = service.name[: -len(".service")]
        return [
            (service, render_systemd_service(spec).encode("utf-8")),
            (service.with_name(f"{unit}.timer"), render_systemd_timer(spec, unit).encode("utf-8")),
            (service.with_name(f"{unit}.crontab"), render_crontab(spec).encode("utf-8")),
        ]
    if platform == "windows":
        install = _with_suffix(out, ".ps1")
        return [
            (install, render_windows_install(spec)),
            (install.with_name(f"{install.stem}.uninstall.ps1"), render_windows_uninstall(spec)),
        ]
    raise ValueError(f"unknown platform {platform!r}; choose one of {', '.join(PLATFORMS)}")


def commands_for(platform: str, files: list[Path], spec: ScheduleSpec) -> dict[str, list[str]]:
    """Commands for the user to run by hand on `platform` (nothing here runs them)."""
    q = shlex.quote
    if platform == "macos":
        return install_commands(files[0], spec.label, spec.log_file)
    if platform == "linux":
        service, timer, cron = files
        unit = service.name[: -len(".service")]
        user_dir = "~/.config/systemd/user"
        marker = q(f"{CRON_MARKER}{spec.label}")
        return {
            "Validate": [f"systemd-analyze --user verify {q(str(service))}"],
            "Install (systemd --user)": [
                f"mkdir -p {user_dir}",
                f"cp {q(str(service))} {q(str(timer))} {user_dir}/",
                "systemctl --user daemon-reload",
                f"systemctl --user enable --now {q(unit + '.timer')}",
            ],
            "Run once now (test)": [
                f"systemctl --user start {q(unit + '.service')}",
                f"tail -n 40 {q(str(spec.log_file))}",
                f"systemctl --user list-timers {q(unit + '.timer')}",
            ],
            "Uninstall (systemd --user)": [
                f"systemctl --user disable --now {q(unit + '.timer')}",
                f"rm {user_dir}/{q(unit + '.service')} {user_dir}/{q(unit + '.timer')}",
                "systemctl --user daemon-reload",
            ],
            "Or use cron instead of systemd": [
                f"(crontab -l 2>/dev/null | grep -vF {marker}; cat {q(str(cron))}) | crontab -",
            ],
            "Uninstall (cron)": [f"crontab -l | grep -vF {marker} | crontab -"],
        }
    if platform == "windows":
        install, uninstall = files
        run = "powershell -NoProfile -ExecutionPolicy Bypass -File"
        return {
            "Install (PowerShell)": [f'{run} "{install}"'],
            "Run once now (test)": [
                f'powershell -NoProfile -Command "Start-ScheduledTask -TaskName {ps_quote(spec.label)}"',
                f'powershell -NoProfile -Command "Get-Content -Tail 40 {ps_quote(str(spec.log_file))}"',
            ],
            "Uninstall (PowerShell)": [f'{run} "{uninstall}"'],
        }
    raise ValueError(f"unknown platform {platform!r}; choose one of {', '.join(PLATFORMS)}")
