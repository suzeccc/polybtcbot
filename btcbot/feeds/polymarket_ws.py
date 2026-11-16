"""Polymarket CLOB market stream with per-token book and freshness state."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import websockets

from ..config import CONFIG

log = logging.getLogger(__name__)
PING_INTERVAL_SEC = 10
OnPolyPrice = Callable[[str, float], Awaitable[None]]


@dataclass
class OrderBook:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    best_bid_hint: float | None = None
    best_ask_hint: float | None = None
    received_at: float = 0.0
    initialized: bool = False

    @property
    def best_bid(self) -> float | None:
        return self.best_bid_hint if self.best_bid_hint is not None else max(self.bids, default=None)

    @property
    def best_ask(self) -> float | None:
        return self.best_ask_hint if self.best_ask_hint is not None else min(self.asks, default=None)

    @property
    def midpoint(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None or not 0 < bid <= ask < 1:
            return None
        return (bid + ask) / 2


class PolymarketFeed:
    """One persistent market-channel connection for current and prepared tokens."""

    def __init__(self, on_price: OnPolyPrice | None = None) -> None:
        self._on_price = on_price
        self._desired_tokens: set[str] = set()
        self._current_tokens: set[str] = set()
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._books: dict[str, OrderBook] = {}
        self._prices: dict[str, float] = {}
        self._subscription_sent_at: dict[str, float] = {}
        self._last_stale_reconnect = 0.0

    def get_price(self, token_id: str) -> float | None:
        return self._prices.get(token_id)

    def get_book(self, token_id: str) -> OrderBook | None:
        return self._books.get(token_id)

    def book_age(self, token_id: str, now: float | None = None) -> float | None:
        book = self._books.get(token_id)
        if not book or not book.initialized:
            return None
        return (time.monotonic() if now is None else now) - book.received_at

    def has_fresh_book(self, token_id: str, max_age: float, now: float | None = None) -> bool:
        age = self.book_age(token_id, now)
        book = self._books.get(token_id)
        return bool(
            book and book.initialized and book.midpoint is not None
            and age is not None and 0 <= age <= max_age
        )

    async def reconnect_if_stale(self, token_ids: set[str], max_age: float) -> None:
        async with self._lock:
            now = time.monotonic()
            if (
                not self._ws
                or now - self._last_stale_reconnect < max_age
                or all(self.has_fresh_book(token, max_age, now) for token in token_ids)
            ):
                return
            self._last_stale_reconnect = now
            log.warning("CLOB active-market book is stale; reconnecting")
            try:
                await self._ws.close()
            except Exception:
                log.warning("Failed to reconnect stale CLOB feed", exc_info=True)

    async def retry_stale_subscriptions(self, token_ids: set[str], max_age: float) -> None:
        """Re-request prepared books that have not arrived or have gone stale."""
        async with self._lock:
            if not self._ws:
                return
            now = time.monotonic()
            retry = {
                token for token in token_ids & self._current_tokens
                if not self.has_fresh_book(token, max_age, now)
                and now - self._subscription_sent_at.get(token, 0.0) >= max_age
            }
            if not retry:
                return
            for token in retry:
                self._subscription_sent_at[token] = now
            try:
                await self._ws.send(json.dumps({
                    "assets_ids": sorted(retry),
                    "operation": "subscribe",
                    "custom_feature_enabled": True,
                }))
                log.warning("Retrying CLOB subscription for %d unready token(s)", len(retry))
            except Exception:
                log.warning("CLOB prepared-book retry failed", exc_info=True)

    async def set_token_ids(self, tokens: set[str]) -> None:
        tokens = {str(token) for token in tokens if token}
        async with self._lock:
            removed_from_desired = self._desired_tokens - tokens
            self._desired_tokens = tokens
            for token in removed_from_desired:
                self._books.pop(token, None)
                self._prices.pop(token, None)
                self._subscription_sent_at.pop(token, None)
            if not self._ws:
                return
            added = tokens - self._current_tokens
            removed = self._current_tokens - tokens
            if not added and not removed:
                return
            try:
                if added:
                    now = time.monotonic()
                    retry = {
                        token for token in added
                        if now - self._subscription_sent_at.get(token, 0.0)
                        >= CONFIG.market_data_max_age_sec
                    }
                    if retry:
                        for token in retry:
                            self._subscription_sent_at[token] = now
                        await self._ws.send(json.dumps({
                            "assets_ids": sorted(retry),
                            "operation": "subscribe",
                            "custom_feature_enabled": True,
                        }))
                        self._current_tokens.update(retry)
                if removed:
                    await self._ws.send(json.dumps({
                        "assets_ids": sorted(removed),
                        "operation": "unsubscribe",
                    }))
                    self._current_tokens.difference_update(removed)
                log.info("CLOB subscriptions updated: +%d -%d", len(added), len(removed))
            except Exception:
                log.warning("CLOB dynamic subscription failed; will retry", exc_info=True)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws:
            await self._ws.close()

    async def run(self) -> None:
        """Connect, subscribe, receive snapshots and updates, then reconnect."""
        backoff = 1.0
        while not self._stop.is_set():
            if not self._desired_tokens:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                log.info("Connecting to Polymarket WS")
                async with websockets.connect(
                    CONFIG.clob_ws_url,
                    ping_interval=None,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._clear_books()
                    async with self._lock:
                        self._current_tokens = set(self._desired_tokens)
                        await ws.send(json.dumps({
                            "type": "market",
                            "assets_ids": sorted(self._current_tokens),
                            "custom_feature_enabled": True,
                        }))
                        sent_at = time.monotonic()
                        self._subscription_sent_at = {
                            token: sent_at for token in self._current_tokens
                        }
                    backoff = 1.0
                    log.info("CLOB subscription sent for %d tokens", len(self._current_tokens))

                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        while not self._stop.is_set():
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=PING_INTERVAL_SEC + 5
                            )
                            if self._stop.is_set():
                                break
                            await self._handle_message(raw)
                    finally:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
            except websockets.ConnectionClosed:
                log.warning("CLOB WS disconnected; reconnecting in %.0fs", backoff)
            except asyncio.TimeoutError:
                log.warning("CLOB WS heartbeat timed out; reconnecting in %.0fs", backoff)
            except Exception:
                log.warning("CLOB WS error; reconnecting in %.0fs", backoff, exc_info=True)
            finally:
                self._ws = None
                self._current_tokens.clear()
                self._clear_books()
            if not self._stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _clear_books(self) -> None:
        self._books.clear()
        self._prices.clear()
        self._subscription_sent_at.clear()

    async def _ping_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SEC)
                await ws.send("PING")
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass
        except Exception:
            log.warning("CLOB WS heartbeat failed; reconnecting", exc_info=True)
            await ws.close()

    async def _handle_message(self, raw: str | bytes) -> None:
        if raw in {"PONG", b"PONG"}:
            return
        try:
            decoded = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if decoded == "PONG":
            return
        events = decoded if isinstance(decoded, list) else [decoded]
        for event in events:
            if not isinstance(event, dict):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
            event_type = str(event.get("event_type") or event.get("type") or "").lower()
            if event_type == "book":
                token_id = payload.get("asset_id") or payload.get("assetId")
                if token_id:
                    self._set_snapshot(str(token_id), payload)
                    await self._publish_price(str(token_id))
            elif event_type == "price_change":
                changes = payload.get("price_changes") or payload.get("priceChanges")
                if isinstance(changes, list):
                    for change in changes:
                        if isinstance(change, dict):
                            token_id = change.get("asset_id") or change.get("assetId")
                            if token_id:
                                self._apply_change(str(token_id), change)
                                await self._publish_price(str(token_id))
                else:
                    token_id = payload.get("asset_id") or payload.get("assetId")
                    if token_id:
                        self._apply_change(str(token_id), payload)
                        await self._publish_price(str(token_id))
            elif event_type == "best_bid_ask":
                token_id = payload.get("asset_id") or payload.get("assetId")
                if token_id:
                    self._apply_change(str(token_id), payload)
                    await self._publish_price(str(token_id))

    def _set_snapshot(self, token_id: str, event: dict) -> None:
        book = OrderBook()
        for side, levels in ((book.bids, event.get("bids")), (book.asks, event.get("asks"))):
            if not isinstance(levels, list):
                continue
            for level in levels:
                if not isinstance(level, dict):
                    continue
                price, size = self._number(level.get("price")), self._number(level.get("size"))
                if price is not None and size is not None and 0 < price < 1 and size > 0:
                    side[price] = size
        book.received_at = time.monotonic()
        book.initialized = True
        self._books[token_id] = book

    def _apply_change(self, token_id: str, change: dict) -> None:
        book = self._books.get(token_id)
        if not book or not book.initialized:
            return
        price, size = self._number(change.get("price")), self._number(change.get("size"))
        side = str(change.get("side") or "").upper()
        if price is not None and size is not None and 0 < price < 1 and side in {"BUY", "SELL"}:
            levels = book.bids if side == "BUY" else book.asks
            if size <= 0:
                levels.pop(price, None)
            else:
                levels[price] = size
        if "best_bid" in change or "bestBid" in change:
            book.best_bid_hint = self._quote(change.get("best_bid", change.get("bestBid")))
        if "best_ask" in change or "bestAsk" in change:
            book.best_ask_hint = self._quote(change.get("best_ask", change.get("bestAsk")))
        book.received_at = time.monotonic()

    async def _publish_price(self, token_id: str) -> None:
        book = self._books.get(token_id)
        price = book.midpoint if book else None
        if price is None:
            self._prices.pop(token_id, None)
            return
        self._prices[token_id] = price
        if self._on_price:
            await self._on_price(token_id, price)

    @staticmethod
    def _number(value: object) -> float | None:
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _quote(cls, value: object) -> float | None:
        quote = cls._number(value)
        return quote if quote is not None and 0 < quote < 1 else None
