"""Polymarket RTDS Chainlink BTC/USD price stream."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable

import websockets

log = logging.getLogger(__name__)

_URL = "wss://ws-live-data.polymarket.com"
_PING_INTERVAL = 5.0
_RECV_TIMEOUT = 30.0
OnPrice = Callable[[float, float], Awaitable[None]]


class PolymarketChainlinkFeed:
    """Receive Polymarket's live Chainlink observations over RTDS."""

    def __init__(self, on_price: OnPrice | None = None) -> None:
        self._on_price = on_price
        self._stop = asyncio.Event()
        self._ws = None
        self.latest_price = 0.0
        self.latest_ts = 0.0

    async def stop(self) -> None:
        self._stop.set()
        if self._ws:
            await self._ws.close()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    _URL, ping_interval=None, close_timeout=5,
                ) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": '{"symbol":"btc/usd"}',
                        }],
                    }))
                    backoff = 1.0
                    log.info("Subscribed to Polymarket RTDS Chainlink BTC/USD")
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        while not self._stop.is_set():
                            raw = await asyncio.wait_for(ws.recv(), timeout=_RECV_TIMEOUT)
                            await self._handle_message(raw)
                    finally:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
            except asyncio.TimeoutError:
                log.warning("Polymarket RTDS Chainlink stream timed out")
            except websockets.ConnectionClosed:
                log.warning("Polymarket RTDS Chainlink disconnected")
            except Exception:
                log.warning("Polymarket RTDS Chainlink error", exc_info=True)
            finally:
                self._ws = None
            if not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)

    async def _ping_loop(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(_PING_INTERVAL)
                await ws.send("ping")
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass
        except Exception:
            log.warning("Polymarket RTDS Chainlink heartbeat failed", exc_info=True)
            await ws.close()

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            decoded = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        events = decoded if isinstance(decoded, list) else [decoded]
        for event in events:
            if (
                not isinstance(event, dict)
                or event.get("topic") != "crypto_prices_chainlink"
                or event.get("type") != "update"
            ):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or str(payload.get("symbol", "")).lower() != "btc/usd":
                continue
            try:
                price = float(payload["value"])
                timestamp = float(payload["timestamp"]) / 1000
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(price) or not math.isfinite(timestamp) or price <= 0 or timestamp <= 0:
                continue
            self.latest_price = price
            self.latest_ts = timestamp
            if self._on_price:
                await self._on_price(price, timestamp)
