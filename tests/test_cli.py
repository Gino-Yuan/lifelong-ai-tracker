"""CLI smoke tests via Typer's CliRunner (in-process, fixture data only)."""

import pytest
from rich.console import Console
from typer.testing import CliRunner

from conftest import TEST_PRICING
from lifelong_tracker import cli
from lifelong_tracker.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_plain_console(monkeypatch):
    # deterministic, wide, colourless output so assertions don't depend on the terminal
    monkeypatch.setattr(cli, "console", Console(width=200, no_color=True, highlight=False))


def _invoke(*args):
    return runner.invoke(app, list(args), catch_exceptions=False)


def test_sync_requires_explicit_source(db_path):
    result = runner.invoke(app, ["sync", "--db", str(db_path)])
    assert result.exit_code != 0
    assert "--source" in result.output
    assert not db_path.exists()


def test_sync_rejects_missing_dir(db_path, tmp_path):
    result = runner.invoke(app, ["sync", "--source", str(tmp_path / "nope"), "--db", str(db_path)])
    assert result.exit_code != 0


def test_placeholder_tool_refuses(db_path, tmp_path):
    result = _invoke("sync", "--source", str(tmp_path), "--tool", "t3", "--db", str(db_path))
    assert result.exit_code == 3
    assert "placeholder" in result.output
    assert not db_path.exists()


def test_sync_stats_reprice_flow(db_path, claude_source):
    r = _invoke("sync", "--source", str(claude_source), "--db", str(db_path), "--pricing", str(TEST_PRICING))
    assert r.exit_code == 0, r.output
    # inserted / updated / unchanged: the resumed-session replay of A002 in the beta file is a no-op
    assert "5 / 0 / 1" in r.output

    r = _invoke("sync", "--source", str(claude_source), "--db", str(db_path), "--pricing", str(TEST_PRICING))
    assert "0 / 2" in r.output  # scanned / unchanged

    r = _invoke("stats", "--db", str(db_path), "--pricing", str(TEST_PRICING))
    assert r.exit_code == 0, r.output
    for label in ("Lifelong", "Cache hit %", "Token categories", "By model", "By tool",
                  "By project", "24h activity", "Longest streak", "Unpriced models", "fictional-model-x1"):
        assert label in r.output, label
    assert "9,327" in r.output  # lifelong total tokens

    r = _invoke("stats", "--range", "7d", "--db", str(db_path), "--pricing", str(TEST_PRICING))
    assert r.exit_code == 0 and "Last 7 days" in r.output

    r = _invoke("reprice", "--db", str(db_path), "--pricing", str(TEST_PRICING))
    assert r.exit_code == 0 and "unpriced: fictional-model-x1" in r.output


def test_stats_bad_range_and_missing_db(db_path):
    assert runner.invoke(app, ["stats", "--range", "decade", "--db", str(db_path)]).exit_code == 2
    assert runner.invoke(app, ["stats", "--db", str(db_path)]).exit_code == 1
    assert not db_path.exists()


def test_collectors_listing():
    r = _invoke("collectors")
    assert "claude-code" in r.output and "placeholder" in r.output
