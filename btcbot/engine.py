"""Main trading engine — orchestrates feeds, signals, execution, and risk."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, replace

import httpx

from .config import CONFIG
from .feeds.binance_ws import BinanceFeed
from .feeds.chainlink import ChainlinkFeed
from .feeds.polymarket_chainlink_ws import PolymarketChainlinkFeed
from .feeds.polymarket_ws import PolymarketFeed
from .market_discovery import discover_markets
from .models import Market, OpenPosition, TradeRecord
from .fees import taker_fee_per_share
from .paired_strategy import (
    CALIBRATION_BIN, calibration_bins, calibrated_probability, paired_shares, settlement_pnl,
    should_exit_weak_side, strong_direction, walk_book,
)
from .regime import RegimeDetector
from .risk import RiskManager
from .strategies import create_signal_generator
from .storage.db import connect, init_db
from .storage.repo import (
    clear_open_position,
    get_market,
    insert_btc_price,
    insert_trade,
    load_open_position,
    load_open_positions,
    pnl_since,
    save_open_position,
    set_market_outcome,
    set_market_start_price,
    trailing_loss_streak,
    trades_for_market,
    update_daily_pnl,
    upsert_market,
    upsert_result,
    win_loss_counts,
)

log = logging.getLogger(__name__)


@dataclass
class MarketRuntime:
    market: Market
    status: str = "DISCOVERED"
    discovered_at: float = 0.0
    subscribed_at: float | None = None
    ready_at: float | None = None


def _summarize_market_result(trades: list[TradeRecord], outcome: str) -> tuple[float, float, float, float, int | None]:
    """Return entry cost, hedge cost, payout, net P&L, and outcome classification."""
    entry_cost = sum(t.amount_usd + t.fee_usd for t in trades if t.trade_type == "ENTRY")
    hedge_cost = sum(t.amount_usd + t.fee_usd for t in trades if t.trade_type == "HEDGE")
    payout = sum(
        t.token_quantity for t in trades
        if t.direction == outcome and t.side == "BUY"
    ) - sum(
        t.token_quantity for t in trades
        if t.direction == outcome and t.side == "SELL"
    )
    sale_proceeds = sum(t.amount_usd - t.fee_usd for t in trades if t.trade_type == "EXIT")
    net_pnl = payout + sale_proceeds - entry_cost - hedge_cost

    entries = [t for t in trades if t.trade_type == "ENTRY"]
    if not entries:
        outcome_correct = None
    elif {t.direction for t in entries} == {"UP", "DOWN"}:
        outcome_correct = int(net_pnl > 0)
    else:
        # Classify on the entry direction regardless of hedging. A hedged
        # market that wins on the hedge side is still an "incorrect" entry,
        # which matters for stats and — critically — for the auto-redeem
        # trigger which gates on this field.
        outcome_correct = 1 if entries[0].direction == outcome else 0

    return entry_cost, hedge_cost, payout, net_pnl, outcome_correct


class Engine:
    """Runs 5 concurrent async loops under a single TaskGroup."""

    def __init__(self, paper_mode: bool = True) -> None:
        self._paper_mode = paper_mode
        self._auto_trading_enabled = CONFIG.auto_trading
        self._stop = asyncio.Event()
        self._price_event = asyncio.Event()
        self._market_event = asyncio.Event()
        self._discovery_lock = asyncio.Lock()
        self._discovery_wakeup = asyncio.Event()
        self._order_lock = asyncio.Lock()

        # Components
        self._binance = BinanceFeed(on_price=self._on_btc_price)
        self._chainlink = PolymarketChainlinkFeed(on_price=self._on_chainlink_price)
        self._chainlink_onchain = ChainlinkFeed()
        self._polymarket = PolymarketFeed(on_price=self._on_poly_price)
        self._signal_strategy = CONFIG.strategy
        self._signal_gen = create_signal_generator(self._signal_strategy)
        self._risk = RiskManager()
        self._regime = RegimeDetector(window=CONFIG.regime_window)

        # Build executor
        if paper_mode:
            from .paper import PaperExecutor
            self._executor: object = PaperExecutor()
        else:
            from .execution import Executor
            self._executor = Executor()

        # State
        self._current_market: Market | None = None
        self._position: OpenPosition | None = None
        self._positions_by_slug: dict[str, OpenPosition] = {}
        self._paired_positions_by_slug: dict[str, list[OpenPosition]] = {}
        self._paired_samples: deque[dict[str, float]] = deque(maxlen=30)
        self._paired_sample_ts = 0.0
        self._probability_bins: dict[int, tuple[int, int]] = {}
        self._market_states: dict[str, MarketRuntime] = {}
        self._resolving_markets: set[str] = set()
        self._http_client: httpx.AsyncClient | None = None
        self._btc_price_sample_ts: float = 0.0
        self._clock_sample: tuple[float, float, float] | None = None
        self._clock_warning = False
        self._market_gap_started_at: float | None = None
        self._current_date: str = datetime.date.today().isoformat()
        self._mid_window_price: float | None = None
        self._mid_captured: bool = False

    async def run(self) -> None:
        """Main lifecycle. Initialises DB, then runs concurrent loops."""
        mode = "PAPER" if self._paper_mode else "LIVE"
        log.info("Starting BTC bot in %s mode (bankroll=$%.2f)", mode, CONFIG.bankroll)
        if CONFIG.position_mode == "fixed":
            if CONFIG.fixed_size_type == "usd":
                log.info("Position Mode: FIXED")
                log.info("Fixed Size Type: USD")
                log.info("Fixed Position: %.2f USDC", CONFIG.fixed_position_usd)
            else:
                log.info("Position Mode: FIXED")
                log.info("Fixed Size Type: SHARES")
                log.info("Fixed Position: %.2f Shares", CONFIG.fixed_position_shares)
        else:
            log.info("Position Mode: DYNAMIC")
            log.info("Position Algorithm: Quarter-Kelly")
            log.info("Min Position: %.2f USDC", CONFIG.min_position_usd)
            log.info("Max Position: %.2f USDC", CONFIG.max_position_usd)

        await init_db()
        await self._restore_state()
        self._http_client = httpx.AsyncClient(
            headers={"User-Agent": "btcbot/0.1"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._binance.run(), name="binance_ws")
                tg.create_task(self._chainlink.run(), name="chainlink_rtds")
                tg.create_task(self._chainlink_onchain.run(), name="chainlink_onchain")
                tg.create_task(self._polymarket.run(), name="polymarket_ws")
                tg.create_task(self._discovery_loop(), name="discovery")
                tg.create_task(self._market_manager_loop(), name="market_manager")
                tg.create_task(self._trading_loop(), name="trading")
                tg.create_task(self._risk_monitor_loop(), name="risk")
                tg.create_task(self._sweep_unresolved_loop(), name="sweep")
        except* KeyboardInterrupt:
            log.info("Keyboard interrupt — shutting down")
        finally:
            await self._binance.stop()
            await self._chainlink.stop()
            await self._chainlink_onchain.stop()
            await self._polymarket.stop()
            if self._http_client:
                await self._http_client.aclose()
            log.info("Engine stopped")

    # ── State recovery ─────────────────────────────────────────────────

    async def _restore_state(self) -> None:
        """Restore open position and regime detector from DB."""
        await self._seed_regime()
        try:
            async with connect() as conn:
                today_ts = int(
                    datetime.datetime.combine(
                        datetime.date.today(), datetime.time.min
                    ).timestamp()
                )
                self._risk.daily_pnl = await pnl_since(conn, today_ts)
                self._risk.sync_streak(await trailing_loss_streak(conn))

                positions = await load_open_positions(conn)
                trade_cache = {}
                for pos_data in positions:
                    market_row = await get_market(conn, pos_data["market_slug"])
                    if not market_row:
                        log.warning("Saved position references unknown market %s", pos_data["market_slug"])
                        await clear_open_position(conn, pos_data["market_slug"])
                        continue
                    if market_row.slug not in trade_cache:
                        trade_cache[market_row.slug] = await trades_for_market(conn, market_row.slug)
                    market_trades = trade_cache[market_row.slug]
                    token_trades = [t for t in market_trades if t.token_id == pos_data["token_id"]]
                    if any(t.side == "BUY" for t in token_trades):
                        remaining = sum(t.token_quantity for t in token_trades if t.side == "BUY") - sum(
                            t.token_quantity for t in token_trades if t.side == "SELL"
                        )
                        if remaining <= 0.000001:
                            await clear_open_position(conn, market_row.slug, pos_data["token_id"])
                            continue
                        pos_data["token_quantity"] = min(
                            float(pos_data["token_quantity"]), remaining
                        )
                    market = Market(
                        slug=market_row.slug,
                        condition_id=market_row.condition_id,
                        up_token_id=market_row.up_token_id,
                        down_token_id=market_row.down_token_id,
                        start_ts=market_row.start_ts,
                        end_ts=market_row.end_ts,
                        active=False,
                        accepting_orders=False,
                    )
                    position = OpenPosition(
                        market=market,
                        direction=pos_data["direction"],
                        token_id=pos_data["token_id"],
                        fill_price=pos_data["fill_price"],
                        token_quantity=pos_data["token_quantity"],
                        entry_time=pos_data.get("entry_time", time.time()),
                        hedge_count=pos_data.get("hedge_count", 0),
                        hedge_amount_usd=pos_data.get("hedge_amount_usd", 0.0),
                        hedge_token_quantity=pos_data.get("hedge_token_quantity", 0.0),
                        hedge_fill_price=pos_data.get("hedge_fill_price"),
                        strategy_id=pos_data.get("strategy_id", "btc_momentum_regime"),
                    )
                    self._paired_positions_by_slug.setdefault(market.slug, []).append(position)
                    self._positions_by_slug.setdefault(market.slug, position)
                    self._market_states[market.slug] = MarketRuntime(
                        market=market, discovered_at=time.time()
                    )
                    log.info("Restored position: %s %s @ $%.3f", pos_data["direction"], market.slug, pos_data["fill_price"])
                if positions:
                    self._market_event.set()
            await self._refresh_probability_calibration()
        except Exception:
            log.warning("Failed to restore position state", exc_info=True)

    async def _refresh_probability_calibration(self) -> None:
        """Calibrate directional signal probabilities from resolved historical entries."""
        try:
            async with connect() as conn:
                cur = await conn.execute(
                    """SELECT t.market_slug,
                              AVG(CASE WHEN t.direction = 'UP' THEN t.signal_fair_prob
                                       ELSE 1.0 - t.signal_fair_prob END), m.outcome
                       FROM trades t JOIN markets m ON m.slug = t.market_slug
                       WHERE t.trade_type = 'ENTRY' AND t.signal_fair_prob IS NOT NULL
                         AND m.outcome IS NOT NULL
                       GROUP BY t.market_slug"""
                )
                rows = await cur.fetchall()
            observations = []
            for _, probability_up, outcome in rows:
                probability_up = float(probability_up)
                up_bin = min(int((probability_up + 1e-12) / CALIBRATION_BIN), 19)
                down_probability = 1.0 - probability_up
                down_bin = min(int((down_probability + 1e-12) / CALIBRATION_BIN), 19)
                if up_bin != down_bin:
                    observations.extend((
                        (probability_up, str(outcome) == "UP"),
                        (down_probability, str(outcome) == "DOWN"),
                    ))
            self._probability_bins = calibration_bins(observations)
            supported = sum(1 for count, _ in self._probability_bins.values() if count >= 20)
            log.info(
                "Signal calibration: %d resolved direction samples; %d probability bins meet the 20-sample floor",
                len(observations), supported,
            )
        except Exception:
            log.warning("Could not load historical signal calibration", exc_info=True)

    async def _seed_regime(self) -> None:
        """Seed regime detector from recent historical data so it starts informed."""
        try:
            async with connect() as conn:
                cur = await conn.execute(
                    """SELECT m.start_ts, m.start_btc_price, m.outcome
                       FROM markets m
                       WHERE m.outcome IS NOT NULL AND m.start_btc_price IS NOT NULL
                       ORDER BY m.start_ts DESC
                       LIMIT ?""",
                    (CONFIG.regime_window,),
                )
                rows = await cur.fetchall()

            # Process oldest first so the buffer ends with the most recent
            seeded = 0
            for start_ts, start_price, outcome in reversed(rows):
                mid_ts = start_ts + 150
                async with connect() as conn:
                    cur = await conn.execute(
                        "SELECT price FROM btc_prices WHERE ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT 1",
                        (mid_ts - 15, mid_ts + 15),
                    )
                    mid_row = await cur.fetchone()
                if mid_row:
                    first_half_dir = "UP" if mid_row[0] >= start_price else "DOWN"
                    self._regime.record(first_half_dir, outcome)
                    seeded += 1

            if seeded:
                log.info(
                    "Regime detector seeded: %d samples, choppiness=%.2f",
                    seeded, self._regime.choppiness,
                )
        except Exception:
            log.warning("Failed to seed regime detector", exc_info=True)

    # ── Callbacks ──────────────────────────────────────────────────────

    async def _on_btc_price(self, price: float, ts: float) -> None:
        """Called on every Binance tick."""
        self._signal_gen.update_btc_price(price, ts)
        self._price_event.set()
        self._market_event.set()

        # Sample BTC prices to DB every 5 seconds
        if ts - self._btc_price_sample_ts >= 5.0:
            self._btc_price_sample_ts = ts
            try:
                async with connect() as conn:
                    await insert_btc_price(conn, price, int(ts))
            except Exception:
                pass

    async def _on_chainlink_price(self, price: float, ts: float) -> None:
        """Called on every Chainlink update — this is the oracle's price source."""
        self._signal_gen.update_chainlink_price(price, ts)
        self._price_event.set()
        self._market_event.set()

        # Capture mid-window price for regime detection (~150s into the window)
        mkt = self._current_market
        if mkt and not self._mid_captured:
            elapsed = ts - mkt.start_ts
            if elapsed >= 150:
                self._mid_window_price = price
                self._mid_captured = True

    async def _on_poly_price(self, _token_id: str, _price: float) -> None:
        """Called on every Polymarket price update."""
        self._price_event.set()
        self._market_event.set()

    # ── Discovery loop ─────────────────────────────────────────────────

    async def _discovery_loop(self) -> None:
        """Refresh current/future Gamma metadata without blocking trading or switching."""
        backoff = CONFIG.discovery_interval_sec
        while not self._stop.is_set():
            self._discovery_wakeup.clear()
            # Reset risk counters at midnight
            today = datetime.date.today().isoformat()
            if today != self._current_date:
                log.info("New day %s — resetting daily risk counters", today)
                self._risk.reset_daily()
                self._current_date = today

            delay = CONFIG.discovery_interval_sec
            try:
                async with self._discovery_lock:
                    result = await discover_markets(self._http_client)
                if result.server_time is not None:
                    self._clock_sample = (time.time(), time.monotonic(), result.server_time)
                    self._clock_warning = False
                elif self._clock_sample is None:
                    log.error("Gamma response has no server Date header; market switching is disabled")

                now = time.time()
                found = {market.slug: market for market in result.markets}
                for market in found.values():
                    runtime = self._market_states.get(market.slug)
                    persist_market = runtime is None
                    if runtime is None:
                        runtime = MarketRuntime(market=market, discovered_at=now)
                        self._market_states[market.slug] = runtime
                        self._transition(runtime, "PREPARING")
                        lead = market.start_ts - now
                        log.info("[DISCOVERY] Found future market: %s (starts in %.0fs)", market.slug, lead)
                        log.info("[PREPARE] Preparing market: %s", market.slug)
                    else:
                        old = runtime.market
                        identity_changed = (
                            old.condition_id,
                            old.up_token_id,
                            old.down_token_id,
                            old.start_ts,
                            old.end_ts,
                        ) != (
                            market.condition_id,
                            market.up_token_id,
                            market.down_token_id,
                            market.start_ts,
                            market.end_ts,
                        )
                        if identity_changed and runtime.status in {
                            "ACTIVE", "EXPIRED", "RESOLVING", "RESOLVED"
                        }:
                            log.error("Market identity changed after preparation for %s; retaining known identity", market.slug)
                            runtime.market.active = False
                            runtime.market.accepting_orders = False
                            if self._current_market and self._current_market.slug == market.slug:
                                self._current_market.active = False
                                self._current_market.accepting_orders = False
                            continue
                        persist_market = (
                            old.condition_id != market.condition_id
                            or old.up_token_id != market.up_token_id
                            or old.down_token_id != market.down_token_id
                            or old.start_ts != market.start_ts
                            or old.end_ts != market.end_ts
                        )
                        runtime.market = market
                        if self._current_market and self._current_market.slug == market.slug:
                            self._current_market = market
                            if self._position:
                                self._position.market = market
                        if runtime.status == "DISCOVERED":
                            self._transition(runtime, "PREPARING")
                        if identity_changed:
                            runtime.ready_at = None
                            runtime.subscribed_at = None
                            if runtime.status == "READY":
                                self._transition(runtime, "SUBSCRIBING")

                    if runtime.subscribed_at is None:
                        log.info("[SUBSCRIBE] Subscribing UP/DOWN tokens for %s", market.slug)
                    if persist_market:
                        try:
                            async with connect() as conn:
                                await upsert_market(conn, market)
                        except Exception:
                            log.warning("Failed to persist discovered market %s", market.slug, exc_info=True)

                # A successful empty response is a real status change; transport errors retain
                # the last known state so a Gamma outage cannot stop the active market.
                for slug in result.requested_slugs - result.failed_slugs - found.keys():
                    runtime = self._market_states.get(slug)
                    if runtime and runtime.status not in {"EXPIRED", "RESOLVING", "RESOLVED"}:
                        runtime.market.active = False
                        runtime.market.accepting_orders = False
                        if self._current_market and self._current_market.slug == slug:
                            self._current_market.active = False
                            self._current_market.accepting_orders = False

                subscriptions = {
                    token
                    for runtime in self._market_states.values()
                    if runtime.market.end_ts > now and not runtime.market.closed
                    for token in (runtime.market.up_token_id, runtime.market.down_token_id)
                }
                await self._polymarket.set_token_ids(subscriptions)
                for runtime in self._market_states.values():
                    if runtime.subscribed_at is None and runtime.market.end_ts > now:
                        runtime.subscribed_at = time.time()
                        if runtime.status == "PREPARING":
                            self._transition(runtime, "SUBSCRIBING")

                if result.failed_slugs:
                    backoff = min(
                        max(CONFIG.discovery_interval_sec, backoff * 2),
                        CONFIG.discovery_max_backoff_sec,
                    )
                    delay = backoff
                    log.warning("Gamma scan had %d failed request(s); retrying in %.1fs", len(result.failed_slugs), delay)
                else:
                    backoff = CONFIG.discovery_interval_sec
                self._market_event.set()
            except Exception:
                backoff = min(
                    max(CONFIG.discovery_interval_sec, backoff * 2),
                    CONFIG.discovery_max_backoff_sec,
                )
                delay = backoff
                log.warning("Discovery error; retrying in %.1fs", delay, exc_info=True)
            try:
                await asyncio.wait_for(self._discovery_wakeup.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def request_market_refresh(self) -> None:
        """Wake the discovery loop for an immediate market scan."""
        self._discovery_wakeup.set()

    def _transition(self, runtime: MarketRuntime, status: str) -> None:
        allowed = {
            "DISCOVERED": {"PREPARING", "EXPIRED"},
            "PREPARING": {"SUBSCRIBING", "EXPIRED"},
            "SUBSCRIBING": {"READY", "EXPIRED"},
            "READY": {"SUBSCRIBING", "ACTIVE", "EXPIRED"},
            "ACTIVE": {"EXPIRED"},
            "EXPIRED": {"RESOLVING", "RESOLVED"},
            "RESOLVING": {"RESOLVED"},
            "RESOLVED": set(),
        }
        if status == runtime.status:
            return
        if status not in allowed.get(runtime.status, set()):
            log.error("Illegal market state transition %s: %s -> %s", runtime.market.slug, runtime.status, status)
            return
        runtime.status = status

    def _clock_is_sane(self) -> bool:
        sample = self._clock_sample
        if sample is None:
            return False
        local_at_sample, monotonic_at_sample, server_time = sample
        age = time.monotonic() - monotonic_at_sample
        wall_drift = (time.time() - local_at_sample) - age
        healthy = age >= 0 and abs(local_at_sample - server_time) <= 5 and abs(wall_drift) <= 2
        if not healthy and not self._clock_warning:
            log.error("System clock is out of sync with Gamma; market switching and trading are paused")
            self._clock_warning = True
        return healthy

    def _books_ready(self, market: Market) -> bool:
        age = CONFIG.market_data_max_age_sec
        return self._polymarket.has_fresh_book(market.up_token_id, age) and self._polymarket.has_fresh_book(
            market.down_token_id, age
        )

    def _market_inputs_fresh(self, market: Market) -> bool:
        if not self._clock_is_sane() or not market.is_tradable or not self._books_ready(market):
            return False
        now = time.time()
        max_age = CONFIG.market_data_max_age_sec
        for timestamp in (self._binance.latest_ts, self._chainlink.latest_ts):
            data_age = now - timestamp
            if timestamp <= 0 or data_age < -1 or data_age > max_age:
                return False
        return True

    async def _market_manager_loop(self) -> None:
        """Activate prepared markets at their timestamps and retire ended ones."""
        while not self._stop.is_set():
            self._market_event.clear()
            await self._manage_markets()
            now = time.time()
            boundaries = [
                boundary
                for runtime in self._market_states.values()
                for boundary in (runtime.market.start_ts, runtime.market.end_ts)
                if boundary > now
            ]
            timeout = min([1.0, *(boundary - now for boundary in boundaries)])
            try:
                await asyncio.wait_for(self._market_event.wait(), timeout=max(0.01, timeout))
            except asyncio.TimeoutError:
                pass

    async def _manage_markets(self) -> None:
        if not self._clock_is_sane():
            return
        now = time.time()
        for slug, runtime in list(self._market_states.items()):
            if (
                runtime.status in {"EXPIRED", "RESOLVED"}
                and runtime.market.end_ts + CONFIG.discovery_lookahead_sec < now
                and slug not in self._positions_by_slug
                and not self._paired_positions_by_slug.get(slug)
                and slug not in self._resolving_markets
            ):
                self._market_states.pop(slug, None)
        for runtime in list(self._market_states.values()):
            if runtime.status in {"EXPIRED", "RESOLVING", "RESOLVED"}:
                continue
            if runtime.market.end_ts <= now or (runtime.status == "ACTIVE" and not runtime.market.is_tradable):
                await self._expire_market(runtime)
                continue
            if runtime.status == "READY" and not self._books_ready(runtime.market):
                runtime.ready_at = None
                self._transition(runtime, "SUBSCRIBING")
            elif runtime.status == "SUBSCRIBING" and self._books_ready(runtime.market):
                runtime.ready_at = now
                self._transition(runtime, "READY")
                log.info(
                    "[READY] Market %s orderbook ready (discovery-to-ready=%.2fs)",
                    runtime.market.slug,
                    now - runtime.discovered_at,
                )
            elif runtime.status == "ACTIVE":
                await self._polymarket.reconnect_if_stale(
                    {runtime.market.up_token_id, runtime.market.down_token_id},
                    CONFIG.market_data_max_age_sec,
                )
            if runtime.status == "SUBSCRIBING":
                await self._polymarket.retry_stale_subscriptions(
                    {runtime.market.up_token_id, runtime.market.down_token_id},
                    CONFIG.market_data_max_age_sec,
                )

        current = self._current_market
        if current and current.end_ts <= now:
            runtime = self._market_states.get(current.slug)
            if runtime:
                await self._expire_market(runtime)
        if self._current_market is not None:
            return

        for runtime in sorted(self._market_states.values(), key=lambda item: item.market.start_ts):
            market = runtime.market
            if runtime.status == "READY" and market.start_ts <= now < market.end_ts and self._market_inputs_fresh(market):
                await self._switch_market(runtime)
                return

    async def _expire_market(self, runtime: MarketRuntime) -> None:
        market = runtime.market
        async with self._order_lock:
            if runtime.status not in {"EXPIRED", "RESOLVING", "RESOLVED"}:
                was_active = self._current_market is not None and self._current_market.slug == market.slug
                if was_active:
                    self._current_market = None
                    self._position = None
                    self._risk.open_positions[:] = [
                        position for position in self._risk.open_positions if position.market.slug != market.slug
                    ]
                    self._market_gap_started_at = time.time()
                self._transition(runtime, "EXPIRED")
                log.info("[EXPIRE] Market %s expired", market.slug)
                position = self._positions_by_slug.get(market.slug)
                if position:
                    self._transition(runtime, "RESOLVING")
                    self._schedule_resolution(
                        position,
                        market,
                        self._mid_window_price if was_active else None,
                        self._signal_gen._chainlink_start_price if was_active else None,
                    )
                    log.info("[RESOLVE] Market %s resolving", market.slug)
                if was_active:
                    self._signal_gen.reset(market)
                    self._mid_window_price = None
                    self._mid_captured = False

    def _schedule_resolution(
        self,
        position: OpenPosition,
        market: Market,
        mid_price: float | None,
        start_price: float | None,
    ) -> None:
        if market.slug in self._resolving_markets:
            return
        self._resolving_markets.add(market.slug)
        asyncio.create_task(
            self._resolve_position(position, market, mid_price, start_price),
            name=f"resolve-{market.slug}",
        )

    async def _switch_market(self, runtime: MarketRuntime) -> None:
        """Activate one prepared market without restarting shared feeds."""
        market = runtime.market
        async with self._order_lock:
            now = time.time()
            if (
                runtime.status != "READY"
                or not market.start_ts <= now < market.end_ts
                or not self._market_inputs_fresh(market)
            ):
                return
            previous_slug = self._current_market.slug if self._current_market else None
            self._current_market = market
            positions = self._paired_positions_by_slug.get(market.slug, [])
            self._position = positions[0] if positions else self._positions_by_slug.get(market.slug)
            self._risk.open_positions = list(positions) if positions else ([self._position] if self._position else [])
            self._paired_samples.clear()
            self._paired_sample_ts = 0.0
            self._transition(runtime, "ACTIVE")
            if self._signal_strategy != CONFIG.strategy:
                self._signal_strategy = CONFIG.strategy
                self._signal_gen = create_signal_generator(self._signal_strategy)
                log.info("[STRATEGY] Activated %s", self._signal_strategy)
            self._signal_gen.reset(market)
            self._mid_window_price = None
            self._mid_captured = False

            for ts, price in self._binance.prices_since(market.start_ts):
                self._signal_gen.update_btc_price(price, ts)
            if self._chainlink.latest_ts >= market.start_ts:
                self._signal_gen.update_chainlink_price(self._chainlink.latest_price, self._chainlink.latest_ts)

            start_price = 0.0
            if self._chainlink.latest_ts >= market.start_ts:
                start_price = self._chainlink.latest_price
            elif self._binance.latest_ts >= market.start_ts:
                start_price = self._binance.latest_price
            if start_price > 0:
                try:
                    async with connect() as conn:
                        await set_market_start_price(conn, market.slug, start_price)
                except Exception:
                    log.warning("Failed to persist start price for %s", market.slug, exc_info=True)

            boundary_ready = runtime.ready_at is not None and runtime.ready_at <= market.start_ts
            delay = max(0.0, now - market.start_ts)
            gap = now - self._market_gap_started_at if self._market_gap_started_at is not None else 0.0
            log.info(
                "[SWITCH] %s -> %s (boundary-delay=%.3fs ready-at-boundary=%s gap=%.3fs)",
                previous_slug or "none", market.slug, delay, boundary_ready, gap,
            )
            log.info("[ACTIVE] Trading market: %s", market.slug)
            self._market_gap_started_at = None

    async def _fetch_oracle_outcome(self, slug: str) -> str | None:
        """Poll the Gamma API for the oracle-resolved outcome."""
        for _ in range(80):
            try:
                resp = await self._http_client.get(
                    f"{CONFIG.gamma_api_base}/events",
                    params={"slug": slug},
                    timeout=5.0,
                )
                resp.raise_for_status()
                data = resp.json()
                if data:
                    event = data[0] if isinstance(data, list) else data
                    market = event.get("markets", [{}])[0]
                    outcomes = market.get("outcomes", [])
                    prices = market.get("outcomePrices", [])
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                    if "1" in prices:
                        return outcomes[prices.index("1")].upper()
            except Exception:
                log.debug("Outcome not available for %s yet", slug, exc_info=True)
            await asyncio.sleep(15)
        return None

    async def _resolve_position(
        self,
        pos: OpenPosition | None = None,
        mkt: Market | None = None,
        mid_window_price: float | None = None,
        chainlink_start_price: float | None = None,
    ) -> None:
        """Resolve the current position using the Polymarket oracle outcome."""
        pos = pos or self._position
        mkt = mkt or self._current_market
        if not pos or not mkt:
            return

        persisted = False
        payout = 0.0
        try:
            outcome = await self._fetch_oracle_outcome(mkt.slug)
            if outcome is None:
                log.warning("Could not fetch oracle outcome for %s; retaining position for retry", mkt.slug)
                return

            btc_end = self._binance.latest_price
            mid_window_price = mid_window_price if mid_window_price is not None else self._mid_window_price
            chainlink_start_price = (
                chainlink_start_price
                if chainlink_start_price is not None
                else self._signal_gen._chainlink_start_price
            )
            if mid_window_price is not None and chainlink_start_price is not None:
                first_half_dir = "UP" if mid_window_price >= chainlink_start_price else "DOWN"
                self._regime.record(first_half_dir, outcome)

            status = "WIN" if pos.direction == outcome else "LOSS"
            async with connect() as conn:
                market_trades = await trades_for_market(conn, mkt.slug)
                entry_cost, hedge_cost, payout, net_pnl, outcome_correct = _summarize_market_result(
                    market_trades, outcome
                )
                if pos.strategy_id == "paired_tilt_exit":
                    status = "WIN" if net_pnl > 0 else "LOSS"

                log.info(
                    "Resolved %s: %s%s — PnL=$%.2f (entry=$%.2f hedge=$%.2f payout=$%.2f)",
                    mkt.slug,
                    status,
                    " [HEDGED]" if hedge_cost > 0 else "",
                    net_pnl,
                    entry_cost,
                    hedge_cost,
                    payout,
                )

                await set_market_outcome(conn, mkt.slug, outcome, btc_end)
                await upsert_result(
                    conn, mkt.slug,
                    entry_cost=entry_cost,
                    hedge_cost=hedge_cost,
                    payout=payout,
                    net_pnl=net_pnl,
                    outcome_correct=outcome_correct,
                )
                today_ts = int(
                    datetime.datetime.combine(
                        datetime.date.today(), datetime.time.min
                    ).timestamp()
                )
                self._risk.daily_pnl = await pnl_since(conn, today_ts)
                self._risk.sync_streak(await trailing_loss_streak(conn))

                # Update daily P&L aggregate
                today = datetime.date.today().isoformat()
                wins, losses, hedged = await win_loss_counts(conn, since_ts=today_ts)
                cur = await conn.execute(
                    "SELECT COALESCE(SUM(net_pnl_usd), 0) FROM market_results WHERE resolved_at >= ?",
                    (today_ts,),
                )
                row = await cur.fetchone()
                today_pnl = float(row[0])
                await update_daily_pnl(
                    conn, today,
                    trades_count=wins + losses + hedged,
                    wins=wins,
                    losses=losses,
                    hedged=hedged,
                    gross_pnl=today_pnl,
                    net_pnl=today_pnl,
                )
                await clear_open_position(conn, mkt.slug)
            persisted = True
        except Exception:
            log.warning("Failed to persist resolution", exc_info=True)
        finally:
            self._resolving_markets.discard(mkt.slug)

        if not persisted:
            return
        await self._refresh_probability_calibration()
        self._positions_by_slug.pop(mkt.slug, None)
        self._paired_positions_by_slug.pop(mkt.slug, None)
        self._risk.open_positions[:] = [
            position for position in self._risk.open_positions if position.market.slug != mkt.slug
        ]
        if self._position and self._position.market.slug == mkt.slug:
            self._position = None
        runtime = self._market_states.get(mkt.slug)
        if runtime and runtime.status == "RESOLVING":
            self._transition(runtime, "RESOLVED")
        log.info("[RESOLVED] Market %s settled; final PnL=$%+.2f", mkt.slug, net_pnl)

        # Auto-redeem whenever we hold any winning shares — covers plain wins
        # AND hedged markets where the hedge side is the winner.
        if payout > 0 and not self._paper_mode and hasattr(self._executor, "redeem"):
            asyncio.create_task(
                self._redeem_and_mark(mkt.slug, mkt.condition_id),
                name=f"redeem-{mkt.slug}",
            )

    async def _redeem_and_mark(self, slug: str, condition_id: str) -> None:
        """Redeem tokens and mark the result as redeemed in the DB."""
        tx_hash = await self._executor.redeem(condition_id)
        if tx_hash:
            try:
                async with connect() as conn:
                    await conn.execute(
                        "UPDATE market_results SET redeemed_at = ? WHERE market_slug = ?",
                        (int(time.time()), slug),
                    )
                    await conn.commit()
            except Exception:
                log.warning("Failed to mark %s as redeemed", slug, exc_info=True)

    # ── Trading loop ───────────────────────────────────────────────────

    async def _trading_loop(self) -> None:
        """Evaluate signals on every price update and trade when appropriate."""
        while not self._stop.is_set():
            # Wait for a price update
            try:
                await asyncio.wait_for(self._price_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            self._price_event.clear()

            market = self._current_market
            runtime = self._market_states.get(market.slug) if market else None
            if (
                not market or not runtime or runtime.status != "ACTIVE"
                or not runtime.market.is_active or not self._market_inputs_fresh(runtime.market)
            ):
                continue
            market = runtime.market

            signal = self._signal_gen.evaluate(
                btc_price=self._binance.latest_price,
                poly_up_price=self._polymarket.get_price(market.up_token_id),
                poly_down_price=self._polymarket.get_price(market.down_token_id),
                time_remaining_sec=market.seconds_remaining,
                choppiness=self._regime.choppiness,
            )

            if self._signal_strategy == "paired_tilt_exit":
                if self._paired_positions_by_slug.get(market.slug):
                    await self._maybe_exit_pair(market, signal)
                else:
                    await self._enter_pair(market, signal)
                continue

            # Existing single-position strategies retain their original flow.
            if self._position:
                continue

            if signal.fair_prob < 0.60:
                continue

            if signal.strength < CONFIG.min_signal_strength:
                continue

            # Place trade
            shares = None
            if CONFIG.position_mode == "fixed" and CONFIG.fixed_size_type == "shares":
                shares = CONFIG.fixed_position_shares
                amount = shares * (signal.estimated_entry_cost_per_share or signal.poly_implied_prob)
            elif CONFIG.position_mode == "fixed":
                amount = CONFIG.fixed_position_usd
            else:
                amount = self._risk.calc_position_size(
                    signal, choppiness=self._regime.choppiness
                )

            limit_price = signal.expected_entry_price or min(
                signal.poly_implied_prob + CONFIG.limit_slippage, 0.99
            )
            target_amount = (
                shares * (signal.estimated_entry_cost_per_share or limit_price)
                if shares is not None else amount
            )
            if target_amount > CONFIG.max_position_usd:
                log.warning(
                    "ENTRY order market=%s token=%s price=$%.4f target_usd=$%.2f "
                    "target_shares=%.2f submitted_usd=$0.00 submitted_shares=0.00 "
                    "position_mode=%s result=REJECTED reason=max_position_$%.2f",
                    market.slug,
                    market.token_id_for(signal.direction),
                    signal.poly_implied_prob,
                    target_amount,
                    shares if shares is not None else (
                        amount / signal.poly_implied_prob
                        if signal.poly_implied_prob > 0 else 0.0
                    ),
                    CONFIG.position_mode.upper(),
                    CONFIG.max_position_usd,
                )
                continue

            async with self._order_lock:
                runtime = self._market_states.get(market.slug)
                if (
                    self._current_market is None or self._current_market.slug != market.slug
                    or not runtime or runtime.status != "ACTIVE"
                    or not runtime.market.is_active or not self._market_inputs_fresh(runtime.market)
                    or not self._auto_trading_enabled
                    or self._position is not None
                    or not self._risk.can_trade(signal, choppiness=self._regime.choppiness)
                ):
                    continue
                market = runtime.market
                trade = await self._executor.place_trade(market, signal, amount, shares=shares)

                if trade:
                    self._position = OpenPosition(
                        market=market,
                        direction=trade.direction,
                        token_id=trade.token_id,
                        fill_price=trade.fill_price,
                        token_quantity=trade.token_quantity,
                        strategy_id=CONFIG.strategy,
                    )
                    self._positions_by_slug[market.slug] = self._position
                    self._risk.open_positions[:] = [self._position]

                    try:
                        async with connect() as conn:
                            await insert_trade(conn, trade)
                            await save_open_position(
                                conn,
                                market_slug=market.slug,
                                direction=trade.direction,
                                token_id=trade.token_id,
                                fill_price=trade.fill_price,
                                token_quantity=trade.token_quantity,
                                entry_time=self._position.entry_time,
                                hedge_count=self._position.hedge_count,
                                hedge_amount_usd=self._position.hedge_amount_usd,
                                hedge_token_quantity=self._position.hedge_token_quantity,
                                hedge_fill_price=self._position.hedge_fill_price,
                                strategy_id=self._position.strategy_id,
                            )
                    except Exception:
                        log.warning("Failed to persist trade", exc_info=True)

                    log.info(
                        "TRADE %s %s @ $%.3f ($%.2f) — edge=%.3f chop=%.2f",
                        trade.direction,
                        market.slug,
                        trade.fill_price,
                        trade.amount_usd,
                        signal.edge,
                        self._regime.choppiness,
                    )

    async def _enter_pair(self, market: Market, signal) -> None:
        """Preflight both legs by token depth, then place the calibrated share plan."""
        if (
            not self._auto_trading_enabled or market.seconds_remaining <= 60 or signal.probability_up is None
            or signal.fair_prob < 0.60 or signal.strength < CONFIG.min_signal_strength
            or not self._risk.can_trade(signal, choppiness=self._regime.choppiness)
        ):
            return

        up_token, down_token = market.up_token_id, market.down_token_id
        if not all(self._polymarket.has_fresh_book(
            token, CONFIG.market_data_max_age_sec
        ) for token in (up_token, down_token)):
            return
        up_book, down_book = self._polymarket.get_book(up_token), self._polymarket.get_book(down_token)
        if not up_book or not down_book or not up_book.asks or not down_book.asks:
            return

        raw_up = signal.probability_up
        raw_down = 1.0 - raw_up
        p_up = calibrated_probability(raw_up, self._probability_bins)
        p_down = calibrated_probability(raw_down, self._probability_bins)
        up_shares, down_shares = paired_shares(
            CONFIG.paired_base_shares, p_up, p_down,
            CONFIG.paired_tilt_gap, CONFIG.paired_tilt_pct,
        )
        up_shares = math.floor(up_shares * 100) / 100
        down_shares = math.floor(down_shares * 100) / 100

        limits = {}
        order_limits = {}
        plans = {}
        for direction, book, token, shares in (
            ("UP", up_book, up_token, up_shares),
            ("DOWN", down_book, down_token, down_shares),
        ):
            best_ask = book.best_ask
            if best_ask is None or best_ask > CONFIG.paired_max_price:
                return
            limit = best_ask + CONFIG.limit_slippage
            if limit > CONFIG.paired_max_price:
                return
            asks = [(price, size) for price, size in book.asks.items() if price <= limit]
            plan = walk_book(
                asks, shares, selling=False, fee_per_share=taker_fee_per_share,
                slippage=CONFIG.limit_slippage,
            )
            if plan is None:
                return
            limits[direction] = best_ask
            order_limits[direction] = limit
            plans[direction] = plan

        total_cost = plans["UP"][1] + plans["DOWN"][1]
        worst_cost = sum(
            shares * (order_limits[direction] + taker_fee_per_share(min(order_limits[direction], 0.50)))
            for direction, shares in (("UP", up_shares), ("DOWN", down_shares))
        )
        if worst_cost > CONFIG.paired_max_cost_usd:
            log.info(
                "PAIR skipped %s: estimated cost $%.2f; protected maximum $%.2f exceeds cap $%.2f",
                market.slug, total_cost, worst_cost, CONFIG.paired_max_cost_usd,
            )
            return
        up_pnl, down_pnl = settlement_pnl(up_shares, down_shares, total_cost)
        log.info(
            "PAIR preflight %s UP %.2f/DOWN %.2f shares; estimated cost=$%.2f; settlement P&L UP=$%+.2f DOWN=$%+.2f",
            market.slug, up_shares, down_shares, total_cost, up_pnl, down_pnl,
        )

        async with self._order_lock:
            runtime = self._market_states.get(market.slug)
            if (
                self._current_market is None or self._current_market.slug != market.slug
                or not runtime or runtime.status != "ACTIVE" or not runtime.market.is_active
                or not self._market_inputs_fresh(runtime.market)
                or not self._auto_trading_enabled
                or self._paired_positions_by_slug.get(market.slug)
                or not self._risk.can_trade(signal, choppiness=self._regime.choppiness)
            ):
                return
            opened: list[OpenPosition] = []
            filled_trades: list[TradeRecord] = []
            # shortcut: CLOB has no atomic two-token order; track each confirmed leg if another cannot fill.
            for direction, token, shares, prob in (
                ("UP", up_token, up_shares, raw_up),
                ("DOWN", down_token, down_shares, raw_down),
            ):
                leg_signal = replace(
                    signal, direction=direction, fair_prob=prob,
                    poly_implied_prob=(plans[direction][0] if self._paper_mode else limits[direction]),
                    expected_entry_price=(plans[direction][0] if self._paper_mode else limits[direction]),
                    estimated_entry_cost_per_share=plans[direction][1] / shares,
                )
                trade = await self._executor.place_trade(
                    market, leg_signal, max(0.01, shares * limits[direction]), shares=shares
                )
                if not trade:
                    log.warning("PAIR leg unfilled: %s %s; other filled legs remain tracked", market.slug, direction)
                    continue
                position = OpenPosition(
                    market, direction, token, trade.fill_price, trade.token_quantity,
                    strategy_id="paired_tilt_exit",
                )
                opened.append(position)
                filled_trades.append(trade)
                log.info(
                    "PAIR fill %s %s %.2f shares @ $%.4f; fee=$%.4f; slippage vs best ask=$%+.4f/share",
                    market.slug, direction, trade.token_quantity, trade.fill_price,
                    trade.fee_usd, trade.fill_price - limits[direction],
                )
                try:
                    async with connect() as conn:
                        await insert_trade(conn, trade)
                        await save_open_position(
                            conn, market_slug=market.slug, direction=direction,
                            token_id=token, fill_price=trade.fill_price,
                            token_quantity=trade.token_quantity, entry_time=position.entry_time,
                            strategy_id="paired_tilt_exit",
                        )
                except Exception:
                    log.error("Trade %s filled but persistence failed", trade.order_id, exc_info=True)

            if not opened:
                return
            self._paired_positions_by_slug[market.slug] = opened
            self._positions_by_slug[market.slug] = opened[0]
            self._position = opened[0]
            self._risk.open_positions[:] = opened
            actual_cost = sum(t.amount_usd + t.fee_usd for t in filled_trades)
            actual_up = sum(t.token_quantity for t in filled_trades if t.direction == "UP")
            actual_down = sum(t.token_quantity for t in filled_trades if t.direction == "DOWN")
            actual_up_pnl, actual_down_pnl = settlement_pnl(actual_up, actual_down, actual_cost)
            if len(opened) == 2:
                log.info(
                    "PAIR opened %s; actual cost including fees=$%.2f; settlement P&L UP=$%+.2f DOWN=$%+.2f; calibration=%d samples",
                    market.slug, actual_cost, actual_up_pnl, actual_down_pnl,
                    sum(count for count, _ in self._probability_bins.values()),
                )
            else:
                log.warning(
                    "PAIR incomplete %s; actual cost including fees=$%.2f; settlement P&L UP=$%+.2f DOWN=$%+.2f",
                    market.slug, actual_cost, actual_up_pnl, actual_down_pnl,
                )

    async def _maybe_exit_pair(self, market: Market, signal) -> None:
        """Exit only a confirmed weak leg in the final minute when calibrated EV agrees."""
        positions = self._paired_positions_by_slug.get(market.slug, [])
        if market.seconds_remaining > 60 or not positions:
            return
        exit_freshness = min(CONFIG.market_data_max_age_sec, 2.0)
        chainlink_ts = self._chainlink.latest_ts
        binance_ts = self._binance.latest_ts
        start_price = self._signal_gen._chainlink_start_price
        if (
            chainlink_ts <= 0 or binance_ts <= 0
            or time.time() - chainlink_ts > exit_freshness
            or time.time() - binance_ts > exit_freshness
            or not start_price or self._chainlink.latest_price <= 0
            or signal.probability_up is None
        ):
            return
        up_book = self._polymarket.get_book(market.up_token_id)
        down_book = self._polymarket.get_book(market.down_token_id)
        if (
            not up_book or not down_book or up_book.midpoint is None or down_book.midpoint is None
            or not self._polymarket.has_fresh_book(market.up_token_id, exit_freshness)
            or not self._polymarket.has_fresh_book(market.down_token_id, exit_freshness)
        ):
            return
        now = time.monotonic()
        if now - self._paired_sample_ts >= 1.0:
            self._paired_samples.append({
                "momentum_5": self._binance.momentum(5),
                "momentum_15": self._binance.momentum(15),
                "momentum_30": self._binance.momentum(30),
                "start_delta": self._chainlink.latest_price - start_price,
                "poly_gap": up_book.midpoint - down_book.midpoint,
                "sample_ts": now,
            })
            self._paired_sample_ts = now
        strong = strong_direction(
            self._paired_samples,
            min_move=CONFIG.btc_5m_volatility * 0.05,
        )
        if not strong:
            return
        weak = "DOWN" if strong == "UP" else "UP"
        position = next((pos for pos in positions if pos.direction == weak), None)
        if not position or position.token_quantity <= 0:
            return
        raw_weak = signal.probability_up if weak == "UP" else 1.0 - signal.probability_up
        win_probability = calibrated_probability(raw_weak, self._probability_bins)
        raw_strong = signal.probability_up if strong == "UP" else 1.0 - signal.probability_up
        strong_probability = calibrated_probability(raw_strong, self._probability_bins)
        if (
            win_probability is None or strong_probability is None
            or strong_probability - win_probability <= CONFIG.paired_tilt_gap
        ):
            log.info("PAIR exit held %s: no calibrated probability for %s", market.slug, weak)
            return
        book = self._polymarket.get_book(position.token_id)
        if not book or not book.bids or not self._polymarket.has_fresh_book(
            position.token_id, CONFIG.market_data_max_age_sec
        ):
            return
        levels = sorted(book.bids.items(), reverse=True)
        plan = walk_book(
            levels, position.token_quantity, selling=True,
            fee_per_share=taker_fee_per_share, slippage=CONFIG.limit_slippage,
        )
        if plan is None:
            return
        remaining = position.token_quantity
        worst_bid = None
        for price, size in levels:
            remaining -= min(remaining, size)
            worst_bid = price
            if remaining <= 1e-8:
                break
        limit_price = max(
            0.01,
            math.floor(((worst_bid or 0.01) - CONFIG.limit_slippage + 1e-9) * 100) / 100,
        )
        guaranteed_net = position.token_quantity * (
            limit_price - taker_fee_per_share(limit_price)
        )
        if not should_exit_weak_side(position.token_quantity, guaranteed_net, win_probability):
            return
        weak_signal = replace(signal, direction=weak, fair_prob=raw_weak)

        async with self._order_lock:
            current = self._paired_positions_by_slug.get(market.slug, [])
            runtime = self._market_states.get(market.slug)
            now = time.time()
            if (
                position not in current or position.token_quantity <= 0
                or self._current_market is None or self._current_market.slug != market.slug
                or not runtime or runtime.status != "ACTIVE" or not runtime.market.is_active
                or now - self._binance.latest_ts > exit_freshness
                or now - self._chainlink.latest_ts > exit_freshness
                or not self._polymarket.has_fresh_book(market.up_token_id, exit_freshness)
                or not self._polymarket.has_fresh_book(market.down_token_id, exit_freshness)
            ):
                return
            trade = await self._executor.place_exit(
                market, position, position.token_quantity, limit_price, weak_signal
            )
            if not trade:
                return
            if (
                not math.isfinite(trade.token_quantity) or trade.token_quantity <= 0
                or trade.token_quantity > position.token_quantity + 1e-6
            ):
                log.error("Exit returned invalid quantity for %s; leaving position unchanged", market.slug)
                return
            exited = min(position.token_quantity, trade.token_quantity)
            position.token_quantity = round(position.token_quantity - exited, 6)
            try:
                async with connect() as conn:
                    await insert_trade(conn, trade)
                    if position.token_quantity <= 0.000001:
                        await clear_open_position(conn, market.slug, position.token_id)
                    else:
                        await save_open_position(
                            conn, market_slug=market.slug, direction=position.direction,
                            token_id=position.token_id, fill_price=position.fill_price,
                            token_quantity=position.token_quantity, entry_time=position.entry_time,
                            strategy_id=position.strategy_id,
                        )
            except Exception:
                log.error("Exit %s filled but persistence failed", trade.order_id, exc_info=True)
            if position.token_quantity <= 0.000001:
                current.remove(position)
            self._paired_positions_by_slug[market.slug] = current
            self._risk.open_positions[:] = current
            self._position = current[0] if current else None
            if self._position:
                self._positions_by_slug[market.slug] = self._position
            else:
                self._positions_by_slug.pop(market.slug, None)
            log.info(
                "PAIR exit %s sold %.2f %s at $%.4f; net proceeds=$%.2f; hold EV=$%.2f; remaining=%.2f",
                market.slug, exited, weak, trade.fill_price,
                trade.amount_usd - trade.fee_usd,
                win_probability * (exited + position.token_quantity), position.token_quantity,
            )

    # ── Risk monitor loop ──────────────────────────────────────────────

    async def _risk_monitor_loop(self) -> None:
        """Periodically check if open positions need hedging."""
        while not self._stop.is_set():
            position, current = self._position, self._current_market
            runtime = self._market_states.get(current.slug) if current else None
            if (
                position and current and position.market.slug == current.slug and runtime
                and position.strategy_id != "paired_tilt_exit"
                and runtime.status == "ACTIVE" and current.is_active
                and self._market_inputs_fresh(runtime.market)
            ):
                async with self._order_lock:
                    position, current = self._position, self._current_market
                    runtime = self._market_states.get(current.slug) if current else None
                    if (
                        position and current and position.market.slug == current.slug and runtime
                        and position.strategy_id != "paired_tilt_exit"
                        and runtime.status == "ACTIVE" and current.is_active
                        and self._market_inputs_fresh(runtime.market)
                    ):
                        current = runtime.market
                        opposite_dir = "DOWN" if position.direction == "UP" else "UP"
                        opposite_token = current.token_id_for(opposite_dir)
                        opposite_price = self._polymarket.get_price(opposite_token)
                        if self._risk.should_hedge(
                            position,
                            self._binance.latest_price,
                            self._polymarket,
                            choppiness=self._regime.choppiness,
                            opposite_price=opposite_price,
                        ):
                            hedge = await self._executor.place_hedge(
                                current, position, estimated_price=opposite_price
                            )
                            if hedge:
                                position.hedge_count += 1
                                position.hedge_amount_usd += hedge.amount_usd
                                position.hedge_token_quantity += hedge.token_quantity
                                position.hedge_fill_price = hedge.fill_price
                                try:
                                    async with connect() as conn:
                                        await insert_trade(conn, hedge)
                                        await save_open_position(
                                            conn,
                                            market_slug=current.slug,
                                            direction=position.direction,
                                            token_id=position.token_id,
                                            fill_price=position.fill_price,
                                            token_quantity=position.token_quantity,
                                            entry_time=position.entry_time,
                                            hedge_count=position.hedge_count,
                                            hedge_amount_usd=position.hedge_amount_usd,
                                            hedge_token_quantity=position.hedge_token_quantity,
                                            hedge_fill_price=position.hedge_fill_price,
                                            strategy_id=position.strategy_id,
                                        )
                                except Exception:
                                    log.warning("Failed to persist hedge", exc_info=True)
                                log.info(
                                    "HEDGED %s — bought %s @ $%.3f ($%.2f)",
                                    current.slug, hedge.direction, hedge.fill_price, hedge.amount_usd,
                                )
            await _sleep_or_stop(self._stop, CONFIG.risk_check_interval_sec)

    # ── Unresolved sweep loop ────────────────────────────────────────

    async def _sweep_unresolved_loop(self) -> None:
        """Periodically resolve markets that the background task missed."""
        while not self._stop.is_set():
            await _sleep_or_stop(self._stop, 300)  # every 5 minutes
            try:
                async with connect() as conn:
                    # Find trades with no market_result yet, for markets that ended >5min ago
                    cutoff = int(time.time()) - 300
                    cur = await conn.execute(
                        """SELECT DISTINCT t.market_slug, m.condition_id
                           FROM trades t
                           JOIN markets m ON m.slug = t.market_slug
                           LEFT JOIN market_results mr ON mr.market_slug = m.slug
                           WHERE t.trade_type = 'ENTRY'
                             AND m.outcome IS NULL
                             AND m.end_ts < ?
                             AND mr.market_slug IS NULL""",
                        (cutoff,),
                    )
                    rows = await cur.fetchall()

                if not rows:
                    continue

                log.info("Sweep: found %d unresolved market(s)", len(rows))

                for row in rows:
                    slug, condition_id = row[0], row[1]
                    if slug in self._resolving_markets:
                        continue
                    outcome = await self._fetch_oracle_outcome(slug)
                    if not outcome:
                        continue

                    # Feed regime detector from historical prices
                    try:
                        async with connect() as conn2:
                            mkt_row = await get_market(conn2, slug)
                            if mkt_row and mkt_row.start_btc_price:
                                mid_ts = mkt_row.start_ts + 150
                                cur2 = await conn2.execute(
                                    "SELECT price FROM btc_prices WHERE ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT 1",
                                    (mid_ts - 15, mid_ts + 15),
                                )
                                mid_row = await cur2.fetchone()
                                if mid_row:
                                    first_half_dir = "UP" if mid_row[0] >= mkt_row.start_btc_price else "DOWN"
                                    self._regime.record(first_half_dir, outcome)
                    except Exception:
                        pass

                    async with connect() as conn:
                        market_trades = await trades_for_market(conn, slug)
                    entry_cost, hedge_cost, payout, net_pnl, outcome_correct = _summarize_market_result(
                        market_trades, outcome
                    )

                    log.info(
                        "Sweep resolved %s: %s%s — PnL=$%+.2f",
                        slug,
                        outcome,
                        " [HEDGED]" if hedge_cost > 0 else "",
                        net_pnl,
                    )

                    async with connect() as conn:
                        await set_market_outcome(conn, slug, outcome, 0.0)
                        await upsert_result(
                            conn, slug,
                            entry_cost=entry_cost,
                            hedge_cost=hedge_cost,
                            payout=payout,
                            net_pnl=net_pnl,
                            outcome_correct=outcome_correct,
                        )
                        await clear_open_position(conn, slug)

                    self._positions_by_slug.pop(slug, None)
                    self._paired_positions_by_slug.pop(slug, None)
                    self._risk.open_positions[:] = [
                        position for position in self._risk.open_positions if position.market.slug != slug
                    ]
                    if self._position and self._position.market.slug == slug:
                        self._position = None
                    runtime = self._market_states.get(slug)
                    if runtime:
                        if runtime.status not in {"EXPIRED", "RESOLVING", "RESOLVED"}:
                            self._transition(runtime, "EXPIRED")
                        if runtime.status == "EXPIRED":
                            self._transition(runtime, "RESOLVING")
                        if runtime.status == "RESOLVING":
                            self._transition(runtime, "RESOLVED")
                    log.info("[RESOLVED] Market %s settled by sweep; final PnL=$%+.2f", slug, net_pnl)

                    async with connect() as conn3:
                        today_ts = int(
                            datetime.datetime.combine(
                                datetime.date.today(), datetime.time.min
                            ).timestamp()
                        )
                        self._risk.daily_pnl = await pnl_since(conn3, today_ts)
                        self._risk.sync_streak(await trailing_loss_streak(conn3))

                    # Auto-redeem if we hold any winning shares (includes hedged
                    # markets where the hedge won).
                    if payout > 0 and not self._paper_mode and hasattr(self._executor, "redeem"):
                        asyncio.create_task(
                            self._redeem_and_mark(slug, condition_id),
                            name=f"redeem-sweep-{slug}",
                        )

            except Exception:
                log.warning("Sweep error", exc_info=True)

    # ── Shutdown ───────────────────────────────────────────────────────

    async def stop(self) -> None:
        self._stop.set()
        self._discovery_wakeup.set()
        await self._binance.stop()
        await self._chainlink.stop()
        await self._chainlink_onchain.stop()
        await self._polymarket.stop()


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep for `seconds`, but return immediately if `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
