"""Open-source hardening, step 1: Windows inodes, non-editable installs, UTF-8 and
Windows-safe atomic writes, and a shipped pricing.json without personal estimates.

Everything runs on fabricated data in pytest's workspace-local tmp dir; the
"installed package" case copies src/lifelong_tracker into a tmp folder and runs it
from another tmp folder, so no real ledger, sources or subscriptions file is read.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from _factory import assistant_line, write
from conftest import TEST_PRICING
from lifelong_tracker import backup as backup_mod
from lifelong_tracker import cli, config, fsutil
from lifelong_tracker.collectors import ClaudeCodeCollector, OpenCodeCollector
from lifelong_tracker.collectors import base as base_mod
from lifelong_tracker.collectors import sqlite_source
from lifelong_tracker.pricing import PricingTable, set_model_price
from lifelong_tracker.storage import connect
from lifelong_tracker.sync import sync_source
from lifelong_tracker.web.app import create_app

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "lifelong_tracker"
NTFS_HIGH_BIT = 0xFFFF_0000_0000_0000  # NTFS file IDs can set the top bits of a 64-bit value
runner = CliRunner()


# ============================================================== 1. inode masking

def test_stable_inode_masks_to_signed_63_bits():
    assert fsutil.stable_inode(SimpleNamespace(st_ino=2**64 - 1)) == 2**63 - 1
    assert fsutil.stable_inode(SimpleNamespace(st_ino=2**63)) == 0
    assert fsutil.stable_inode(SimpleNamespace(st_ino=0x8000_0000_0000_1234)) == 0x1234
    for small in (0, 1, 123456789, 2**63 - 1):  # every POSIX-sized inode is unchanged
        assert fsutil.stable_inode(SimpleNamespace(st_ino=small)) == small


def test_masked_inode_fits_sqlite_integer():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (inode INTEGER NOT NULL)")
    with pytest.raises(OverflowError):  # the raw NTFS value would not even bind
        conn.execute("INSERT INTO t VALUES (?)", (2**64 - 1,))
    conn.execute("INSERT INTO t VALUES (?)", (fsutil.stable_inode(SimpleNamespace(st_ino=2**64 - 1)),))
    assert conn.execute("SELECT inode FROM t").fetchone()[0] == 2**63 - 1


@pytest.mark.parametrize("old, new, replaced", [
    (5, 5, False), (5, 6, True), (0, 6, False), (5, 0, False), (0, 0, False),
])
def test_inode_replaced_needs_two_known_inodes(old, new, replaced):
    assert fsutil.inode_replaced(old, new) is replaced


def _huge_inodes(monkeypatch, module):
    """Make every stat() look like NTFS: the top bits of st_ino set."""
    real = fsutil.stable_inode
    monkeypatch.setattr(module, "stable_inode",
                        lambda st: real(SimpleNamespace(st_ino=st.st_ino | NTFS_HIGH_BIT)))


def _claude_sync(db, src):
    with connect(db) as conn:
        return sync_source(conn, ClaudeCodeCollector(), src, PricingTable.load(TEST_PRICING))


def _checkpoint_inodes(db):
    with connect(db) as conn:
        return [r[0] for r in conn.execute("SELECT inode FROM sync_checkpoints")]


def test_jsonl_checkpoint_stores_huge_inode(tmp_path, monkeypatch):
    _huge_inodes(monkeypatch, base_mod)
    src, db = tmp_path / "logs", tmp_path / "t.db"
    f = write(src / "p" / "s.jsonl", assistant_line("m1"), assistant_line("m2"))
    first = _claude_sync(db, src)
    assert first.upsert.inserted == 2
    (inode,) = _checkpoint_inodes(db)
    assert 0 < inode <= 2**63 - 1 and inode == (os.stat(f).st_ino | NTFS_HIGH_BIT) & fsutil.INODE_MASK
    again = _claude_sync(db, src)
    assert again.files_unchanged == 1 and again.files_reset == 0  # stable across syncs
    write(f, assistant_line("m3"), mode="a")
    more = _claude_sync(db, src)
    assert more.upsert.inserted == 1 and more.files_reset == 0  # incremental, not "replaced"


def test_jsonl_inode_zero_relies_on_size_mtime_and_head(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "stable_inode", lambda st: 0)
    src, db = tmp_path / "logs", tmp_path / "t.db"
    f = write(src / "p" / "s.jsonl", assistant_line("m1"), assistant_line("m2"))
    assert _claude_sync(db, src).upsert.inserted == 2
    assert _checkpoint_inodes(db) == [0]
    assert _claude_sync(db, src).files_unchanged == 1          # size + mtime unchanged: skip
    write(f, assistant_line("m3"), mode="a")
    grown = _claude_sync(db, src)
    assert grown.files_reset == 0 and grown.lines_read == 1    # appended: read from the offset
    # replaced by a different file: no inode to compare, the head hash catches it
    new = write(tmp_path / "other.jsonl", assistant_line("x1", out=7), assistant_line("x2"),
                assistant_line("x3"), assistant_line("x4"))
    os.replace(new, f)
    swapped = _claude_sync(db, src)
    assert swapped.resets == [(str(f.resolve()), "rewritten")] and swapped.upsert.inserted == 4
    # truncated below the stored offset
    write(f, assistant_line("x1", out=7))
    cut = _claude_sync(db, src)
    assert cut.resets[0][1] == "truncated"


def test_jsonl_unknown_old_inode_is_not_a_replacement(tmp_path, monkeypatch):
    """A checkpoint written with inode 0 and a later real inode: same size/mtime = unchanged."""
    src, db = tmp_path / "logs", tmp_path / "t.db"
    write(src / "p" / "s.jsonl", assistant_line("m1"))
    monkeypatch.setattr(base_mod, "stable_inode", lambda st: 0)
    _claude_sync(db, src)
    monkeypatch.setattr(base_mod, "stable_inode", fsutil.stable_inode)
    assert _claude_sync(db, src).files_unchanged == 1


def _oc_fixture():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_opencode_db", REPO / "tests" / "fixtures" / "opencode" / "build_opencode_db.py")
    fx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fx)
    return fx


def _oc_sync(db, oc_db):
    with connect(db) as conn:
        return sync_source(conn, OpenCodeCollector(), oc_db, PricingTable.load(TEST_PRICING))


def test_sqlite_signature_masks_inode(tmp_path, monkeypatch):
    fx = _oc_fixture()
    oc_db = tmp_path / "oc" / "opencode.db"
    fx.build(oc_db)
    _huge_inodes(monkeypatch, sqlite_source)
    inode, size, _ = sqlite_source.signature(oc_db)
    assert inode == (os.stat(oc_db).st_ino | NTFS_HIGH_BIT) & fsutil.INODE_MASK and size > 0
    db = tmp_path / "t.db"
    assert _oc_sync(db, oc_db).upsert.inserted == len(fx.EXPECTED_EVENTS)
    assert 0 < _checkpoint_inodes(db)[0] <= 2**63 - 1
    assert _oc_sync(db, oc_db).files_unchanged == 1


def test_sqlite_inode_zero_uses_size_mtime_and_mark(tmp_path, monkeypatch):
    fx = _oc_fixture()
    oc_db = tmp_path / "oc" / "opencode.db"
    fx.build(oc_db)
    monkeypatch.setattr(sqlite_source, "stable_inode", lambda st: 0)
    db = tmp_path / "t.db"
    assert _oc_sync(db, oc_db).upsert.inserted == len(fx.EXPECTED_EVENTS)
    assert _oc_sync(db, oc_db).files_unchanged == 1
    fresh = tmp_path / "oc" / "fresh.db"
    fx.build(fresh)
    os.replace(fresh, oc_db)  # a real replacement, but no inode to see it with
    again = _oc_sync(db, oc_db)
    assert ("replaced" not in [r for _, r in again.resets]) and again.upsert.inserted == 0
    with connect(db) as conn:  # nothing lost or duplicated
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == len(fx.EXPECTED_EVENTS)


# ============================================================== 2. installs and paths

def test_source_checkout_paths_are_unchanged():
    assert config.IS_SOURCE_CHECKOUT and config.SOURCE_ROOT == REPO
    assert config.PROJECT_ROOT == REPO
    assert config.DEFAULT_DB_PATH == REPO / "data" / "tracker.db"
    assert config.DEFAULT_PRICING_PATH == REPO / "pricing.json"
    assert config.DEFAULT_SOURCES_PATH == REPO / "sources.json"
    assert config.DEFAULT_SUBSCRIPTIONS_PATH == REPO / "subscriptions.json"
    assert config.DEFAULT_BACKUP_DIR == REPO / "backups"
    assert config.DEFAULT_LOG_PATH == REPO / "logs" / "sync.log"
    assert config.DEFAULT_TRACKER_BIN == config.venv_tracker_bin(REPO, config.IS_WINDOWS)


def _fake_layout(root: Path, *, pyproject: str | None, parent: str = "src") -> Path:
    pkg = root / parent / "lifelong_tracker"
    pkg.mkdir(parents=True)
    if pyproject is not None:
        (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return pkg


def test_find_source_root_layouts(tmp_path):
    ours = _fake_layout(tmp_path / "a", pyproject='[project]\nname = "lifelong-ai-tracker"\n')
    assert config.find_source_root(ours) == tmp_path / "a"
    other = _fake_layout(tmp_path / "b", pyproject='[project]\nname = "something-else"\n')
    assert config.find_source_root(other) is None
    bare = _fake_layout(tmp_path / "c", pyproject=None)
    assert config.find_source_root(bare) is None
    site = _fake_layout(tmp_path / "d" / "lib" / "python3.13", pyproject=None, parent="site-packages")
    assert config.find_source_root(site) is None


def test_resolve_project_root_falls_back_to_cwd(tmp_path):
    site = _fake_layout(tmp_path / "venv", pyproject=None, parent="site-packages")
    work = tmp_path / "work"
    assert config.resolve_project_root(site, cwd=work) == work.absolute()
    ours = _fake_layout(tmp_path / "repo", pyproject='name = "lifelong-ai-tracker"\n')
    assert config.resolve_project_root(ours, cwd=work) == tmp_path / "repo"


def test_tracker_bin_is_os_aware(tmp_path):
    assert config.venv_tracker_bin(tmp_path, windows=True) == tmp_path / ".venv" / "Scripts" / "tracker.exe"
    assert config.venv_tracker_bin(tmp_path, windows=False) == tmp_path / ".venv" / "bin" / "tracker"
    assert config.default_tracker_bin(tmp_path, source_checkout=True, windows=True).parts[-3:] == (
        ".venv", "Scripts", "tracker.exe")
    installed = config.default_tracker_bin(tmp_path, source_checkout=False, windows=False,
                                           python=str(tmp_path / "env" / "bin" / "python3"))
    assert installed == tmp_path / "env" / "bin" / "tracker"
    win = config.default_tracker_bin(tmp_path, source_checkout=False, windows=True,
                                     python=str(tmp_path / "env" / "Scripts" / "python.exe"))
    assert win == tmp_path / "env" / "Scripts" / "tracker.exe"


def test_pricing_read_path_falls_back_only_for_the_default(tmp_path):
    default, packaged = tmp_path / "pricing.json", tmp_path / "pkg" / "default_pricing.json"
    packaged.parent.mkdir()
    packaged.write_text("{}", encoding="utf-8")
    assert config.pricing_read_path(default, default, packaged) == packaged
    assert config.pricing_seed(default, default, packaged) == packaged
    other = tmp_path / "mine.json"
    assert config.pricing_read_path(other, default, packaged) == other       # explicit: no fallback
    assert config.pricing_seed(other, default, packaged) is None
    default.write_text("{}", encoding="utf-8")
    assert config.pricing_read_path(default, default, packaged) == default   # the real file wins
    assert config.pricing_seed(tmp_path / "x" / ".." / "pricing.json", default, packaged) == packaged


def test_default_pricing_is_package_data():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "default_pricing.json" in project["tool"]["setuptools"]["package-data"]["lifelong_tracker"]
    assert config.PACKAGED_PRICING_PATH == PACKAGE / "default_pricing.json"
    assert config.PACKAGED_PRICING_PATH.is_file()
    PricingTable.load(config.PACKAGED_PRICING_PATH)  # valid table


def test_packaged_default_matches_repo_pricing():
    """The bundled table and the repository's pricing.json must ship identical prices."""
    packaged = json.loads(config.PACKAGED_PRICING_PATH.read_text(encoding="utf-8"))
    repo = json.loads((REPO / "pricing.json").read_text(encoding="utf-8"))
    assert packaged == repo, "copy pricing.json to src/lifelong_tracker/default_pricing.json"


def test_explicit_missing_pricing_is_an_error(tmp_path):
    missing = tmp_path / "nope.json"
    r = runner.invoke(cli.app, ["reprice", "--db", str(tmp_path / "t.db"), "--pricing", str(missing)])
    assert r.exit_code == 2 and "Cannot load pricing file" in r.output
    r = runner.invoke(cli.app, ["serve", "--db", str(tmp_path / "t.db"), "--pricing", str(missing)])
    assert r.exit_code == 2 and "Cannot load pricing file" in r.output
    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({"sources": [{"tool": "claude-code", "path": str(tmp_path)}]}), encoding="utf-8")
    out = tmp_path / "deploy" / "job.plist"
    r = runner.invoke(cli.app, ["schedule", "render", "--platform", "macos", "--config", str(cfg),
                                "--pricing", str(missing), "--out", str(out)])
    assert r.exit_code == 2 and not out.exists()


def test_set_model_price_seeds_a_new_file_from_the_bundled_table(tmp_path):
    target = tmp_path / "pricing.json"
    seed_bytes = config.PACKAGED_PRICING_PATH.read_bytes()
    upd = set_model_price(target, "my-local-model", {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0},
                          updated="2026-11-26", seed=config.PACKAGED_PRICING_PATH)
    assert not upd.replaced and target.is_file()
    written = json.loads(target.read_text(encoding="utf-8"))
    bundled = json.loads(seed_bytes)
    assert set(written["models"]) == set(bundled["models"]) | {"my-local-model"}
    assert written["_meta"]["notes"] == bundled["_meta"]["notes"]
    assert config.PACKAGED_PRICING_PATH.read_bytes() == seed_bytes  # the package is never written


def test_web_reads_bundled_table_until_first_edit(tmp_path):
    target = tmp_path / "pricing.json"
    seed_bytes = config.PACKAGED_PRICING_PATH.read_bytes()
    app = create_app(tmp_path / "none.db", target, pricing_seed=config.PACKAGED_PRICING_PATH)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    bundled = PricingTable.load(config.PACKAGED_PRICING_PATH)
    listed = client.get("/api/pricing").json()
    assert listed["count"] == len(bundled.entries()) and listed["updated"] == "2026-11-26"
    assert not target.exists()
    r = client.post("/api/pricing", json={"model": "claude-sonnet-4-5", "input": 2.5, "output": 15.0,
                                          "cache_read": 0.3, "cache_write": 3.75})
    assert r.status_code == 200, r.text
    assert target.is_file() and config.PACKAGED_PRICING_PATH.read_bytes() == seed_bytes
    after = client.get("/api/pricing").json()
    assert after["count"] == listed["count"]  # the whole table was carried over
    row = next(m for m in after["models"] if m["model"] == "claude-sonnet-4-5")
    assert row["input"] == 2.5


def test_web_without_seed_still_errors_on_missing_file(tmp_path):
    client = TestClient(create_app(tmp_path / "none.db", tmp_path / "pricing.json"),
                        base_url="http://127.0.0.1:8765")
    assert client.get("/api/pricing").status_code == 500


_RUNNER = """
import json, sys
sys.path.insert(0, sys.argv[1])
if sys.argv[2] == "config":
    from lifelong_tracker import config as c
    print(json.dumps({k: str(getattr(c, k)) for k in (
        "__file__", "IS_SOURCE_CHECKOUT", "PROJECT_ROOT", "DEFAULT_DB_PATH", "DEFAULT_PRICING_PATH",
        "DEFAULT_SOURCES_PATH", "DEFAULT_TRACKER_BIN", "PACKAGED_PRICING_PATH")} | {"python": sys.executable}))
else:
    from lifelong_tracker.cli import app
    app(args=sys.argv[2:], prog_name="tracker")
"""


@pytest.fixture
def installed(tmp_path):
    """src/lifelong_tracker copied outside any source tree, run from an unrelated folder."""
    site = tmp_path / "site-packages"
    shutil.copytree(PACKAGE, site / "lifelong_tracker", ignore=shutil.ignore_patterns("__pycache__"))
    work = tmp_path / "work dir"
    work.mkdir()

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", "-c", _RUNNER, str(site), *args], cwd=work,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    return SimpleNamespace(site=site, work=work, run=run)


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if "__pycache__" not in p.parts)


def test_installed_package_defaults_to_cwd(installed):
    out = installed.run("config")
    assert out.returncode == 0, out.stderr
    c = json.loads(out.stdout)
    work = installed.work.resolve()
    assert Path(c["__file__"]).parent == (installed.site / "lifelong_tracker").resolve()
    assert c["IS_SOURCE_CHECKOUT"] == "False"
    assert Path(c["PROJECT_ROOT"]).resolve() == work
    assert Path(c["DEFAULT_DB_PATH"]).resolve() == work / "data" / "tracker.db"
    assert Path(c["DEFAULT_PRICING_PATH"]).resolve() == work / "pricing.json"
    assert Path(c["DEFAULT_SOURCES_PATH"]).resolve() == work / "sources.json"
    assert Path(c["DEFAULT_TRACKER_BIN"]) == Path(c["python"]).parent / (
        "tracker.exe" if os.name == "nt" else "tracker")
    assert Path(c["PACKAGED_PRICING_PATH"]).parent == Path(c["__file__"]).parent


def test_installed_package_syncs_with_bundled_prices(installed, tmp_path):
    logs = tmp_path / "logs"
    write(logs / "p" / "s.jsonl", assistant_line("m1", inp=1_000_000), assistant_line("m2"))
    package_files = _tree(installed.site)
    out = installed.run("sync", "--tool", "claude-code", "--source", str(logs))
    assert out.returncode == 0, out.stdout + out.stderr
    db = installed.work / "data" / "tracker.db"
    assert db.is_file()                                      # ledger lands in the working dir
    assert not (installed.work / "pricing.json").exists()    # read-only fallback, nothing copied
    assert _tree(installed.site) == package_files            # nothing written into the package
    with closing_conn(db) as conn:
        costs = [r[0] for r in conn.execute("SELECT cost_usd FROM events ORDER BY message_id")]
    assert costs[0] == pytest.approx(3.0 + 20 * 15.0 / 1e6)  # claude-sonnet-4-5 from the bundled table
    rep = installed.run("reprice")
    assert rep.returncode == 0, rep.stdout + rep.stderr
    bad = installed.run("reprice", "--pricing", "missing.json")
    assert bad.returncode == 2 and "Cannot load pricing file" in bad.stdout


def closing_conn(db: Path):
    from contextlib import closing
    return closing(sqlite3.connect(db))


# ============================================================== 3. UTF-8 + atomic writes

def test_atomic_write_text_is_utf8_with_lf(tmp_path):
    p = tmp_path / "x.json"
    fsutil.atomic_write_text(p, "naïve ÷ 价格\nline2\n")
    assert p.read_bytes() == "naïve ÷ 价格\nline2\n".encode("utf-8")
    assert sorted(x.name for x in tmp_path.iterdir()) == ["x.json"]


def test_atomic_write_keeps_mode_on_posix(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission bits")
    p = tmp_path / "x.json"
    p.write_text("{}", encoding="utf-8")
    os.chmod(p, 0o640)
    fsutil.atomic_write_text(p, "{}\n")
    assert p.stat().st_mode & 0o777 == 0o640


def _flaky(fails: int, exc: BaseException):
    calls = []

    def replace(src, dst):
        calls.append((src, dst))
        if len(calls) <= fails:
            raise exc
        os.replace(src, dst)

    return replace, calls


def test_replace_retries_transient_permission_error_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(fsutil, "IS_WINDOWS", True)
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("new", encoding="utf-8")
    sleeps: list[float] = []
    flaky, calls = _flaky(2, PermissionError(13, "in use"))
    fsutil.replace(a, b, _replace=flaky, _sleep=sleeps.append)
    assert b.read_text(encoding="utf-8") == "new" and len(calls) == 3 and sleeps == [0.02, 0.02]


def test_replace_gives_up_after_three_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(fsutil, "IS_WINDOWS", True)
    sleeps: list[float] = []
    flaky, calls = _flaky(99, PermissionError(13, "locked"))
    with pytest.raises(PermissionError):
        fsutil.replace(tmp_path / "a", tmp_path / "b", _replace=flaky, _sleep=sleeps.append)
    assert len(calls) == 4 and sleeps == [0.02] * 3


def test_replace_does_not_retry_other_errors_or_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(fsutil, "IS_WINDOWS", True)
    flaky, calls = _flaky(99, OSError(28, "disk full"))
    with pytest.raises(OSError):
        fsutil.replace(tmp_path / "a", tmp_path / "b", _replace=flaky, _sleep=lambda s: None)
    assert len(calls) == 1
    monkeypatch.setattr(fsutil, "IS_WINDOWS", False)
    flaky, calls = _flaky(99, PermissionError(13, "denied"))
    with pytest.raises(PermissionError):
        fsutil.replace(tmp_path / "a", tmp_path / "b", _replace=flaky, _sleep=lambda s: None)
    assert len(calls) == 1  # a POSIX permission error is permanent


def _windows_with_flaky_replace(monkeypatch, fails: int):
    monkeypatch.setattr(fsutil, "IS_WINDOWS", True)
    monkeypatch.setattr(fsutil.time, "sleep", lambda s: None)
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) <= fails:
            raise PermissionError(13, "The process cannot access the file")
        real(src, dst)

    monkeypatch.setattr(fsutil.os, "replace", flaky)
    return calls


def test_pricing_and_subscriptions_survive_a_brief_windows_lock(tmp_path, monkeypatch):
    from lifelong_tracker.subscriptions import save_subscriptions

    pricing = tmp_path / "pricing.json"
    shutil.copy(TEST_PRICING, pricing)
    calls = _windows_with_flaky_replace(monkeypatch, fails=2)
    set_model_price(pricing, "my-model", {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0})
    assert "my-model" in json.loads(pricing.read_text(encoding="utf-8"))["models"] and len(calls) == 3
    subs = tmp_path / "subscriptions.json"
    calls.clear()
    save_subscriptions(subs, {"claude-code": 20.0})
    assert json.loads(subs.read_text(encoding="utf-8")) and len(calls) == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pricing.json", "subscriptions.json"]


def test_atomic_write_cleans_up_when_the_lock_persists(tmp_path, monkeypatch):
    p = tmp_path / "pricing.json"
    shutil.copy(TEST_PRICING, p)
    before = p.read_bytes()
    _windows_with_flaky_replace(monkeypatch, fails=99)
    with pytest.raises(PermissionError):
        set_model_price(p, "my-model", {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0})
    assert p.read_bytes() == before and sorted(x.name for x in tmp_path.iterdir()) == ["pricing.json"]


def test_backup_survives_a_brief_windows_lock(tmp_path, monkeypatch):
    db = tmp_path / "ledger" / "t.db"
    db.parent.mkdir()
    with connect(db):
        pass
    calls = _windows_with_flaky_replace(monkeypatch, fails=1)
    res = backup_mod.backup_database(db, tmp_path / "bk", keep=3, now=datetime(2026, 11, 26, 23, 30))
    assert res.path.name == "tracker-20261126-233000.db" and res.integrity == "ok" and len(calls) == 2
    assert [p.name for p in (tmp_path / "bk").iterdir()] == [res.path.name]


def test_safe_chmod_never_breaks_a_write(tmp_path, monkeypatch):
    def refuse(*_a, **_k):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(fsutil.os, "chmod", refuse)  # e.g. a FAT/exFAT or network drive
    p = tmp_path / "x.json"
    fsutil.atomic_write_text(p, "{}\n")
    assert p.read_text(encoding="utf-8") == "{}\n"
    monkeypatch.setattr(fsutil, "IS_WINDOWS", True)
    touched = []
    monkeypatch.setattr(fsutil.os, "chmod", lambda *a: touched.append(a))
    fsutil.safe_chmod(p, 0o444)  # would make the file read-only on Windows
    assert touched == []


def test_source_text_io_declares_utf8():
    """Every text read/write in the package names its encoding (Windows defaults to cp1252)."""
    import ast

    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            name, kws = node.func.attr, {k.arg for k in node.keywords}
            text_mode = not any(isinstance(a, ast.Constant) and isinstance(a.value, str) and "b" in a.value
                                for a in node.args)
            if name in ("read_text", "write_text", "fdopen") and "encoding" not in kws:
                offenders.append(f"{path.name}:{node.lineno} {name}")
            elif name == "open" and text_mode and "encoding" not in kws and node.args:
                offenders.append(f"{path.name}:{node.lineno} open")
    assert offenders == []


# ============================================================== 4. shipped pricing.json

@pytest.mark.parametrize("path", [REPO / "pricing.json", PACKAGE / "default_pricing.json"])
def test_shipped_pricing_has_no_personal_unknown_estimate(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    table = PricingTable.load(path)
    assert "unknown" not in raw["models"] and table.lookup("unknown") is None
    assert raw["models"]["codex-auto-review"] == {
        "input": 2.0, "output": 10.0, "thinking": 10.0, "cache_read": 0.2, "cache_write": 0.0}
    notes = " ".join(raw["_meta"]["notes"])
    assert "codex-auto-review" in notes and "Estimate" in notes and "'unknown'" not in notes


def test_unnamed_model_events_stay_unpriced(tmp_path):
    src, db = tmp_path / "logs", tmp_path / "t.db"
    write(src / "p" / "s.jsonl", assistant_line("m1", model="unknown"))
    with connect(db) as conn:
        sync_source(conn, ClaudeCodeCollector(), src, PricingTable.load(REPO / "pricing.json"))
        rows = conn.execute("SELECT model, cost_usd FROM events").fetchall()
    assert [tuple(r) for r in rows] == [("unknown", None)]
