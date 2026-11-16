"""Engine integration checks for the paired entry and partial exit paths."""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import btcbot.engine as engine_module
from btcbot.engine import Engine, MarketRuntime
from btcbot.feeds.polymarket_ws import OrderBook
from btcbot.models import Market, Signal, TradeRecord
from btcbot.paired_strategy import calibration_bins


def _book(bid=0.49, ask=0.50, size=100):
    return OrderBook(
        bids={bid: size}, asks={ask: size}, received_at=time.monotonic(), initialized=True
    )


def _signal(probability_up=0.70):
    return Signal(
        "UP", 0.8, 0.2, 1.0, 0.5, probability_up, "test",
        expected_entry_price=0.5, estimated_entry_cost_per_share=0.51,
        probability_up=probability_up,
    )


def _patch_storage(monkeypatch):
    @asynccontextmanager
    async def connect():
        yield object()

    monkeypatch.setattr(engine_module, "connect", connect)
    monkeypatch.setattr(engine_module, "insert_trade", AsyncMock())
    monkeypatch.setattr(engine_module, "save_open_position", AsyncMock())


def _pair_engine(monkeypatch, *, cap=10):
    now = int(time.time())
    market = Market("pair", "condition", "up", "down", now - 120, now + 180)
    monkeypatch.setattr(engine_module, "CONFIG", replace(
        engine_module.CONFIG,
        strategy="paired_tilt_exit", paired_base_shares=5,
        paired_max_cost_usd=cap, paired_max_price=0.65,
        limit_slippage=0.02, paired_tilt_gap=0.03,
    ))
    engine = Engine(paper_mode=True)
    engine._current_market = market
    engine._market_states[market.slug] = MarketRuntime(market, status="ACTIVE")
    engine._market_inputs_fresh = lambda _market: True
    engine._risk.can_trade = lambda *args, **kwargs: True
    engine._polymarket._books = {"up": _book(), "down": _book(0.48, 0.49)}
    engine._probability_bins = calibration_bins(
        [(0.70, True)] * 20 + [(0.30, False)] * 20
    )
    return engine, market


@pytest.mark.asyncio
async def test_pair_entry_tilts_shares_and_tracks_both_fills(monkeypatch):
    engine, market = _pair_engine(monkeypatch)
    _patch_storage(monkeypatch)
    submitted = []

    async def place_trade(_market, signal, _amount, *, shares):
        submitted.append((signal.direction, shares))
        return TradeRecord(
            market.slug, "ENTRY", signal.direction, market.token_id_for(signal.direction),
            "BUY", shares * 0.5, 0.5, shares, signal.strength, signal.edge,
            signal_fair_prob=signal.fair_prob, fee_usd=0.01,
        )

    engine._executor = SimpleNamespace(place_trade=place_trade)
    await engine._enter_pair(market, _signal())

    assert submitted == [("UP", 5.5), ("DOWN", 5)]
    assert {position.direction for position in engine._paired_positions_by_slug[market.slug]} == {"UP", "DOWN"}


@pytest.mark.asyncio
async def test_concurrent_pair_entry_signals_do_not_duplicate_orders(monkeypatch):
    engine, market = _pair_engine(monkeypatch)
    _patch_storage(monkeypatch)
    submitted = []

    async def place_trade(_market, signal, _amount, *, shares):
        submitted.append(signal.direction)
        await asyncio.sleep(0)
        return TradeRecord(
            market.slug, "ENTRY", signal.direction, market.token_id_for(signal.direction),
            "BUY", shares * 0.5, 0.5, shares, signal.strength, signal.edge,
        )

    engine._executor = SimpleNamespace(place_trade=place_trade)
    await asyncio.gather(
        engine._enter_pair(market, _signal()),
        engine._enter_pair(market, _signal()),
    )

    assert submitted == ["UP", "DOWN"]


@pytest.mark.asyncio
async def test_pair_entry_rejects_combined_cost_over_cap(monkeypatch):
    engine, market = _pair_engine(monkeypatch, cap=1)
    executor = SimpleNamespace(place_trade=AsyncMock())
    engine._executor = executor

    await engine._enter_pair(market, _signal())

    executor.place_trade.assert_not_awaited()
    assert market.slug not in engine._paired_positions_by_slug


@pytest.mark.asyncio
async def test_partial_pair_exit_only_retries_remaining_shares(monkeypatch):
    engine, market = _pair_engine(monkeypatch)
    market.end_ts = int(time.time()) + 50
    engine._chainlink.latest_ts = time.time()
    engine._chainlink.latest_price = 100010
    engine._binance.latest_ts = time.time()
    engine._signal_gen._chainlink_start_price = 100000
    now = time.monotonic()
    engine._paired_samples.extend(
        {"momentum_5": 0.1, "momentum_15": 0.1, "momentum_30": 0.1,
         "start_delta": 10, "poly_gap": 0.10 + i * 0.015, "sample_ts": now - 7 + i}
        for i in range(8)
    )
    engine._paired_sample_ts = now + 1
    positions = [
        engine_module.OpenPosition(market, "UP", "up", 0.5, 5, strategy_id="paired_tilt_exit"),
        engine_module.OpenPosition(market, "DOWN", "down", 0.5, 5, strategy_id="paired_tilt_exit"),
    ]
    engine._paired_positions_by_slug[market.slug] = positions
    engine._positions_by_slug[market.slug] = positions[0]
    engine._risk.open_positions[:] = positions
    engine._probability_bins = calibration_bins(
        [(0.70, True)] * 20 + [(0.30, False)] * 20
    )
    _patch_storage(monkeypatch)
    requested = []
    limits = []

    async def place_exit(_market, position, shares, limit, _signal):
        requested.append(shares)
        limits.append(limit)
        sold = min(2, shares)
        return TradeRecord(
            market.slug, "EXIT", position.direction, position.token_id, "SELL",
            sold * 0.30, 0.30, sold, 0.8, 0.1, fee_usd=0.01,
        )

    engine._executor = SimpleNamespace(place_exit=place_exit)
    await engine._maybe_exit_pair(market, _signal())
    await engine._maybe_exit_pair(market, _signal())

    assert requested == [5, 3]
    assert limits == [0.46, 0.46]
    assert positions[1].token_quantity == 1


@pytest.mark.asyncio
async def test_pair_exit_holds_when_binance_data_is_stale(monkeypatch):
    engine, market = _pair_engine(monkeypatch)
    market.end_ts = int(time.time()) + 50
    engine._chainlink.latest_ts = time.time()
    engine._chainlink.latest_price = 100010
    engine._signal_gen._chainlink_start_price = 100000
    positions = [
        engine_module.OpenPosition(market, "UP", "up", 0.5, 5, strategy_id="paired_tilt_exit"),
        engine_module.OpenPosition(market, "DOWN", "down", 0.5, 5, strategy_id="paired_tilt_exit"),
    ]
    engine._paired_positions_by_slug[market.slug] = positions
    executor = SimpleNamespace(place_exit=AsyncMock())
    engine._executor = executor

    await engine._maybe_exit_pair(market, _signal())

    executor.place_exit.assert_not_awaited()
    assert [position.token_quantity for position in positions] == [5, 5]
