"""Order execution via Polymarket's unified Python SDK (sync, wrapped for async)."""

from __future__ import annotations

import asyncio
import atexit
import datetime
import logging
import math
import threading
import time
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from .config import CONFIG
from .fees import taker_fee_usd, taker_fee_per_share
from .models import Market, OpenPosition, Signal, TradeRecord

log = logging.getLogger(__name__)

_NOT_REDEEMED_LOG = Path("notRedeemed.log")
_CLOB_CLIENT: Any = None
_CLOB_CLIENT_LOCK = threading.Lock()


def wallet_usdc_balance() -> float:
    """Return the authenticated Polymarket wallet's available USDC balance."""
    client = _build_clob_client()
    if client is None:
        raise RuntimeError("Polymarket wallet is not authenticated")
    return client.get_balance_allowance(asset_type="COLLATERAL").balance / 1_000_000


def _log_unredeemed(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _NOT_REDEEMED_LOG.open("a") as f:
        f.write(f"{ts}  {msg}\n")


def _is_fok_kill(exc: BaseException) -> bool:
    """True when an exception is a Polymarket 'FOK couldn't fill' rejection."""
    return "couldn't be fully filled" in str(exc) or "FOK orders are" in str(exc)


def _build_clob_client() -> Any:
    """Create the authenticated client, which resolves the Deposit Wallet."""
    global _CLOB_CLIENT
    if not CONFIG.private_key:
        return None
    if _CLOB_CLIENT is not None:
        return _CLOB_CLIENT
    with _CLOB_CLIENT_LOCK:
        if _CLOB_CLIENT is not None:
            return _CLOB_CLIENT
        try:
            from polymarket import SecureClient

            _CLOB_CLIENT = SecureClient.create(private_key=CONFIG.private_key)
            atexit.register(_CLOB_CLIENT.close)
            log.info("Authenticated Polymarket %s wallet", _CLOB_CLIENT.wallet_type)
            return _CLOB_CLIENT
        except Exception:
            log.error("Failed to build Polymarket client", exc_info=True)
            return None


class Executor:
    """Places orders on Polymarket's CLOB exchange."""

    def __init__(self) -> None:
        self._client = _build_clob_client()

    @property
    def is_ready(self) -> bool:
        return self._client is not None

    async def place_trade(
        self,
        market: Market,
        signal: Signal,
        amount_usd: float,
        shares: float | None = None,
    ) -> TradeRecord | None:
        """Place a buy order for the signal direction.

        Uses Fill-or-Kill to ensure instant execution. Falls back to an
        aggressive limit order if FOK fails. Fixed-share orders stay in Shares.
        """
        token_id = market.token_id_for(signal.direction)
        if shares is not None:
            target_shares = shares
        elif (
            signal.estimated_entry_cost_per_share is not None
            and math.isfinite(signal.estimated_entry_cost_per_share)
            and signal.estimated_entry_cost_per_share > 0
        ):
            target_shares = amount_usd / signal.estimated_entry_cost_per_share
        else:
            target_shares = 0.0
        target_usd = (
            shares * (signal.estimated_entry_cost_per_share or signal.poly_implied_prob)
            if shares is not None else amount_usd
        )
        if not self._client:
            log.error("CLOB client not initialised — cannot trade")
            self._log_order(
                "ENTRY", market, token_id, target_usd, target_shares,
                signal.poly_implied_prob, 0.0, 0.0, "REJECTED",
            )
            return None

        prepared = await asyncio.to_thread(
            self._prepare_buy, token_id, amount_usd, signal.poly_implied_prob, shares
        )
        if not prepared:
            self._log_order(
                "ENTRY", market, token_id, target_usd, target_shares,
                signal.poly_implied_prob, 0.0, 0.0, "REJECTED",
            )
            return None
        limit_price, order_shares, submitted_usd, min_order_size = prepared

        try:
            resp = await asyncio.to_thread(
                self._fok_buy, token_id, amount_usd, limit_price, order_shares, min_order_size
            )
            if resp:
                filled_size = float(resp["filled_size"])
                fill_price = float(resp["fill_price"])
                if not math.isfinite(fill_price) or not 0 < fill_price < 1:
                    log.warning("ENTRY rejected: CLOB returned invalid fill price")
                    self._log_order(
                        "ENTRY", market, token_id, target_usd, target_shares,
                        fill_price, 0.0, 0.0, "INVALID_FILL",
                    )
                    return None
                real_amount_usd = filled_size * fill_price
                trade = TradeRecord(
                    market_slug=market.slug,
                    trade_type="ENTRY",
                    direction=signal.direction,
                    token_id=token_id,
                    side="BUY",
                    amount_usd=real_amount_usd,
                    fill_price=fill_price,
                    token_quantity=filled_size,
                    signal_strength=signal.strength,
                    signal_edge=signal.edge,
                    signal_fair_prob=signal.fair_prob,
                    signal_net_edge=signal.net_edge,
                    fee_usd=resp.get("fee_usd", taker_fee_usd(fill_price, filled_size)),
                    order_id=resp["orderID"],
                )
                self._log_order(
                    "ENTRY", market, token_id, target_usd, target_shares,
                    fill_price, real_amount_usd, filled_size, "FILLED",
                )
                return trade
        except Exception as e:
            # FOK rejection (couldn't fully fill) is expected — log quietly
            # and fall back to a limit order. Anything else is real.
            if _is_fok_kill(e):
                log.info("FOK unfilled — falling back to limit order")
            else:
                log.warning("FOK order failed unexpectedly", exc_info=True)

        # Fallback: aggressive limit order. _limit_buy returns real fill info
        # (or None if nothing filled) so we never record phantom trades.
        try:
            resp = await asyncio.to_thread(
                self._limit_buy, token_id, amount_usd, limit_price, order_shares, min_order_size
            )
            if resp:
                filled_size = float(resp["filled_size"])
                fill_price = float(resp["fill_price"])
                if not math.isfinite(fill_price) or not 0 < fill_price < 1:
                    log.warning("ENTRY rejected: CLOB returned invalid fill price")
                    self._log_order(
                        "ENTRY", market, token_id, target_usd, target_shares,
                        fill_price, 0.0, 0.0, "INVALID_FILL",
                    )
                    return None
                real_amount_usd = filled_size * fill_price
                trade = TradeRecord(
                    market_slug=market.slug,
                    trade_type="ENTRY",
                    direction=signal.direction,
                    token_id=token_id,
                    side="BUY",
                    amount_usd=real_amount_usd,
                    fill_price=fill_price,
                    token_quantity=filled_size,
                    signal_strength=signal.strength,
                    signal_edge=signal.edge,
                    signal_fair_prob=signal.fair_prob,
                    signal_net_edge=signal.net_edge,
                    fee_usd=resp.get("fee_usd", taker_fee_usd(fill_price, filled_size)),
                    order_id=resp["orderID"],
                )
                self._log_order(
                    "ENTRY", market, token_id, target_usd, target_shares,
                    fill_price, real_amount_usd, filled_size, "FILLED",
                )
                return trade
        except Exception:
            log.error("Limit order also failed", exc_info=True)

        limit_shares = (
            shares if shares is not None
            else self._round_down(amount_usd / limit_price, 0.01)
        )
        if limit_shares < min_order_size:
            self._log_order(
                "ENTRY", market, token_id, target_usd, target_shares,
                limit_price, 0.0, 0.0, "REJECTED",
            )
            return None
        self._log_order(
            "ENTRY", market, token_id, target_usd, target_shares,
            limit_price,
            submitted_usd if shares is not None else limit_shares * limit_price,
            limit_shares,
            "UNFILLED",
        )
        return None

    async def place_hedge(
        self,
        market: Market,
        position: OpenPosition,
        estimated_price: float | None = None,
    ) -> TradeRecord | None:
        """Buy the opposite side to hedge a losing position.

        Tries FOK first for instant fill; if it can't be fully filled, falls
        back to an aggressive GTC limit at ``est_price + limit_slippage``.
        Only returns a TradeRecord if real shares were acquired.
        """
        opposite_dir = "DOWN" if position.direction == "UP" else "UP"
        opposite_token = market.token_id_for(opposite_dir)
        est_price = estimated_price if estimated_price and estimated_price > 0 else 0.50
        target_cost = position.token_quantity * est_price
        hedge_cost = min(target_cost, CONFIG.max_hedge_usd)
        if not self._client:
            log.error("CLOB client not initialised — cannot hedge")
            self._log_order(
                "HEDGE", market, opposite_token, target_cost,
                position.token_quantity, est_price, 0.0, 0.0, "REJECTED",
            )
            return None
        prepared = await asyncio.to_thread(
            self._prepare_buy, opposite_token, hedge_cost, est_price, None
        )
        if not prepared:
            self._log_order(
                "HEDGE", market, opposite_token, target_cost,
                position.token_quantity, est_price, 0.0, 0.0, "REJECTED",
            )
            return None
        limit_price, _, _, min_order_size = prepared

        def _build_hedge_record(resp: dict) -> TradeRecord | None:
            filled_size = float(resp["filled_size"])
            fill_price = float(resp["fill_price"])
            if not math.isfinite(fill_price) or not 0 < fill_price < 1:
                log.warning("HEDGE rejected: CLOB returned invalid fill price")
                self._log_order(
                    "HEDGE", market, opposite_token, target_cost,
                    position.token_quantity, fill_price, 0.0, 0.0, "INVALID_FILL",
                )
                return None
            record = TradeRecord(
                market_slug=market.slug,
                trade_type="HEDGE",
                direction=opposite_dir,
                token_id=opposite_token,
                side="BUY",
                amount_usd=filled_size * fill_price,
                fill_price=fill_price,
                token_quantity=filled_size,
                signal_strength=0.0,
                signal_edge=0.0,
                signal_net_edge=0.0,
                fee_usd=resp.get("fee_usd", taker_fee_usd(fill_price, filled_size)),
                order_id=resp["orderID"],
            )
            self._log_order(
                "HEDGE", market, opposite_token, target_cost,
                position.token_quantity, fill_price, record.amount_usd,
                filled_size, "FILLED",
            )
            return record

        try:
            resp = await asyncio.to_thread(
                self._fok_buy, opposite_token, hedge_cost, limit_price, None, min_order_size
            )
            if resp:
                return _build_hedge_record(resp)
        except Exception as e:
            if _is_fok_kill(e):
                log.info("Hedge FOK unfilled — falling back to limit order")
            else:
                log.warning("Hedge FOK failed unexpectedly", exc_info=True)

        # Fallback: aggressive limit order so the position doesn't stay
        # unhedged on a thin book.
        try:
            resp = await asyncio.to_thread(
                self._limit_buy, opposite_token, hedge_cost, limit_price, None, min_order_size
            )
            if resp:
                return _build_hedge_record(resp)
        except Exception:
            log.error("Hedge limit order also failed", exc_info=True)

        log.warning("Hedge failed for %s — position remains unprotected", market.slug)
        limit_shares = self._round_down(hedge_cost / limit_price, 0.01)
        if limit_shares < min_order_size:
            self._log_order(
                "HEDGE", market, opposite_token, target_cost, position.token_quantity,
                limit_price, 0.0, 0.0, "REJECTED",
            )
            return None
        self._log_order(
            "HEDGE", market, opposite_token, target_cost, position.token_quantity,
            limit_price, limit_shares * limit_price, limit_shares, "UNFILLED",
        )
        return None

    async def place_exit(
        self, market: Market, position: OpenPosition, shares: float, limit_price: float,
        signal: Signal,
    ) -> TradeRecord | None:
        """Sell at a protected minimum price and record only queried fills."""
        if not self._client or not math.isfinite(shares) or shares <= 0:
            return None
        prepared = await asyncio.to_thread(self._prepare_sell, position.token_id, shares, limit_price)
        if not prepared:
            return None
        price, quantity, minimum = prepared
        try:
            fill = await asyncio.to_thread(self._fok_sell, position.token_id, price, quantity, minimum)
        except Exception:
            log.warning("Exit FOK failed for %s", market.slug, exc_info=True)
            return None
        if not fill:
            return None
        qty, fill_price, order_id = fill["filled_size"], fill["fill_price"], fill["orderID"]
        if not math.isfinite(qty) or qty <= 0 or qty > position.token_quantity + 1e-6:
            log.error("Exit rejected: invalid matched share count %.4f", qty)
            return None
        if not math.isfinite(fill_price) or not 0 < fill_price < 1:
            log.error("Exit rejected: invalid matched price %.4f", fill_price)
            return None
        return TradeRecord(
            market_slug=market.slug, trade_type="EXIT", direction=position.direction,
            token_id=position.token_id, side="SELL", amount_usd=qty * fill_price,
            fill_price=fill_price, token_quantity=qty, signal_strength=signal.strength,
            signal_edge=signal.edge, signal_fair_prob=signal.fair_prob,
            signal_net_edge=signal.net_edge,
            fee_usd=fill.get("fee_usd", taker_fee_usd(fill_price, qty)),
            order_id=order_id,
        )

    def _prepare_sell(self, token_id: str, shares: float, minimum_price: float):
        if not math.isfinite(shares) or shares <= 0 or not math.isfinite(minimum_price):
            return None
        try:
            book = self._client.get_order_book(token_id=token_id)
            tick = Decimal(str(self._field(book, "tick_size")))
            minimum = float(self._field(book, "min_order_size"))
            price = float((Decimal(str(minimum_price)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick)
            balance = self._client.get_balance_allowance(
                asset_type="CONDITIONAL", token_id=token_id
            )
            available = float(self._field(balance, "balance")) / 1_000_000
        except Exception:
            log.warning("Exit rejected: unable to verify token balance and market minimum", exc_info=True)
            return None
        quantity = self._round_down(min(shares, available), 0.01)
        if not math.isfinite(minimum) or minimum <= 0 or quantity < minimum or not 0 < price < 1:
            return None
        return price, quantity, minimum

    def _fok_sell(self, token_id: str, price: float, shares: float, minimum: float):
        if shares < minimum:
            return None
        signed = self._client.create_market_order(
            token_id=token_id,
            side="SELL",
            shares=shares,
            min_price=price,
            order_type="FOK",
        )
        response = self._client.post_order(signed)
        order_id = self._posted_order_id(response)
        if not order_id:
            return None
        filled, fill_price, _, fee_usd = self._query_fill(order_id)
        if not math.isfinite(filled) or filled <= 0 or not math.isfinite(fill_price):
            return None
        return {
            "orderID": order_id, "filled_size": filled,
            "fill_price": fill_price, "fee_usd": fee_usd,
        }

    async def redeem(self, condition_id: str) -> str | None:
        """Redeem winning conditional tokens for USDC on-chain.

        Retries with backoff since the on-chain oracle may not have resolved
        the market yet when the bot detects the outcome.
        """
        delays = [120, 150, 180, 120, 150, 180, 120, 150, 180]
        for delay in delays:
            await asyncio.sleep(delay)
            try:
                tx_hash = await asyncio.to_thread(self._redeem_sync, condition_id)
                log.info("Redeemed %s — tx: %s", condition_id[:18], tx_hash)
                return tx_hash
            except Exception as e:
                if "not received yet" in str(e):
                    log.info("Oracle not ready for %s — retrying in %ds", condition_id[:18], delay)
                else:
                    log.warning("Redemption failed for %s", condition_id[:18], exc_info=True)
                    return None
        msg = f"{condition_id} — gave up after all retries"
        log.warning("Redemption gave up for %s after all retries", condition_id[:18])
        _log_unredeemed(msg)
        return None

    def _redeem_sync(self, condition_id: str) -> str:
        """Redeem through the SDK for the authenticated Deposit Wallet."""
        if self._client is None:
            raise RuntimeError("Polymarket wallet is not authenticated")
        outcome = self._client.redeem_positions(condition_id=condition_id).wait()
        return str(outcome.transaction_hash)

    @staticmethod
    def _field(value: Any, name: str) -> Any:
        return value.get(name) if isinstance(value, dict) else getattr(value, name, None)

    @classmethod
    def _posted_order_id(cls, response: Any) -> str | None:
        if response is None or cls._field(response, "ok") is False:
            return None
        return cls._field(response, "order_id") or cls._field(response, "orderID")

    @staticmethod
    def _round_down(value: float, step: float) -> float:
        return float(
            (Decimal(str(value)) / Decimal(str(step))).to_integral_value(
                rounding=ROUND_DOWN
            ) * Decimal(str(step))
        )

    def _prepare_buy(
        self,
        token_id: str,
        amount_usd: float,
        reference_price: float,
        shares: float | None = None,
    ) -> tuple[float, float, float, float] | None:
        """Validate price, CLOB minimum/precision, and available collateral."""
        if (
            not math.isfinite(amount_usd)
            or amount_usd <= 0
            or not math.isfinite(reference_price)
            or not 0 < reference_price < 1
            or (shares is not None and (not math.isfinite(shares) or shares <= 0))
        ):
            log.warning("Order rejected: invalid price, amount, or shares")
            return None

        try:
            book = self._client.get_order_book(token_id=token_id)
            tick_size = Decimal(str(self._field(book, "tick_size")))
            min_order_size = float(self._field(book, "min_order_size"))
            raw_price = min(reference_price + CONFIG.limit_slippage, 0.99)
            limit_price = float(
                (Decimal(str(raw_price)) / tick_size).to_integral_value(
                    rounding=ROUND_DOWN
                ) * tick_size
            )
        except Exception:
            log.warning("Order rejected: could not read market tick/minimum")
            return None

        if (
            not math.isfinite(min_order_size)
            or min_order_size <= 0
            or not math.isfinite(limit_price)
            or not 0 < limit_price < 1
        ):
            log.warning("Order rejected: invalid market minimum or rounded price")
            return None

        requested_shares = (
            shares
            if shares is not None
            else amount_usd / (limit_price + taker_fee_per_share(limit_price))
        )
        rounded_shares = self._round_down(requested_shares, 0.01)
        if rounded_shares < min_order_size:
            log.warning(
                "Order rejected: %.2f shares below market minimum %.2f",
                rounded_shares,
                min_order_size,
            )
            return None

        submitted_usd = rounded_shares * limit_price + taker_fee_usd(limit_price, rounded_shares)
        try:
            balance_data = self._client.get_balance_allowance(
                asset_type="COLLATERAL"
            )
            available_usd = float(self._field(balance_data, "balance")) / 1_000_000
        except Exception:
            log.warning("Order rejected: could not verify available USDC balance")
            return None
        if not math.isfinite(available_usd) or available_usd < submitted_usd:
            log.warning(
                "Order rejected: insufficient USDC (available $%.2f, required $%.2f)",
                available_usd,
                submitted_usd,
            )
            return None

        return limit_price, rounded_shares, submitted_usd, min_order_size

    @staticmethod
    def _log_order(
        trade_type: str,
        market: Market,
        token_id: str,
        target_usd: float,
        target_shares: float,
        price: float,
        submitted_usd: float,
        submitted_shares: float,
        result: str,
    ) -> None:
        log.info(
            "%s order market=%s token=%s price=$%.4f target_usd=$%.2f "
            "target_shares=%.2f submitted_usd=$%.2f submitted_shares=%.2f "
            "position_mode=%s result=%s",
            trade_type,
            market.slug,
            token_id,
            price,
            target_usd,
            target_shares,
            submitted_usd,
            submitted_shares,
            CONFIG.position_mode.upper(),
            result,
        )

    def _fok_buy(
        self,
        token_id: str,
        amount_usd: float,
        limit_price: float,
        shares: float | None = None,
        min_order_size: float = 0.0,
    ) -> dict | None:
        """Synchronous FOK buy (runs in thread).

        Uses a FOK limit order with a share size, or a FOK market order without one.
        Queries ``get_order`` for the authoritative fill.
        """
        if shares is None:
            market_price = self._client.estimate_market_price(
                token_id=token_id,
                side="BUY",
                amount=amount_usd,
                order_type="FOK",
            )
            if not math.isfinite(market_price) or not 0 < market_price < 1:
                log.warning("FOK rejected: invalid market price")
                return None
            if self._round_down(amount_usd / market_price, 0.01) < min_order_size:
                log.warning("FOK rejected: order would be below market minimum")
                return None
            signed = self._client.create_market_order(
                token_id=token_id,
                side="BUY",
                amount=amount_usd,
                max_price=limit_price,
                order_type="FOK",
            )
        else:
            signed = self._client.create_limit_order(
                token_id=token_id, price=limit_price, size=shares, side="BUY"
            )
            # shortcut: SDK 0.18 omits limit-order FOK; replace when its API exposes it.
            signed = replace(signed, order_type="FOK")
        resp = self._client.post_order(signed)
        order_id = self._posted_order_id(resp)
        if not order_id:
            return None

        filled, real_price, status, fee_usd = self._query_fill(order_id)
        if not math.isfinite(filled) or filled <= 0:
            # FOK succeeded per the API but get_order reported no fill —
            # treat as no trade to avoid recording a phantom.
            log.warning("FOK %s reported success but size_matched=0", order_id[:20])
            return None
        if not math.isfinite(real_price) or not 0 < real_price < 1:
            log.warning("FOK %s reported an invalid fill price", order_id[:20])
            return None

        return {
            "orderID": order_id,
            "filled_size": filled,
            "fill_price": real_price,
            "fee_usd": fee_usd,
            "status": status,
        }

    def _query_fill(self, order_id: str) -> tuple[float, float, str, float]:
        """Return matched shares, execution VWAP, and fees from the match records."""
        order = None
        for attempt in range(5):
            try:
                order = self._client.get_order(order_id=order_id)
                if not order:
                    return 0.0, 0.0, "UNKNOWN", 0.0
                actual_qty = actual_value = 0.0
                actual_fee = 0.0
                associated = self._field(order, "associate_trades") or []
                if associated:
                    for trade_id in associated:
                        trades = self._client.list_account_trades(id=trade_id).first_page().items
                        for trade in trades:
                            if self._field(trade, "taker_order_id") == order_id:
                                qty = self._number(self._field(trade, "size"))
                                price = self._number(self._field(trade, "price"))
                                if qty and price and 0 < price < 1:
                                    actual_qty += qty
                                    actual_value += qty * price
                                    rate_bps = self._number(self._field(trade, "fee_rate_bps"))
                                    actual_fee += taker_fee_usd(
                                        price, qty,
                                        0.07 if rate_bps is None else rate_bps / 10_000,
                                    )
                            else:
                                # Matching as a maker incurs no trading fee.
                                maker_orders = self._field(trade, "maker_orders") or []
                                for maker in maker_orders:
                                    if self._field(maker, "order_id") != order_id:
                                        continue
                                    qty = self._number(self._field(maker, "matched_amount"))
                                    price = self._number(self._field(maker, "price"))
                                    if qty and price and 0 < price < 1:
                                        actual_qty += qty
                                        actual_value += qty * price
                matched = self._number(self._field(order, "size_matched")) or 0.0
                if matched > 0 and abs(actual_qty - matched) <= 1e-4:
                    return (
                        matched, actual_value / actual_qty,
                        str(self._field(order, "status") or "UNKNOWN").upper(),
                        round(actual_fee, 5),
                    )
            except Exception:
                log.warning("Could not read actual fills for %s", order_id, exc_info=True)
            if attempt < 4:
                time.sleep(0.2)
        if not order:
            return 0.0, 0.0, "UNKNOWN", 0.0
        matched = self._number(self._field(order, "size_matched")) or 0.0
        limit_price = self._number(self._field(order, "price")) or 0.0
        if matched > 0:
            log.error(
                "Complete trade data unavailable for %s; using protected limit and conservative fee estimate",
                order_id,
            )
            # shortcut: order data can precede the trade index; use the protected limit after five retries.
            return (
                matched, limit_price,
                str(self._field(order, "status") or "UNKNOWN").upper(),
                taker_fee_usd(limit_price, matched),
            )
        return 0.0, 0.0, str(self._field(order, "status") or "UNKNOWN").upper(), 0.0

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    def _limit_buy(
        self,
        token_id: str,
        amount_usd: float,
        price: float,
        shares: float | None = None,
        min_order_size: float = 0.0,
    ) -> dict | None:
        """Synchronous aggressive limit buy (runs in thread).

        Posts a GTC limit order, then polls ``get_order`` to read the real
        ``size_matched``. If the order partially filled, the remainder is
        canceled. If nothing filled, the order is canceled and ``None`` is
        returned so the caller does not record a phantom trade.

        Returns a dict with keys ``orderID``, ``filled_size``, ``fill_price``,
        and ``status`` reflecting the real on-exchange state.
        """
        if price <= 0:
            return None
        size = shares if shares is not None else self._round_down(amount_usd / price, 0.01)
        if size <= 0 or size < min_order_size:
            log.warning("Limit order rejected: %.2f shares below market minimum %.2f", size, min_order_size)
            return None

        signed = self._client.create_limit_order(
            token_id=token_id, price=price, size=size, side="BUY"
        )
        resp = self._client.post_order(signed)
        order_id = self._posted_order_id(resp)
        if not order_id:
            return None

        filled = 0.0
        fee_usd = 0.0
        real_price = price
        status = "LIVE"
        for _ in range(3):
            time.sleep(1.0)
            filled, queried_price, status, fee_usd = self._query_fill(order_id)
            if queried_price > 0:
                real_price = queried_price
            if status == "MATCHED" or filled > 0:
                break

        if not math.isfinite(filled) or filled <= 0:
            try:
                self._client.cancel_order(order_id=order_id)
            except Exception:
                log.warning("Failed to cancel unfilled order %s", order_id, exc_info=True)
            log.info("Limit order %s did not fill — canceled", order_id[:20])
            return None

        if status != "MATCHED":
            try:
                self._client.cancel_order(order_id=order_id)
            except Exception:
                log.warning("Failed to cancel partial order %s", order_id, exc_info=True)
            log.info("Limit order %s partially filled (%.4f/%.4f)", order_id[:20], filled, size)

        return {
            "orderID": order_id,
            "filled_size": filled,
            "fill_price": real_price,
            "fee_usd": fee_usd,
            "status": status,
        }
