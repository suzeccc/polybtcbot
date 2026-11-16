"""Core dataclasses shared across the bot."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Market:
    """A single 5-minute BTC Up/Down market window."""

    slug: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    start_ts: int
    end_ts: int
    active: bool = True
    closed: bool = False
    accepting_orders: bool = True
    enable_order_book: bool = True

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.end_ts - time.time())

    @property
    def seconds_elapsed(self) -> float:
        return max(0.0, time.time() - self.start_ts)

    @property
    def is_active(self) -> bool:
        now = time.time()
        return self.start_ts <= now < self.end_ts

    @property
    def is_tradable(self) -> bool:
        return self.active and not self.closed and self.accepting_orders and self.enable_order_book

    def token_id_for(self, direction: str) -> str:
        return self.up_token_id if direction == "UP" else self.down_token_id


@dataclass(frozen=True)
class Signal:
    """Output of the signal generator — a trading recommendation."""

    direction: Literal["UP", "DOWN"]
    strength: float          # 0..1 combined confidence
    edge: float              # fair_prob - market_prob
    btc_momentum: float      # $/second recent rate
    poly_implied_prob: float  # what Polymarket thinks
    fair_prob: float          # what we think
    reason: str
    expected_entry_price: float | None = None
    estimated_entry_cost_per_share: float | None = None
    probability_up: float | None = None

    @property
    def net_edge(self) -> float:
        cost = self.estimated_entry_cost_per_share
        return self.edge if cost is None else self.fair_prob - cost


@dataclass
class TradeRecord:
    """A single executed trade (entry, hedge, or exit)."""

    market_slug: str
    trade_type: Literal["ENTRY", "HEDGE", "EXIT"]
    direction: Literal["UP", "DOWN"]
    token_id: str
    side: Literal["BUY", "SELL"]
    amount_usd: float
    fill_price: float
    token_quantity: float
    signal_strength: float
    signal_edge: float
    order_id: str = ""
    is_paper: bool = False
    created_at: int = field(default_factory=lambda: int(time.time()))
    signal_fair_prob: float | None = None
    signal_net_edge: float | None = None
    fee_usd: float = 0.0


@dataclass
class OpenPosition:
    """Tracks a live position within a market window."""

    market: Market
    direction: Literal["UP", "DOWN"]
    token_id: str
    fill_price: float
    token_quantity: float
    entry_time: float = field(default_factory=time.time)
    hedge_count: int = 0
    hedge_amount_usd: float = 0.0
    hedge_token_quantity: float = 0.0
    hedge_fill_price: float | None = None
    strategy_id: str = "btc_momentum_regime"

    @property
    def is_hedged(self) -> bool:
        return self.hedge_count > 0
