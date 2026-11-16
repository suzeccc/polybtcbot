"""Market activation, repeated discovery, and old-market settlement isolation."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

import btcbot.engine as engine_module
from btcbot.engine import Engine, MarketRuntime
from btcbot.market_discovery import DiscoveryResult
from btcbot.models import Market, OpenPosition
from btcbot.storage import db, repo


def _market(slug: str, start: int) -> Market:
    return Market(slug, f"condition-{slug}", f"up-{slug}", f"down-{slug}", start, start + 300)


def _discovery_mock(engine: Engine, result: DiscoveryResult, scans: int = 1) -> AsyncMock:
    count = 0

    async def discover(*_args):
        nonlocal count
        count += 1
        if count == scans:
            engine._stop.set()
            engine._discovery_wakeup.set()
        return result

    return AsyncMock(side_effect=discover)


@pytest.mark.asyncio
async def test_repeated_discovery_updates_one_market_and_subscribes_once(monkeypatch):
    now = time.time()
    market = _market(f"btc-updown-5m-{int(now // 300) * 300 + 300}", int(now // 300) * 300 + 300)
    result = DiscoveryResult([market, market], {market.slug}, set(), now)
    monkeypatch.setattr(engine_module, "CONFIG", replace(
        engine_module.CONFIG, discovery_interval_sec=0.01, discovery_max_backoff_sec=0.02
    ))
    monkeypatch.setattr(engine_module, "upsert_market", AsyncMock())

    @asynccontextmanager
    async def fake_connect():
        yield object()

    monkeypatch.setattr(engine_module, "connect", fake_connect)
    engine = Engine()
    engine._polymarket.set_token_ids = AsyncMock()
    monkeypatch.setattr(engine_module, "discover_markets", _discovery_mock(engine, result, scans=2))
    await asyncio.wait_for(engine._discovery_loop(), timeout=1)

    assert list(engine._market_states) == [market.slug]
    assert engine._market_states[market.slug].status == "SUBSCRIBING"
    assert engine._polymarket.set_token_ids.await_count == 2
    assert engine_module.upsert_market.await_count == 1


@pytest.mark.asyncio
async def test_changed_ready_market_identity_is_subscribed_again(monkeypatch):
    now = time.time()
    start = int(now // 300) * 300 + 300
    original = _market(f"btc-updown-5m-{start}", start)
    changed = replace(
        original,
        condition_id="replacement-condition",
        up_token_id="replacement-up",
        down_token_id="replacement-down",
    )
    monkeypatch.setattr(engine_module, "CONFIG", replace(
        engine_module.CONFIG, discovery_interval_sec=0.01, discovery_max_backoff_sec=0.02
    ))
    result = DiscoveryResult([changed], {changed.slug}, set(), now)
    monkeypatch.setattr(engine_module, "upsert_market", AsyncMock())

    @asynccontextmanager
    async def fake_connect():
        yield object()

    monkeypatch.setattr(engine_module, "connect", fake_connect)
    engine = Engine()
    runtime = MarketRuntime(original, status="READY", discovered_at=now - 30, ready_at=now - 1)
    engine._market_states[original.slug] = runtime
    engine._polymarket.set_token_ids = AsyncMock()

    monkeypatch.setattr(engine_module, "discover_markets", _discovery_mock(engine, result))
    await asyncio.wait_for(engine._discovery_loop(), timeout=1)

    assert runtime.status == "SUBSCRIBING"
    assert runtime.ready_at is None
    assert engine._polymarket.set_token_ids.await_args.args[0] == {
        "replacement-up", "replacement-down"
    }


@pytest.mark.asyncio
async def test_active_market_identity_change_is_frozen(monkeypatch):
    now = time.time()
    start = int(now // 300) * 300
    original = _market(f"btc-updown-5m-{start}", start)
    changed = replace(original, up_token_id="replacement-up")
    monkeypatch.setattr(engine_module, "CONFIG", replace(
        engine_module.CONFIG, discovery_interval_sec=0.01, discovery_max_backoff_sec=0.02
    ))
    result = DiscoveryResult([changed], {changed.slug}, set(), now)
    upsert = AsyncMock()
    monkeypatch.setattr(engine_module, "upsert_market", upsert)

    @asynccontextmanager
    async def fake_connect():
        yield object()

    monkeypatch.setattr(engine_module, "connect", fake_connect)
    engine = Engine()
    runtime = MarketRuntime(original, status="ACTIVE", discovered_at=now - 60)
    engine._market_states[original.slug] = runtime
    engine._current_market = original
    engine._polymarket.set_token_ids = AsyncMock()

    monkeypatch.setattr(engine_module, "discover_markets", _discovery_mock(engine, result))
    await asyncio.wait_for(engine._discovery_loop(), timeout=1)

    assert runtime.market.up_token_id == original.up_token_id
    assert not runtime.market.active
    assert not engine._current_market.accepting_orders
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_market_activates_without_restarting_shared_feeds(monkeypatch):
    now = int(time.time())
    market = _market(f"btc-updown-5m-{now - 1}", now - 1)
    engine = Engine()
    runtime = MarketRuntime(market, status="READY", discovered_at=now - 20, ready_at=now - 2)
    engine._market_states[market.slug] = runtime
    engine._clock_is_sane = lambda: True
    engine._books_ready = lambda _market: True
    engine._market_inputs_fresh = lambda _market: True

    await engine._manage_markets()

    assert engine._current_market is market
    assert runtime.status == "ACTIVE"
    assert engine._binance.latest_ts == 0
    assert engine._chainlink.latest_ts == 0


@pytest.mark.asyncio
async def test_only_one_market_becomes_active_at_a_time():
    now = int(time.time())
    start = now - 1
    first = _market("first-overlap", start)
    second = _market("second-overlap", start)
    engine = Engine()
    engine._market_states[first.slug] = MarketRuntime(first, status="READY")
    engine._market_states[second.slug] = MarketRuntime(second, status="READY")
    engine._clock_is_sane = lambda: True
    engine._books_ready = lambda _market: True
    engine._market_inputs_fresh = lambda _market: True

    await engine._manage_markets()

    active = [runtime for runtime in engine._market_states.values() if runtime.status == "ACTIVE"]
    assert len(active) == 1
    assert engine._current_market is active[0].market


@pytest.mark.asyncio
async def test_full_books_make_market_ready_and_switch_at_exact_start(monkeypatch):
    now = int(time.time())
    start = now + 300
    market = _market(f"btc-updown-5m-{start}", start)
    engine = Engine()
    runtime = MarketRuntime(market, status="SUBSCRIBING", discovered_at=now - 10)
    engine._market_states[market.slug] = runtime
    engine._clock_is_sane = lambda: True
    engine._market_inputs_fresh = lambda _market: True
    engine._binance.run = AsyncMock()
    engine._binance.stop = AsyncMock()
    engine._chainlink.run = AsyncMock()
    engine._chainlink.stop = AsyncMock()

    await engine._manage_markets()
    assert runtime.status == "SUBSCRIBING"

    for token in (market.up_token_id, market.down_token_id):
        await engine._polymarket._handle_message(json.dumps({
            "event_type": "book",
            "asset_id": token,
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.50", "size": "10"}],
        }))
    await engine._manage_markets()
    assert runtime.status == "READY"

    monkeypatch.setattr(engine_module.time, "time", lambda: start)
    await engine._manage_markets()

    assert engine._current_market is market
    assert runtime.status == "ACTIVE"
    engine._binance.run.assert_not_awaited()
    engine._binance.stop.assert_not_awaited()
    engine._chainlink.run.assert_not_awaited()
    engine._chainlink.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_unready_next_market_leaves_no_active_market_at_boundary():
    now = int(time.time())
    old = Market("old", "old-condition", "old-up", "old-down", now - 301, now - 1)
    upcoming = _market(f"btc-updown-5m-{now - 1}", now - 1)
    engine = Engine()
    engine._current_market = old
    engine._market_states[old.slug] = MarketRuntime(old, status="ACTIVE")
    engine._market_states[upcoming.slug] = MarketRuntime(upcoming, status="SUBSCRIBING")
    engine._clock_is_sane = lambda: True

    await engine._manage_markets()

    assert engine._current_market is None
    assert engine._market_states[upcoming.slug].status == "SUBSCRIBING"
    assert engine._market_states[old.slug].status == "EXPIRED"


@pytest.mark.asyncio
async def test_market_expiry_waits_for_inflight_order_lock():
    now = int(time.time())
    market = _market("closing-market", now - 1)
    engine = Engine()
    runtime = MarketRuntime(market, status="ACTIVE")
    engine._market_states[market.slug] = runtime
    engine._current_market = market
    await engine._order_lock.acquire()
    task = asyncio.create_task(engine._expire_market(runtime))
    await asyncio.sleep(0)

    assert not task.done()
    engine._order_lock.release()
    await task

    assert runtime.status == "EXPIRED"
    assert engine._current_market is None


@pytest.mark.asyncio
async def test_gamma_failure_preserves_active_market(monkeypatch):
    now = time.time()
    start = int(now // 300) * 300
    market = _market(f"btc-updown-5m-{start}", start)
    result = DiscoveryResult([], {market.slug}, {market.slug}, None)
    monkeypatch.setattr(engine_module, "CONFIG", replace(
        engine_module.CONFIG, discovery_interval_sec=0.01, discovery_max_backoff_sec=0.02
    ))
    engine = Engine()
    runtime = MarketRuntime(market, status="ACTIVE", discovered_at=now - 60)
    engine._market_states[market.slug] = runtime
    engine._current_market = market
    engine._polymarket.set_token_ids = AsyncMock()

    monkeypatch.setattr(engine_module, "discover_markets", _discovery_mock(engine, result))
    await asyncio.wait_for(engine._discovery_loop(), timeout=1)

    assert runtime.market.is_tradable
    assert engine._current_market is market
    assert runtime.status == "ACTIVE"


def test_market_is_not_active_at_its_end_timestamp(monkeypatch):
    market = Market("m", "c", "u", "d", 100, 200)
    monkeypatch.setattr("btcbot.models.time.time", lambda: 200)
    assert not market.is_active


@pytest.mark.asyncio
async def test_old_terminal_market_state_is_pruned(monkeypatch):
    now = int(time.time())
    market = _market("old-terminal", now - 400)
    engine = Engine()
    engine._clock_is_sane = lambda: True
    engine._market_states[market.slug] = MarketRuntime(market, status="RESOLVED")
    monkeypatch.setattr(engine_module, "CONFIG", replace(
        engine_module.CONFIG, discovery_lookahead_sec=30
    ))

    await engine._manage_markets()

    assert market.slug not in engine._market_states


def test_stale_binance_or_chainlink_data_blocks_activation():
    now = time.time()
    market = _market("btc-updown-5m-1", int(now) - 1)
    engine = Engine()
    engine._clock_is_sane = lambda: True
    engine._books_ready = lambda _market: True
    engine._binance.latest_ts = now - 20
    engine._chainlink.latest_ts = now
    assert not engine._market_inputs_fresh(market)

    engine._binance.latest_ts = now
    engine._chainlink.latest_ts = now
    assert engine._market_inputs_fresh(market)


@pytest.mark.asyncio
async def test_resolving_old_market_does_not_clear_new_market_position(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "CONFIG", replace(db.CONFIG, db_path=tmp_path / "markets.db"))
    await db.init_db()
    now = int(time.time())
    old = _market("old", now - 600)
    current = _market("current", now - 100)
    position_old = OpenPosition(old, "UP", old.up_token_id, 0.4, 5)
    position_current = OpenPosition(current, "DOWN", current.down_token_id, 0.5, 5)
    engine = Engine()
    engine._position = position_current
    engine._positions_by_slug = {old.slug: position_old, current.slug: position_current}
    engine._risk.open_positions = [position_current]
    engine._market_states[old.slug] = MarketRuntime(old, status="RESOLVING")
    engine._market_states[current.slug] = MarketRuntime(current, status="ACTIVE")
    engine._fetch_oracle_outcome = AsyncMock(return_value="UP")

    async with db.connect() as conn:
        await repo.upsert_market(conn, old)
        await repo.upsert_market(conn, current)
        await repo.save_open_position(
            conn, old.slug, "UP", old.up_token_id, 0.4, 5, time.time()
        )
        await repo.save_open_position(
            conn, current.slug, "DOWN", current.down_token_id, 0.5, 5, time.time()
        )

    await engine._resolve_position(position_old, old)

    assert engine._position is position_current
    assert engine._risk.open_positions == [position_current]
    assert old.slug not in engine._positions_by_slug
    assert engine._market_states[old.slug].status == "RESOLVED"
    async with db.connect() as conn:
        assert [item["market_slug"] for item in await repo.load_open_positions(conn)] == [current.slug]


@pytest.mark.asyncio
async def test_two_token_positions_persist_and_clear_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "CONFIG", replace(db.CONFIG, db_path=tmp_path / "paired.db"))
    await db.init_db()
    market = _market("paired", int(time.time()) - 10)
    async with db.connect() as conn:
        await repo.upsert_market(conn, market)
        await repo.save_open_position(conn, market.slug, "UP", market.up_token_id, 0.4, 5, time.time(), strategy_id="paired_tilt_exit")
        await repo.save_open_position(conn, market.slug, "DOWN", market.down_token_id, 0.5, 5, time.time(), strategy_id="paired_tilt_exit")
        assert len(await repo.load_open_positions(conn)) == 2
        await repo.clear_open_position(conn, market.slug, market.up_token_id)
        remaining = await repo.load_open_positions(conn)
        assert [(item["direction"], item["token_quantity"]) for item in remaining] == [("DOWN", 5)]
        await repo.clear_open_position(conn, market.slug)
        assert await repo.load_open_positions(conn) == []


@pytest.mark.asyncio
async def test_restart_restores_open_market_position(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "CONFIG", replace(db.CONFIG, db_path=tmp_path / "restart.db"))
    await db.init_db()
    market = _market("restart-market", int(time.time()) - 10)
    async with db.connect() as conn:
        await repo.upsert_market(conn, market)
        await repo.save_open_position(
            conn, market.slug, "UP", market.up_token_id, 0.4, 5, time.time()
        )

    engine = Engine()
    await engine._restore_state()

    assert market.slug in engine._positions_by_slug
    assert engine._positions_by_slug[market.slug].token_quantity == 5
    assert engine._market_states[market.slug].status == "DISCOVERED"


@pytest.mark.asyncio
async def test_paper_cash_counts_sell_proceeds_and_only_remaining_payout(tmp_path, monkeypatch):
    from btcbot.models import TradeRecord

    monkeypatch.setattr(db, "CONFIG", replace(db.CONFIG, db_path=tmp_path / "cash.db"))
    await db.init_db()
    market = _market("cash", int(time.time()) - 10)
    async with db.connect() as conn:
        await repo.upsert_market(conn, market)
        for trade in (
            TradeRecord(market.slug, "ENTRY", "UP", market.up_token_id, "BUY", 2.5, 0.5, 5, 0.8, 0.1, fee_usd=0.1, is_paper=True),
            TradeRecord(market.slug, "ENTRY", "DOWN", market.down_token_id, "BUY", 2.5, 0.5, 5, 0.8, 0.1, fee_usd=0.1, is_paper=True),
            TradeRecord(market.slug, "EXIT", "DOWN", market.down_token_id, "SELL", 2, 0.4, 5, 0.8, 0.1, fee_usd=0.1, is_paper=True),
        ):
            await repo.insert_trade(conn, trade)
        await repo.set_market_outcome(conn, market.slug, "DOWN")
        assert await repo.paper_cash_balance(conn, 100) == pytest.approx(96.7)
