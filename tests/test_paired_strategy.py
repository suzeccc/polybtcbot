"""Pure decision checks for the isolated paired strategy."""

import pytest

from btcbot.engine import _summarize_market_result
from btcbot.models import TradeRecord
from btcbot.paired_strategy import (
    calibration_bins, calibrated_probability, paired_shares, should_exit_weak_side,
    settlement_pnl, strong_direction, walk_book,
)


def test_calibration_requires_historical_support_and_tilts_token_count():
    bins = calibration_bins([(0.70, i < 12) for i in range(20)])
    assert calibrated_probability(0.70, bins) == 13 / 22
    assert calibrated_probability(0.60, bins) is None
    assert paired_shares(5, 0.59, 0.41) == (5.5, 5)
    assert paired_shares(5, 0.59, 0.41, tilt_pct=25) == (6.25, 5)
    assert paired_shares(5, None, 0.8) == (5, 5)
    assert paired_shares(5, 0.51, 0.49) == (5, 5)


def test_book_walk_requires_depth_and_prices_fees_and_slippage():
    plan = walk_book(
        [(0.50, 3), (0.40, 3)], 5, selling=True,
        fee_per_share=lambda _price: 0.01, slippage=0.02,
    )
    assert plan[0] == pytest.approx(0.46)
    assert plan[1] == pytest.approx(2.15)
    assert walk_book(
        [(0.50, 3)], 5, selling=True, fee_per_share=lambda _price: 0.01
    ) is None


def test_strong_direction_needs_persistent_aligned_signals_and_expanding_odds():
    samples = [
        {"momentum_5": 0.1, "momentum_15": 0.1, "momentum_30": 0.1,
         "start_delta": 5, "poly_gap": 0.05 + i * 0.015, "sample_ts": float(i)}
        for i in range(8)
    ]
    assert strong_direction(samples, min_move=1.5) == "UP"
    samples[-1]["momentum_5"] = -0.1
    assert strong_direction(samples, min_move=1.5) is None

    samples = [
        {"momentum_5": 0.1, "momentum_15": 0.1, "momentum_30": 0.1,
         "start_delta": 5, "poly_gap": gap, "sample_ts": float(i)}
        for i, gap in enumerate((0.10, 0.14, 0.11, 0.15, 0.12, 0.16, 0.13, 0.17))
    ]
    assert strong_direction(samples, min_move=1.5) is None


def test_exit_compares_net_recovery_to_calibrated_settlement_value():
    assert should_exit_weak_side(5, 2.0, 0.3)
    assert not should_exit_weak_side(5, 2.0, 0.5)
    assert not should_exit_weak_side(5, 2.0, None)
    assert settlement_pnl(5.5, 5, 6) == (-0.5, -1)


def test_exit_trade_reduces_settlement_shares_and_adds_net_sale_proceeds():
    trades = [
        TradeRecord("m", "ENTRY", "UP", "u", "BUY", 5, 0.5, 10, 0.8, 0.1),
        TradeRecord("m", "ENTRY", "DOWN", "d", "BUY", 5, 0.5, 10, 0.8, 0.1),
        TradeRecord("m", "EXIT", "DOWN", "d", "SELL", 3, 0.3, 10, 0.8, 0.1, fee_usd=0.1),
    ]
    _, _, payout, pnl, _ = _summarize_market_result(trades, "DOWN")
    assert payout == 0
    assert pnl == -7.1
    assert _summarize_market_result(trades, "DOWN")[-1] == 0


def test_pair_market_result_classifies_profit_not_the_first_leg_direction():
    trades = [
        TradeRecord("m", "ENTRY", "UP", "u", "BUY", 4, 0.4, 10, 0.8, 0.1),
        TradeRecord("m", "ENTRY", "DOWN", "d", "BUY", 4, 0.4, 10, 0.8, 0.1),
    ]
    assert _summarize_market_result(trades, "UP")[-1] == 1
    assert _summarize_market_result(trades, "DOWN")[-1] == 1
