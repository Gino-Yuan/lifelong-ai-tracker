import json

import pytest

from lifelong_tracker.config import DEFAULT_PRICING_PATH
from lifelong_tracker.pricing import ModelPrice, PricingTable, normalize_model


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("claude-sonnet-4-5", "claude-sonnet-4-5"),
        ("  Claude-Sonnet-4-5 ", "claude-sonnet-4-5"),
        ("anthropic/claude-opus-4-5", "claude-opus-4-5"),
        ("openrouter/anthropic/claude-opus-4-5", "claude-opus-4-5"),
        ("us.anthropic.claude-opus-4-5-20251101-v1:0", "claude-opus-4-5-20251101-v1:0"),
        ("anthropic.claude-haiku-4-5", "claude-haiku-4-5"),
        ("claude-opus-4-5@20251101", "claude-opus-4-5"),
        ("", ""),
    ],
)
def test_normalize_model(raw, expected):
    assert normalize_model(raw) == expected


def test_exact_and_longest_prefix_match(pricing):
    assert pricing.lookup("claude-opus-4-5")[0] == "claude-opus-4-5"
    # date suffix -> longest prefix wins over the shorter 'claude-opus-4'
    assert pricing.lookup("claude-opus-4-5-20251101")[0] == "claude-opus-4-5"
    assert pricing.lookup("claude-opus-4-20250514")[0] == "claude-opus-4"
    assert pricing.lookup("anthropic/claude-sonnet-4-5-20250929")[0] == "claude-sonnet-4-5"
    assert pricing.lookup("us.anthropic.claude-opus-4-5-20251101-v1:0")[0] == "claude-opus-4-5"
    assert pricing.lookup("claude-sonnet-4-5[1m]")[0] == "claude-sonnet-4-5"


def test_unknown_minor_version_is_not_borrowed(pricing):
    # 'claude-opus-4-9' is a different model, not 'claude-opus-4' with a suffix
    assert pricing.lookup("claude-opus-4-9") is None
    assert pricing.lookup("claude-opus-4-9-20270101") is None
    # no partial-token prefix matches either
    assert pricing.lookup("claude-opus-45") is None
    assert pricing.lookup("fictional-model-x1") is None
    assert pricing.lookup("") is None


def test_thinking_defaults_to_output_price(pricing):
    _, sonnet = pricing.lookup("claude-sonnet-4-5")
    assert sonnet.thinking == sonnet.output == 15.0


def test_cost_math():
    p = ModelPrice(input=3, output=15, thinking=15, cache_read=0.3, cache_write=3.75)
    cost = p.cost(input_tokens=1_000_000, output_tokens=1_000_000, thinking_tokens=0,
                  cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert cost == pytest.approx(3 + 15 + 0.3 + 3.75)
    assert p.cost() == 0


def test_cost_for_unpriced_is_none(pricing):
    assert pricing.cost_for("fictional-model-x1", input_tokens=5) is None


@pytest.mark.parametrize(
    "spec, message",
    [
        ({"models": {"m": {"input": 1, "output": 1, "cache_read": 1}}}, "cache_write"),
        ({"models": {"m": {"input": -1, "output": 1, "cache_read": 1, "cache_write": 1}}}, ">= 0"),
        ({"models": []}, "must be an object"),
    ],
)
def test_invalid_pricing_rejected(tmp_path, spec, message):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        PricingTable.load(path)


def test_shipped_pricing_json_is_valid():
    table = PricingTable.load(DEFAULT_PRICING_PATH)
    assert len(table) > 0
    assert table.lookup("claude-opus-5-5") is not None
