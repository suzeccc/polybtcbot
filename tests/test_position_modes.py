"""Position sizing and mocked order-execution checks."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import btcbot.config as config_module
from btcbot.engine import Engine, MarketRuntime, _summarize_market_result
from btcbot.execution import Executor
from btcbot.models import Market, OpenPosition, Signal
from btcbot.paper import PaperExecutor
from btcbot.risk import RiskManager


@pytest.fixture
def market() -> Market:
    now = int(time.time())
    return Market("market", "condition", "up-token", "down-token", now - 10, now + 290)


def make_signal(price: float = 0.30) -> Signal:
    return Signal("UP", 0.8, 0.12, 1.0, price, min(price + 0.12, 0.99), "test")


class FakeClob:
    def __init__(self, balance_usd: float = 100.0, min_shares: float = 5.0):
        self.balance_usd = balance_usd
        self.min_shares = min_shares

    def get_order_book(self, *, token_id: str):
        return SimpleNamespace(tick_size="0.01", min_order_size=str(self.min_shares))

    def get_balance_allowance(self, *, asset_type: str, token_id: str | None = None):
        return SimpleNamespace(balance=round(self.balance_usd * 1_000_000))


def test_defaults_and_legacy_config_compatibility(monkeypatch):
    fresh = config_module.load_config()
    assert fresh.position_mode == "fixed"
    assert fresh.fixed_size_type == "usd"
    assert fresh.fixed_position_usd == 2.0
    assert fresh.fixed_position_shares == 5.0

    monkeypatch.setenv("BOT_MAX_POSITION_USD", "25")
    monkeypatch.delenv("BOT_POSITION_MODE", raising=False)
    assert config_module.load_config().position_mode == "dynamic"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("BOT_POSITION_MODE", "kelly", "BOT_POSITION_MODE"),
        ("BOT_FIXED_SIZE_TYPE", "tokens", "BOT_FIXED_SIZE_TYPE"),
        ("BOT_FIXED_POSITION_USD", "0", "positive"),
        ("BOT_PAIRED_MAX_PRICE", "1", "between 0 and 1"),
        ("BOT_PAIRED_TILT_GAP", "1", "tilt gap"),
        ("BOT_PAIRED_TILT_PCT", "101", "BOT_PAIRED_TILT_PCT"),
        ("BOT_MIN_POSITION_USD", "8", "cannot exceed"),
    ],
)
def test_invalid_position_environment_values_are_rejected(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        config_module.load_config()


@pytest.mark.parametrize("price", [0.30, 0.50, 0.70])
def test_fixed_shares_conversion_at_multiple_prices(market, price):
    trade = asyncio.run(PaperExecutor().place_trade(market, make_signal(price), price * 5, shares=5))
    assert trade is not None
    assert trade.trade_type == "ENTRY"
    assert trade.token_quantity == 5
    assert trade.amount_usd == pytest.approx(5 * trade.fill_price)


def test_fixed_usd_converts_to_shares_and_rejects_below_exchange_minimum(market):
    trade = asyncio.run(PaperExecutor().place_trade(market, make_signal(0.30), 2.0))
    assert trade is not None
    assert trade.amount_usd <= 2.0
    assert trade.amount_usd + trade.fee_usd <= 2.0

    rejected = asyncio.run(PaperExecutor().place_trade(market, make_signal(0.50), 2.0))
    assert rejected is None


def test_quarter_kelly_keeps_existing_algorithm_with_configured_max(monkeypatch):
    cfg = config_module.load_config()
    monkeypatch.setattr(
        "btcbot.risk.CONFIG",
        cfg.__class__(**{**cfg.__dict__, "position_mode": "dynamic", "max_position_usd": 5.0}),
    )
    size = RiskManager().calc_position_size(make_signal(0.50))
    assert size == 5.0


def test_paper_accepts_dynamic_quarter_kelly_amount(market, monkeypatch):
    cfg = config_module.load_config()
    monkeypatch.setattr(
        "btcbot.risk.CONFIG",
        cfg.__class__(**{**cfg.__dict__, "position_mode": "dynamic", "max_position_usd": 5.0}),
    )
    amount = RiskManager().calc_position_size(make_signal(0.30))
    trade = asyncio.run(PaperExecutor().place_trade(market, make_signal(0.30), amount))
    assert trade is not None
    assert trade.amount_usd + trade.fee_usd <= amount
    assert trade.trade_type == "ENTRY"


def test_live_preflight_rounds_precision_and_respects_market_minimum():
    executor = Executor.__new__(Executor)
    executor._client = FakeClob()
    prepared = executor._prepare_buy("token", 2.0, 0.30)
    assert prepared == pytest.approx((0.32, 5.96, 1.99798, 5.0))
    for price in (0.50, 0.70):
        assert executor._prepare_buy("token", 2.0, price) is None


def test_fixed_shares_preflight_checks_submitted_usdc():
    executor = Executor.__new__(Executor)
    executor._client = FakeClob(balance_usd=1.59)
    assert executor._prepare_buy("token", 1.50, 0.30, shares=5) is None

    executor._client = FakeClob(balance_usd=1.68)
    assert executor._prepare_buy("token", 1.50, 0.30, shares=5) == pytest.approx(
        (0.32, 5.0, 1.67616, 5.0)
    )


def test_invalid_live_order_price_and_amount_never_prepare():
    executor = Executor.__new__(Executor)
    executor._client = FakeClob()
    assert executor._prepare_buy("token", 0.0, 0.30) is None
    assert executor._prepare_buy("token", 2.0, float("nan")) is None


def test_live_entry_uses_mocked_clob_and_records_actual_fill(market, monkeypatch):
    executor = Executor.__new__(Executor)
    executor._client = FakeClob()
    monkeypatch.setattr(executor, "_fok_buy", lambda *args: {
        "orderID": "mock-entry", "filled_size": 6.25, "fill_price": 0.32,
    })
    trade = asyncio.run(executor.place_trade(market, make_signal(0.30), 2.0))
    assert trade is not None
    assert trade.trade_type == "ENTRY"
    assert trade.token_id == "up-token"
    assert trade.amount_usd == pytest.approx(2.0)
    assert trade.token_quantity == 6.25


def test_live_fixed_shares_are_submitted_as_shares(market):
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class SignedLimitOrder:
        size: float
        price: float
        order_type: str = "GTC"

    executor = Executor.__new__(Executor)
    client = MagicMock()
    client.create_limit_order.return_value = SignedLimitOrder(size=5.0, price=0.32)
    client.post_order.return_value = SimpleNamespace(ok=True, order_id="mock-shares")
    executor._client = client
    executor._query_fill = lambda order_id: (5.0, 0.32, "MATCHED", 0.0)

    result = executor._fok_buy("up-token", 1.50, 0.32, shares=5.0, min_order_size=5.0)
    assert result["filled_size"] == 5.0
    args = client.create_limit_order.call_args.kwargs
    assert args["size"] == 5.0
    assert args["price"] == 0.32
    assert client.post_order.call_args.args[0].order_type == "FOK"
    client.create_market_order.assert_not_called()


def test_live_exit_records_only_actual_matched_shares(market):
    executor = Executor.__new__(Executor)
    executor._client = object()
    executor._prepare_sell = lambda token, shares, price: (price, shares, 5.0)
    executor._fok_sell = lambda token, price, shares, minimum: {
        "orderID": "partial-exit", "filled_size": 2.5, "fill_price": 0.42, "fee_usd": 0.0123,
    }
    position = OpenPosition(market, "DOWN", "down-token", 0.5, 5.0)
    trade = asyncio.run(executor.place_exit(market, position, 5.0, 0.40, make_signal()))
    assert trade is not None
    assert trade.trade_type == "EXIT"
    assert trade.side == "SELL"
    assert trade.token_quantity == 2.5
    assert trade.amount_usd == pytest.approx(1.05)
    assert trade.fee_usd == pytest.approx(0.0123)


def test_live_fill_waits_for_all_trade_prices_and_returns_actual_vwap(monkeypatch):
    executor = Executor.__new__(Executor)
    client = MagicMock()
    client.get_order.return_value = SimpleNamespace(
        size_matched="5", price="0.50", status="MATCHED",
        associate_trades=("taker-trade", "maker-trade"),
    )

    def page(items):
        return SimpleNamespace(first_page=lambda: SimpleNamespace(items=items))

    client.list_account_trades.side_effect = [
        page([{"taker_order_id": "order", "size": "3", "price": "0.50", "fee_rate_bps": "700", "maker_orders": []}]),
        page([]),
        page([{"taker_order_id": "order", "size": "3", "price": "0.50", "fee_rate_bps": "700", "maker_orders": []}]),
        page([{"taker_order_id": "other", "size": "4", "price": "0.40", "maker_orders": [
            {"order_id": "order", "matched_amount": "2", "price": "0.40", "fee_rate_bps": "700"},
        ]}]),
    ]
    executor._client = client
    monkeypatch.setattr("btcbot.execution.time.sleep", lambda _seconds: None)

    filled, price, status, fee_usd = executor._query_fill("order")
    assert filled == pytest.approx(5.0)
    assert price == pytest.approx(0.46)
    assert status == "MATCHED"
    assert fee_usd == pytest.approx(0.0525)
    assert [call.kwargs for call in client.list_account_trades.call_args_list] == [
        {"id": trade_id} for trade_id in ("taker-trade", "maker-trade") * 2
    ]


@pytest.mark.parametrize(
    ("mode", "size_type", "expected_amount", "expected_shares"),
    [
        ("fixed", "usd", 2.0, None),
        ("fixed", "shares", 1.5, 5.0),
        ("dynamic", "usd", 3.25, None),
    ],
)
def test_engine_selects_configured_entry_sizing(
    market, monkeypatch, mode, size_type, expected_amount, expected_shares
):
    cfg = config_module.load_config()
    cfg = cfg.__class__(
        **{
            **cfg.__dict__,
            "position_mode": mode,
            "fixed_size_type": size_type,
            "fixed_position_usd": 2.0,
            "fixed_position_shares": 5.0,
            "max_position_usd": 5.0,
        }
    )
    monkeypatch.setattr("btcbot.engine.CONFIG", cfg)
    engine = Engine(paper_mode=True)
    engine._current_market = market
    engine._market_states[market.slug] = MarketRuntime(market, status="ACTIVE")
    engine._market_inputs_fresh = MagicMock(return_value=True)
    engine._price_event.set()
    engine._binance.latest_price = 50_000
    engine._signal_gen.evaluate = MagicMock(
        return_value=Signal("UP", 0.8, 0.12, 1.0, 0.30, 0.72, "test")
    )
    engine._polymarket.get_price = MagicMock(return_value=0.30)
    engine._risk.can_trade = MagicMock(return_value=True)
    engine._risk.calc_position_size = MagicMock(return_value=3.25)
    engine._stop = asyncio.Event()

    async def capture_order(*args, **kwargs):
        engine._stop.set()
        return None

    place_trade = MagicMock(side_effect=capture_order)
    engine._executor = SimpleNamespace(place_trade=place_trade)
    asyncio.run(engine._trading_loop())

    assert place_trade.call_args.args[2] == expected_amount
    assert place_trade.call_args.kwargs["shares"] == expected_shares


def test_live_hedge_keeps_position_based_budget(market, monkeypatch):
    executor = Executor.__new__(Executor)
    executor._client = FakeClob()
    fok = MagicMock(return_value={
        "orderID": "mock-hedge", "filled_size": 5.5, "fill_price": 0.50,
    })
    monkeypatch.setattr(executor, "_fok_buy", fok)
    position = OpenPosition(market, "UP", "up-token", 0.50, 5.5)
    trade = asyncio.run(executor.place_hedge(market, position, estimated_price=0.50))
    assert trade is not None
    assert trade.trade_type == "HEDGE"
    assert trade.token_id == "down-token"
    assert fok.call_args.args[1] == 2.75
    assert fok.call_args.args[3] is None


def test_paper_hedge_preserves_existing_share_relationship(market):
    position = OpenPosition(market, "UP", "up-token", 0.50, 5.0)
    trade = asyncio.run(PaperExecutor().place_hedge(market, position, estimated_price=0.50))
    assert trade is not None
    assert trade.trade_type == "HEDGE"
    assert trade.token_quantity == position.token_quantity

    below_minimum = OpenPosition(market, "UP", "up-token", 0.50, 4.99)
    assert asyncio.run(
        PaperExecutor().place_hedge(market, below_minimum, estimated_price=0.50)
    ) is None


def test_resolution_is_the_existing_exit_path(market):
    # The bot settles and redeems positions; it has no SELL/EXIT order path.
    from btcbot.models import TradeRecord

    entry = TradeRecord("market", "ENTRY", "UP", "up-token", "BUY", 2.0, 0.5, 4.0, 0.8, 0.1)
    entry_cost, hedge_cost, payout, pnl, _ = _summarize_market_result([entry], "UP")
    assert (entry_cost, hedge_cost, payout, pnl) == (2.0, 0.0, 4.0, 2.0)
