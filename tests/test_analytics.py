"""Five-category totals, ratios with zero denominators, ranges, breakdowns, hours, streaks."""

from datetime import date

import pytest

from lifelong_tracker import analytics
from lifelong_tracker.analytics import Totals, cache_hit_rate, compute_streak, safe_ratio
from lifelong_tracker.pricing import PricingTable
from lifelong_tracker.storage import pricing_drift, reprice
from lifelong_tracker.sync import sync_source

# Expected values from tests/fixtures/claude_code (see test_upsert_dedup for per-row checks)
EXP = dict(events=5, input_tokens=126, output_tokens=551, thinking_tokens=0,
           cache_read_tokens=8000, cache_write_tokens=650)
EXP_COST = (5211 + 10540 + 292.5 + 18) / 1e6  # A001 + A002 + A003 + B002; B001 unpriced
LAST_DAY = date(2026, 3, 5)


@pytest.fixture
def synced(conn, pricing, claude_source, collector):
    sync_source(conn, collector, claude_source, pricing)
    return conn


# ---------------------------------------------------------------- pure metric edges

def test_cache_hit_rate_zero_denominator():
    assert cache_hit_rate(0, 0, 0) == 0.0
    assert cache_hit_rate(0, 50, 0) == 1.0
    assert cache_hit_rate(10, 30, 60) == pytest.approx(0.3)


def test_safe_ratio_zero():
    assert safe_ratio(5, 0) == 0.0 and safe_ratio(0, 0) == 0.0


def test_empty_totals_are_all_zero():
    t = Totals()
    assert t.total_tokens == 0
    assert t.cache_hit_rate == 0.0 and t.output_ratio == 0.0 and t.cost_per_mtok == 0.0
    assert [share for *_, share in t.categories()] == [0.0] * 5


def test_empty_ledger_summary(conn):
    t = analytics.summarize(conn, "all", LAST_DAY)
    assert t.events == 0 and t.total_tokens == 0 and t.cache_hit_rate == 0.0
    assert analytics.breakdown(conn, "model", "all", LAST_DAY) == []
    assert all(h.tokens == 0 for h in analytics.hourly_distribution(conn, "all", LAST_DAY))
    assert analytics.streak(conn, LAST_DAY).longest == 0


# ---------------------------------------------------------------- fixture-backed

def test_lifelong_five_categories(synced):
    t = analytics.summarize(synced, "all", LAST_DAY)
    for k, v in EXP.items():
        assert getattr(t, k) == v, k
    assert t.total_tokens == 126 + 551 + 8000 + 650
    assert [c[0] for c in t.categories()] == ["Input", "Output", "Thinking", "Cache Read", "Cache Write"]
    assert sum(c[2] for c in t.categories()) == pytest.approx(1.0)
    assert t.cache_hit_rate == pytest.approx(8000 / (126 + 8000 + 650))
    assert t.output_ratio == pytest.approx(551 / t.total_tokens)
    assert t.cost_usd == pytest.approx(EXP_COST)
    assert t.unpriced_events == 1
    # blended $/Mtok excludes the unpriced event's 150 tokens
    assert t.cost_per_mtok == pytest.approx(EXP_COST * 1e6 / (t.total_tokens - 150))


@pytest.mark.parametrize(
    "range_name, today, events",
    [
        ("today", date(2026, 3, 5), 2),   # B001, B002
        ("today", date(2026, 3, 4), 0),
        ("7d", date(2026, 3, 5), 5),
        ("7d", date(2026, 3, 8), 4),      # window 03-02..03-08 -> A002, A003, B001, B002
        ("30d", date(2026, 3, 30), 5),
        ("30d", date(2026, 4, 2), 2),     # window 03-04..04-02
        ("year", date(2026, 12, 31), 5),
        ("year", date(2027, 1, 1), 0),
        ("all", date(2030, 1, 1), 5),
    ],
)
def test_ranges(synced, range_name, today, events):
    assert analytics.summarize(synced, range_name, today).events == events


def test_unknown_range_rejected(synced):
    with pytest.raises(ValueError):
        analytics.summarize(synced, "decade", LAST_DAY)


def test_breakdowns(synced):
    by_model = dict(analytics.breakdown(synced, "model", "all", LAST_DAY))
    assert by_model["claude-sonnet-4-5-20250929"].output_tokens == 180
    assert by_model["claude-sonnet-4-5"].events == 1
    assert by_model["fictional-model-x1"].cost_usd == 0 and by_model["fictional-model-x1"].unpriced_events == 1
    by_project = dict(analytics.breakdown(synced, "project", "all", LAST_DAY))
    assert by_project["alpha"].events == 3 and by_project["beta"].events == 2
    by_tool = analytics.breakdown(synced, "tool", "all", LAST_DAY)
    assert [k for k, _ in by_tool] == ["claude-code"]
    first_key, _ = analytics.breakdown(synced, "model", "all", LAST_DAY, limit=1)[0]
    assert first_key == "claude-opus-4-5-20251101"  # largest total
    with pytest.raises(ValueError):
        analytics.breakdown(synced, "events; DROP TABLE events", "all", LAST_DAY)


def test_hourly_distribution(synced):
    hours = analytics.hourly_distribution(synced, "all", LAST_DAY)
    assert len(hours) == 24
    busy = {h.hour: h.events for h in hours if h.events}
    assert busy == {9: 1, 14: 1, 23: 1, 2: 1, 3: 1}
    assert hours[9].tokens == 12 + 180 + 2000 + 500


def test_daily_stats_view_and_series(synced):
    rows = synced.execute("SELECT day, SUM(events) FROM daily_stats GROUP BY day ORDER BY day").fetchall()
    assert [tuple(r) for r in rows] == [("2026-03-01", 1), ("2026-03-02", 1), ("2026-03-03", 1), ("2026-03-05", 2)]
    series = analytics.daily_series(synced, "all", LAST_DAY)
    assert sum(t.total_tokens for _, t in series) == analytics.summarize(synced, "all", LAST_DAY).total_tokens


def test_streak_from_ledger(synced):
    s = analytics.streak(synced, date(2026, 3, 5))
    assert (s.active_days, s.longest, s.longest_start, s.longest_end) == (4, 3, "2026-03-01", "2026-03-03")
    assert s.current == 1
    assert analytics.streak(synced, date(2026, 3, 6)).current == 1  # yesterday still counts
    assert analytics.streak(synced, date(2026, 3, 7)).current == 0


def test_compute_streak_edges():
    d = date.fromisoformat
    assert compute_streak([], d("2026-01-01")).longest == 0
    s = compute_streak([d("2026-01-01")] * 3, d("2026-01-01"))
    assert (s.longest, s.current, s.active_days) == (1, 1, 1)
    days = [d("2025-12-30"), d("2025-12-31"), d("2026-01-01"), d("2026-01-03"), d("2026-01-04")]
    s = compute_streak(days, d("2026-01-04"))
    assert (s.longest, s.longest_start, s.current) == (3, "2025-12-30", 2)


# ---------------------------------------------------------------- pricing integration

def test_reprice_changes_cost_only(synced, tmp_path):
    tokens_before = synced.execute(
        "SELECT SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), SUM(cache_write_tokens) FROM events"
    ).fetchone()
    cheap = tmp_path / "p.json"
    cheap.write_text(
        '{"models": {"claude-sonnet-4-5": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},'
        ' "fictional-model-x1": {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0}}}',
        encoding="utf-8",
    )
    table = PricingTable.load(cheap)
    assert set(pricing_drift(synced, table)) >= {"claude-opus-4-5-20251101", "fictional-model-x1"}
    res = reprice(synced, table)
    assert res.models_unpriced == 2  # opus-4-5 and haiku-4-5 are not in this table
    assert res.cost_after == pytest.approx((100 * 1 + 50 * 2) / 1e6)
    assert pricing_drift(synced, table) == []
    assert synced.execute(
        "SELECT SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), SUM(cache_write_tokens) FROM events"
    ).fetchone() == tokens_before
    assert [u.model for u in analytics.unpriced_models(synced, table)] == [
        "claude-opus-4-5-20251101", "claude-haiku-4-5-20251001"]


def test_no_drift_after_sync(synced, pricing):
    assert pricing_drift(synced, pricing) == []
    assert [u.model for u in analytics.unpriced_models(synced, pricing)] == ["fictional-model-x1"]
