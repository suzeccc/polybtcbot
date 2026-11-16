"""Centralised configuration — every knob is a BOT_ env-var."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .strategies import STRATEGIES

load_dotenv()


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Config:
    # --- Auth (required for live trading) ---
    private_key: str

    # --- API endpoints ---
    gamma_api_base: str
    clob_ws_url: str
    binance_ws_url: str

    # --- Storage ---
    db_path: Path

    # --- Trading parameters ---
    strategy: str
    bankroll: float
    min_signal_strength: float
    min_edge: float
    btc_5m_volatility: float
    limit_slippage: float

    # --- Risk limits ---
    max_position_usd: float
    min_position_usd: float
    position_mode: str
    fixed_size_type: str
    fixed_position_usd: float
    fixed_position_shares: float
    paired_base_shares: float
    paired_tilt_pct: float
    paired_max_cost_usd: float
    paired_max_price: float
    paired_tilt_gap: float
    max_daily_loss_usd: float
    max_consecutive_losses: int
    min_price_to_pay: float
    max_price_to_pay: float
    hedge_trigger_threshold: float
    hedge_enabled: bool
    max_hedge_usd: float

    # --- Regime detection ---
    regime_window: int

    # --- Timing ---
    discovery_interval_sec: float
    discovery_lookahead_sec: float
    discovery_max_backoff_sec: float
    market_data_max_age_sec: float
    warmup_sec: float
    cooldown_sec: float
    risk_check_interval_sec: float

    # --- Web ---
    host: str
    port: int

    # --- Mode ---
    paper_mode: bool
    auto_trading: bool
    log_level: str


def load_config() -> Config:
    strategy = _env_str("BOT_STRATEGY", "btc_momentum_regime").strip()
    if strategy not in STRATEGIES:
        raise ValueError(f"BOT_STRATEGY must be one of: {', '.join(STRATEGIES)}")

    # Explicit legacy Kelly caps imply the old sizing behavior unless a mode is selected.
    position_mode = _env_str(
        "BOT_POSITION_MODE",
        "dynamic" if "BOT_MAX_POSITION_USD" in os.environ else "fixed",
    ).strip().lower()
    fixed_size_type = _env_str("BOT_FIXED_SIZE_TYPE", "usd").strip().lower()
    fixed_position_usd = _env_float("BOT_FIXED_POSITION_USD", 2.0)
    fixed_position_shares = _env_float("BOT_FIXED_POSITION_SHARES", 5.0)
    paired_base_shares = _env_float("BOT_PAIRED_BASE_SHARES", 5.0)
    paired_tilt_pct = _env_float("BOT_PAIRED_TILT_PCT", 10.0)
    paired_max_cost_usd = _env_float("BOT_PAIRED_MAX_COST_USD", 10.0)
    paired_max_price = _env_float("BOT_PAIRED_MAX_PRICE", 0.65)
    paired_tilt_gap = _env_float("BOT_PAIRED_TILT_GAP", 0.03)
    max_hedge_usd = _env_float("BOT_MAX_HEDGE_USD", 30.0)
    min_position_usd = _env_float("BOT_MIN_POSITION_USD", 2.0)
    max_position_usd = _env_float("BOT_MAX_POSITION_USD", 5.0)
    discovery_interval_sec = _env_float("BOT_DISCOVERY_INTERVAL_SEC", 5.0)
    discovery_lookahead_sec = _env_float("BOT_DISCOVERY_LOOKAHEAD_SEC", 1800.0)
    discovery_max_backoff_sec = _env_float("BOT_DISCOVERY_MAX_BACKOFF_SEC", 60.0)
    market_data_max_age_sec = _env_float("BOT_MARKET_DATA_MAX_AGE_SEC", 15.0)

    if position_mode not in {"fixed", "dynamic"}:
        raise ValueError("BOT_POSITION_MODE must be 'fixed' or 'dynamic'")
    if fixed_size_type not in {"usd", "shares"}:
        raise ValueError("BOT_FIXED_SIZE_TYPE must be 'usd' or 'shares'")
    if not all(math.isfinite(v) and v > 0 for v in (
        fixed_position_usd, fixed_position_shares, paired_base_shares,
        paired_max_cost_usd, min_position_usd, max_position_usd, max_hedge_usd
    )):
        raise ValueError("Position sizes and limits must be finite positive numbers")
    if not math.isfinite(paired_tilt_pct) or not 0 <= paired_tilt_pct <= 100:
        raise ValueError("BOT_PAIRED_TILT_PCT must be between 0 and 100")
    if min_position_usd > max_position_usd:
        raise ValueError("BOT_MIN_POSITION_USD cannot exceed BOT_MAX_POSITION_USD")
    if not 0 < paired_max_price < 1 or not 0 <= paired_tilt_gap < 1:
        raise ValueError("Paired max price must be between 0 and 1 and tilt gap in [0, 1)")
    if not all(math.isfinite(v) and v > 0 for v in (
        discovery_interval_sec, discovery_lookahead_sec,
        discovery_max_backoff_sec, market_data_max_age_sec,
    )):
        raise ValueError("Discovery and market-data timing values must be finite positive numbers")

    return Config(
        private_key=_env_str("BOT_PRIVATE_KEY", ""),
        gamma_api_base=_env_str("BOT_GAMMA_API", "https://gamma-api.polymarket.com"),
        clob_ws_url=_env_str(
            "BOT_CLOB_WS",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        ),
        binance_ws_url=_env_str(
            "BOT_BINANCE_WS",
            "wss://stream.binance.com:9443/ws/btcusdt@trade",
        ),
        db_path=Path(_env_str("BOT_DB_PATH", "./btcbot.db")),
        strategy=strategy,
        bankroll=_env_float("BOT_BANKROLL", 100.0),
        min_signal_strength=_env_float("BOT_MIN_SIGNAL_STRENGTH", 0.30),
        min_edge=_env_float("BOT_MIN_EDGE", 0.05),
        btc_5m_volatility=_env_float("BOT_BTC_5M_VOLATILITY", 30.0),
        limit_slippage=_env_float("BOT_LIMIT_SLIPPAGE", 0.02),
        max_position_usd=max_position_usd,
        min_position_usd=min_position_usd,
        position_mode=position_mode,
        fixed_size_type=fixed_size_type,
        fixed_position_usd=fixed_position_usd,
        fixed_position_shares=fixed_position_shares,
        paired_base_shares=paired_base_shares,
        paired_tilt_pct=paired_tilt_pct,
        paired_max_cost_usd=paired_max_cost_usd,
        paired_max_price=paired_max_price,
        paired_tilt_gap=paired_tilt_gap,
        max_daily_loss_usd=_env_float("BOT_MAX_DAILY_LOSS_USD", 50.0),
        max_consecutive_losses=_env_int("BOT_MAX_CONSECUTIVE_LOSSES", 5),
        min_price_to_pay=_env_float("BOT_MIN_PRICE_TO_PAY", 0.0),
        max_price_to_pay=_env_float("BOT_MAX_PRICE_TO_PAY", 0.65),
        hedge_trigger_threshold=_env_float("BOT_HEDGE_TRIGGER", 0.15),
        hedge_enabled=_env_bool("BOT_HEDGE_ENABLED", False),
        max_hedge_usd=max_hedge_usd,
        regime_window=_env_int("BOT_REGIME_WINDOW", 20),
        discovery_interval_sec=discovery_interval_sec,
        discovery_lookahead_sec=discovery_lookahead_sec,
        discovery_max_backoff_sec=discovery_max_backoff_sec,
        market_data_max_age_sec=market_data_max_age_sec,
        warmup_sec=_env_float("BOT_WARMUP_SEC", 60.0),
        cooldown_sec=_env_float("BOT_COOLDOWN_SEC", 60.0),
        risk_check_interval_sec=_env_float("BOT_RISK_CHECK_SEC", 2.0),
        host=_env_str("BOT_HOST", "0.0.0.0"),
        port=_env_int("BOT_PORT", 8500),
        paper_mode=_env_bool("BOT_PAPER_MODE", True),
        auto_trading=_env_bool("BOT_AUTO_TRADING", True),
        log_level=_env_str("BOT_LOG_LEVEL", "INFO"),
    )


_CONFIG: Config | None = None


def __getattr__(name: str):
    """Lazy-load CONFIG on first access so CLI flags and env vars are set."""
    global _CONFIG
    if name == "CONFIG":
        if _CONFIG is None:
            _CONFIG = load_config()
        return _CONFIG
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
