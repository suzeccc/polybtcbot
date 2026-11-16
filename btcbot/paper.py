"""Paper-trading executor — simulates fills using real Polymarket prices."""

from __future__ import annotations

import logging
import math
import time
import uuid

from .config import CONFIG
from .fees import taker_fee_usd, taker_fee_per_share
from .models import Market, OpenPosition, Signal, TradeRecord

log = logging.getLogger(__name__)

# Simulated spread: we assume we'd cross the spread
SIMULATED_SPREAD = 0.02
# shortcut: Paper uses the common 5-share minimum without a live book; update if BTC market minima vary.
PAPER_MIN_ORDER_SHARES = 5.0


class PaperExecutor:
    """Drop-in replacement for Executor that logs virtual trades."""

    @staticmethod
    def _log_order(
        trade_type: str,
        market: Market,
        token_id: str,
        price: float,
        target_usd: float,
        target_shares: float,
        submitted_usd: float,
        submitted_shares: float,
        result: str,
    ) -> None:
        log.info(
            "[PAPER] %s order market=%s token=%s price=$%.4f target_usd=$%.2f "
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

    @property
    def is_ready(self) -> bool:
        return True

    async def place_trade(
        self,
        market: Market,
        signal: Signal,
        amount_usd: float,
        shares: float | None = None,
    ) -> TradeRecord | None:
        """Simulate a fill at the current market price + half spread."""
        token_id = market.token_id_for(signal.direction)
        fill_price = signal.poly_implied_prob + SIMULATED_SPREAD / 2
        fill_price = min(fill_price, 0.99)
        if not math.isfinite(fill_price) or not 0 < fill_price < 1:
            log.warning("[PAPER] ENTRY rejected: invalid price %.4f", fill_price)
            self._log_order("ENTRY", market, token_id, fill_price, amount_usd, shares or 0, 0, 0, "REJECTED")
            return None
        if (
            not math.isfinite(amount_usd)
            or amount_usd <= 0
            or (shares is not None and (not math.isfinite(shares) or shares <= 0))
        ):
            log.warning("[PAPER] ENTRY rejected: invalid amount or shares")
            self._log_order("ENTRY", market, token_id, fill_price, amount_usd, shares or 0, 0, 0, "REJECTED")
            return None
        qty = (
            shares
            if shares is not None
            else amount_usd / (fill_price + taker_fee_per_share(fill_price))
        )
        qty = math.floor(qty * 100) / 100
        if not math.isfinite(qty) or qty < PAPER_MIN_ORDER_SHARES:
            log.warning(
                "[PAPER] ENTRY rejected: %.2f shares below %.2f-share minimum",
                qty,
                PAPER_MIN_ORDER_SHARES,
            )
            self._log_order(
                "ENTRY", market, token_id, fill_price, amount_usd,
                shares if shares is not None else qty, 0, 0, "REJECTED",
            )
            return None
        actual_amount_usd = round(qty * fill_price, 6)

        self._log_order(
            "ENTRY", market, token_id, fill_price,
            amount_usd if shares is None else shares * signal.poly_implied_prob,
            shares if shares is not None else amount_usd / fill_price,
            actual_amount_usd, qty, "FILLED",
        )

        return TradeRecord(
            market_slug=market.slug,
            trade_type="ENTRY",
            direction=signal.direction,
            token_id=token_id,
            side="BUY",
            amount_usd=actual_amount_usd,
            fill_price=fill_price,
            token_quantity=qty,
            signal_strength=signal.strength,
            signal_edge=signal.edge,
            signal_fair_prob=signal.fair_prob,
            signal_net_edge=signal.net_edge,
            fee_usd=taker_fee_usd(fill_price, qty),
            order_id=f"paper-{uuid.uuid4().hex[:8]}",
            is_paper=True,
        )

    async def place_hedge(
        self,
        market: Market,
        position: OpenPosition,
        estimated_price: float | None = None,
    ) -> TradeRecord | None:
        """Simulate a hedge by buying the opposite side."""
        opposite_dir = "DOWN" if position.direction == "UP" else "UP"
        opposite_token = market.token_id_for(opposite_dir)
        est_price = estimated_price if estimated_price and estimated_price > 0 else 0.50
        if not math.isfinite(position.token_quantity) or position.token_quantity <= 0:
            log.warning("[PAPER] HEDGE rejected: invalid position shares")
            self._log_order(
                "HEDGE", market, opposite_token, est_price, 0, 0, 0, 0, "REJECTED"
            )
            return None
        fill_price = min(est_price + SIMULATED_SPREAD / 2, 0.99)
        if not math.isfinite(fill_price) or not 0 < fill_price < 1:
            log.warning("[PAPER] HEDGE rejected: invalid price %.4f", fill_price)
            self._log_order(
                "HEDGE", market, opposite_token, fill_price, 0,
                position.token_quantity, 0, 0, "REJECTED",
            )
            return None
        target_cost = position.token_quantity * fill_price
        qty = min(position.token_quantity, CONFIG.max_hedge_usd / fill_price)
        if target_cost > CONFIG.max_hedge_usd:
            qty = math.floor(qty * 100) / 100
        hedge_cost = qty * fill_price
        if qty < PAPER_MIN_ORDER_SHARES:
            log.warning(
                "[PAPER] HEDGE rejected: %.2f shares below %.2f-share minimum",
                qty,
                PAPER_MIN_ORDER_SHARES,
            )
            self._log_order(
                "HEDGE", market, opposite_token, fill_price,
                target_cost, position.token_quantity, 0, 0, "REJECTED",
            )
            return None

        self._log_order(
            "HEDGE", market, opposite_token, fill_price,
            target_cost, position.token_quantity, hedge_cost, qty, "FILLED",
        )

        return TradeRecord(
            market_slug=market.slug,
            trade_type="HEDGE",
            direction=opposite_dir,
            token_id=opposite_token,
            side="BUY",
            amount_usd=hedge_cost,
            fill_price=fill_price,
            token_quantity=qty,
            signal_strength=0.0,
            signal_edge=0.0,
            signal_net_edge=0.0,
            fee_usd=taker_fee_usd(fill_price, qty),
            order_id=f"paper-hedge-{uuid.uuid4().hex[:8]}",
            is_paper=True,
        )

    async def place_exit(
        self, market: Market, position: OpenPosition, shares: float, limit_price: float,
        signal: Signal,
    ) -> TradeRecord | None:
        """Simulate selling only the visible, pre-checked weak-side quantity."""
        qty = min(shares, position.token_quantity)
        if not math.isfinite(qty) or qty < PAPER_MIN_ORDER_SHARES or not 0 < limit_price < 1:
            return None
        return TradeRecord(
            market_slug=market.slug, trade_type="EXIT", direction=position.direction,
            token_id=position.token_id, side="SELL", amount_usd=qty * limit_price,
            fill_price=limit_price, token_quantity=qty, signal_strength=signal.strength,
            signal_edge=signal.edge, signal_fair_prob=signal.fair_prob,
            signal_net_edge=signal.net_edge, fee_usd=taker_fee_usd(limit_price, qty),
            order_id=f"paper-exit-{uuid.uuid4().hex[:8]}", is_paper=True,
        )
