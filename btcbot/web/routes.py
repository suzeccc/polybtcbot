"""Web routes — HTML pages + JSON API endpoints."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import CONFIG
from ..execution import wallet_usdc_balance
from ..storage.db import connect
from ..storage import repo
from ..strategies import STRATEGIES

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter()
log = logging.getLogger(__name__)
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


# ── Helpers ────────────────────────────────────────────────────────────


def _engine(request: Request) -> Any:
    """Get the engine instance from app state (may be None)."""
    return getattr(request.app.state, "engine", None)


def _engine_running(request: Request) -> bool:
    engine = _engine(request)
    task = getattr(request.app.state, "engine_task", None)
    return engine is not None and (task is None or not task.done())


def _time_ago(ts: int) -> str:
    diff = int(time.time()) - ts
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


def _base_ctx(request: Request, active_page: str) -> dict:
    engine = _engine(request)
    return {
        "request": request,
        "active_page": active_page,
        "paper_mode": engine._paper_mode if engine else CONFIG.paper_mode,
        "engine_running": _engine_running(request),
        "auto_trading_enabled": getattr(engine, "_auto_trading_enabled", CONFIG.auto_trading),
    }


def _parse_position_settings(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")

    payload = {
        "paired_base_shares": CONFIG.paired_base_shares,
        "paired_tilt_pct": CONFIG.paired_tilt_pct,
        "paired_max_cost_usd": CONFIG.paired_max_cost_usd,
        "paired_max_price": CONFIG.paired_max_price,
        "paired_tilt_gap_pct": CONFIG.paired_tilt_gap * 100,
        **payload,
    }

    mode = str(payload.get("position_mode", "")).strip().lower()
    size_type = str(payload.get("fixed_size_type", "")).strip().lower()
    strategy = str(payload.get("strategy", CONFIG.strategy)).strip()
    if mode not in {"fixed", "dynamic"}:
        raise ValueError("Position mode must be fixed or dynamic")
    if size_type not in {"usd", "shares"}:
        raise ValueError("Fixed size type must be usd or shares")
    if strategy not in STRATEGIES:
        raise ValueError("Choose an available strategy")

    def number(field: str, label: str, minimum: float = 0, maximum: float | None = None) -> float:
        raw = payload.get(field)
        if isinstance(raw, bool):
            raise ValueError(f"{label} must be a number")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a number") from exc
        if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
            valid_range = f"{minimum:g}+" if maximum is None else f"{minimum:g} to {maximum:g}"
            raise ValueError(f"{label} must be {valid_range}")
        return value

    def integer(field: str, label: str, minimum: int, maximum: int) -> int:
        value = number(field, label, minimum, maximum)
        if not value.is_integer():
            raise ValueError(f"{label} must be a whole number")
        return int(value)

    values: dict[str, Any] = {
        "strategy": strategy,
        "position_mode": mode,
        "fixed_size_type": size_type,
        "fixed_position_usd": number("fixed_position_usd", "Fixed USDC", 0.01),
        "fixed_position_shares": number("fixed_position_shares", "Fixed Shares", 0.01),
        "paired_base_shares": number("paired_base_shares", "Paired base shares", 0.01),
        "paired_tilt_pct": number("paired_tilt_pct", "Paired share tilt", 0, 100),
        "paired_max_cost_usd": number("paired_max_cost_usd", "Paired maximum cost", 0.01),
        "paired_max_price": number("paired_max_price", "Paired maximum token price", 0.01, 0.99),
        "paired_tilt_gap": number("paired_tilt_gap_pct", "Paired tilt threshold", 0, 99.9) / 100,
        "min_position_usd": number("min_position_usd", "Minimum position", 0.01),
        "max_position_usd": number("max_position_usd", "Maximum position", 0.01),
        "bankroll": number("bankroll", "Bankroll", 0.01),
        "min_signal_strength": number("min_signal_strength_pct", "Minimum signal strength", 0, 100) / 100,
        "min_edge": number("min_edge_pct", "Minimum edge", 0, 100) / 100,
        "btc_5m_volatility": number("btc_5m_volatility", "BTC volatility", 0.01),
        "limit_slippage": number("limit_slippage", "Limit price offset", 0, 0.99),
        "max_daily_loss_usd": number("max_daily_loss_usd", "Daily loss limit", 0.01),
        "max_consecutive_losses": integer("max_consecutive_losses", "Loss limit", 1, 1000),
        "min_price_to_pay": number("min_price_to_pay", "Minimum token price", 0, 1),
        "max_price_to_pay": number("max_price_to_pay", "Maximum token price", 0.01, 1),
        "hedge_trigger_threshold": number("hedge_trigger_pct", "Hedge trigger", 0, 100) / 100,
        "max_hedge_usd": number("max_hedge_usd", "Maximum hedge amount", 0.01),
        "regime_window": integer("regime_window", "Regime lookback", 5, 1000),
        "warmup_sec": number("warmup_sec", "Warm-up", 0, 300),
        "cooldown_sec": number("cooldown_sec", "Entry cutoff", 0, 300),
    }
    if not isinstance(payload.get("hedge_enabled"), bool):
        raise ValueError("Hedge setting must be enabled or disabled")
    values["hedge_enabled"] = payload["hedge_enabled"]

    if values["min_position_usd"] > values["max_position_usd"]:
        raise ValueError("Minimum USDC cannot exceed maximum USDC")
    if values["min_price_to_pay"] > values["max_price_to_pay"]:
        raise ValueError("Minimum token price cannot exceed maximum token price")
    if (
        mode == "fixed"
        and size_type == "usd"
        and values["fixed_position_usd"] > values["max_position_usd"]
    ):
        raise ValueError("Fixed USDC cannot exceed maximum position")
    return values


def _parse_paired_settings(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")

    def number(field: str, label: str, minimum: float, maximum: float | None = None) -> float:
        raw = payload.get(field)
        if isinstance(raw, bool):
            raise ValueError(f"{label} must be a number")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a number") from exc
        if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
            valid_range = f"{minimum:g}+" if maximum is None else f"{minimum:g} to {maximum:g}"
            raise ValueError(f"{label} must be {valid_range}")
        return value

    strategy = str(payload.get("strategy", CONFIG.strategy)).strip()
    if strategy not in STRATEGIES:
        raise ValueError("Choose an available strategy")
    return {
        "strategy": strategy,
        "paired_base_shares": number("paired_base_shares", "Base shares", 0.01),
        "paired_tilt_pct": number("paired_tilt_pct", "Share tilt", 0, 100),
        "paired_max_cost_usd": number("paired_max_cost_usd", "Maximum total cost", 0.01),
        "paired_max_price": number("paired_max_price", "Maximum token price", 0.01, 0.99),
        "paired_tilt_gap": number("paired_tilt_gap_pct", "Minimum probability gap", 0, 99.9) / 100,
    }


def _parse_general_settings(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    runtime_mode = str(payload.get("runtime_mode", "")).strip().lower()
    if runtime_mode not in {"paper", "live"}:
        raise ValueError("Choose Paper or Live trading")
    bankroll_raw = payload.get("paper_starting_balance")
    if isinstance(bankroll_raw, bool):
        raise ValueError("Paper starting balance must be a number")
    try:
        bankroll = float(bankroll_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Paper starting balance must be a number") from exc
    if not math.isfinite(bankroll) or bankroll <= 0:
        raise ValueError("Paper starting balance must be greater than 0")

    private_key = payload.get("private_key", "")
    if not isinstance(private_key, str):
        raise ValueError("Private key must be text")
    private_key = private_key.strip()
    if private_key and not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", private_key):
        raise ValueError("Private key must be a 32-byte hexadecimal key")
    confirm_live = payload.get("confirm_live", False)
    if not isinstance(confirm_live, bool):
        raise ValueError("Live-trading confirmation must be checked")
    if runtime_mode == "live" and not confirm_live:
        raise ValueError("Confirm that Live mode can place real orders")
    if runtime_mode == "live" and not (private_key or os.environ.get("BOT_PRIVATE_KEY", CONFIG.private_key)):
        raise ValueError("Add a Polymarket private key before enabling Live mode")

    return {
        "runtime_mode": runtime_mode,
        "paper_mode": runtime_mode == "paper",
        "bankroll": bankroll,
        "private_key": private_key,
    }


def _request_is_local(request: Request) -> bool:
    if any(
        request.headers.get(header)
        for header in ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")
    ):
        return False
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _persist_env_settings(env_values: dict[str, str]) -> None:
    from dotenv import set_key

    _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".env-", suffix=".tmp", dir=_ENV_FILE.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        if _ENV_FILE.exists():
            shutil.copy2(_ENV_FILE, temp_path)
        for key, value in env_values.items():
            set_key(temp_path, key, value, quote_mode="never")
        os.replace(temp_path, _ENV_FILE)
    finally:
        temp_path.unlink(missing_ok=True)
    for key, value in env_values.items():
        os.environ[key] = value


def _persist_position_settings(values: dict[str, Any]) -> None:
    env_values = {
        "BOT_STRATEGY": values["strategy"],
        "BOT_POSITION_MODE": values["position_mode"],
        "BOT_FIXED_SIZE_TYPE": values["fixed_size_type"],
        "BOT_FIXED_POSITION_USD": str(values["fixed_position_usd"]),
        "BOT_FIXED_POSITION_SHARES": str(values["fixed_position_shares"]),
        "BOT_PAIRED_BASE_SHARES": str(values["paired_base_shares"]),
        "BOT_PAIRED_TILT_PCT": str(values["paired_tilt_pct"]),
        "BOT_PAIRED_MAX_COST_USD": str(values["paired_max_cost_usd"]),
        "BOT_PAIRED_MAX_PRICE": str(values["paired_max_price"]),
        "BOT_PAIRED_TILT_GAP": str(values["paired_tilt_gap"]),
        "BOT_MIN_POSITION_USD": str(values["min_position_usd"]),
        "BOT_MAX_POSITION_USD": str(values["max_position_usd"]),
        "BOT_BANKROLL": str(values["bankroll"]),
        "BOT_MIN_SIGNAL_STRENGTH": str(values["min_signal_strength"]),
        "BOT_MIN_EDGE": str(values["min_edge"]),
        "BOT_BTC_5M_VOLATILITY": str(values["btc_5m_volatility"]),
        "BOT_LIMIT_SLIPPAGE": str(values["limit_slippage"]),
        "BOT_MAX_DAILY_LOSS_USD": str(values["max_daily_loss_usd"]),
        "BOT_MAX_CONSECUTIVE_LOSSES": str(values["max_consecutive_losses"]),
        "BOT_MIN_PRICE_TO_PAY": str(values["min_price_to_pay"]),
        "BOT_MAX_PRICE_TO_PAY": str(values["max_price_to_pay"]),
        "BOT_HEDGE_TRIGGER": str(values["hedge_trigger_threshold"]),
        "BOT_HEDGE_ENABLED": str(values["hedge_enabled"]).lower(),
        "BOT_MAX_HEDGE_USD": str(values["max_hedge_usd"]),
        "BOT_REGIME_WINDOW": str(values["regime_window"]),
        "BOT_WARMUP_SEC": str(values["warmup_sec"]),
        "BOT_COOLDOWN_SEC": str(values["cooldown_sec"]),
    }
    _persist_env_settings(env_values)


def _apply_config_settings(values: dict[str, Any], engine: Any = None) -> None:
    global CONFIG
    new_config = replace(CONFIG, **values)
    from .. import config as config_module

    config_module._CONFIG = new_config
    CONFIG = new_config
    for module_name in (
        "btcbot.engine", "btcbot.risk", "btcbot.signal",
        "btcbot.execution", "btcbot.paper"
    ):
        module = sys.modules.get(module_name)
        if module is not None:
            module.CONFIG = new_config
    if engine is not None and "regime_window" in values:
        engine._regime.resize(new_config.regime_window)
    if engine is not None and "auto_trading" in values:
        engine._auto_trading_enabled = new_config.auto_trading


def _live_ctx(request: Request) -> dict:
    """Build the live dashboard context from engine state."""
    engine = _engine(request)
    engine_running = _engine_running(request)

    ctx: dict[str, Any] = {
        "request": request,
        "btc_price": 0.0,
        "btc_trend": None,
        "chainlink_price": 0.0,
        "chainlink_age": None,
        "chainlink_onchain_price": 0.0,
        "chainlink_onchain_age": None,
        "binance_start_price": None,
        "market": None,
        "market_remaining": 0,
        "signal": None,
        "signal_phase": None,
        "bot_status": "OFFLINE",
        "position": None,
        "poly_up": None,
        "poly_down": None,
        "engine_running": engine_running,
        "total_pnl": 0.0,
        "daily_pnl": 0.0,
        "trades_today": 0,
        "win_rate": 0.0,
    }

    if not engine_running:
        return ctx

    ctx["btc_price"] = engine._binance.latest_price
    ctx["btc_trend"] = engine._binance.trend
    ctx["chainlink_price"] = engine._chainlink.latest_price
    ctx["chainlink_onchain_price"] = engine._chainlink_onchain.latest_price
    ctx["binance_start_price"] = engine._signal_gen._market_start_price
    if engine._chainlink.latest_ts:
        ctx["chainlink_age"] = max(0.0, time.time() - engine._chainlink.latest_ts)
    if engine._chainlink_onchain.latest_ts:
        ctx["chainlink_onchain_age"] = max(0.0, time.time() - engine._chainlink_onchain.latest_ts)

    mkt = engine._current_market
    time_remaining = mkt.seconds_remaining if mkt else 0
    if mkt:
        ctx["market"] = mkt
        ctx["market_remaining"] = time_remaining
        ctx["poly_up"] = engine._polymarket.get_price(mkt.up_token_id)
        ctx["poly_down"] = engine._polymarket.get_price(mkt.down_token_id)

    # Last signal evaluation
    choppiness = engine._regime.choppiness
    if mkt:
        if time_remaining > 300 - (CONFIG.warmup_sec + choppiness * 15):
            ctx["signal_phase"] = "warmup"
        elif time_remaining < CONFIG.cooldown_sec:
            ctx["signal_phase"] = "cooldown"

    sig = engine._signal_gen.evaluate(
        btc_price=engine._binance.latest_price,
        poly_up_price=ctx["poly_up"],
        poly_down_price=ctx["poly_down"],
        time_remaining_sec=time_remaining,
        choppiness=choppiness,
        apply_time_gate=False,
    ) if mkt and engine._binance.latest_price > 0 else None
    ctx["signal"] = sig

    ctx["position"] = engine._position

    if engine._position:
        ctx["bot_status"] = "IN POSITION"
    elif engine._risk.daily_pnl <= -CONFIG.max_daily_loss_usd:
        ctx["bot_status"] = "RISK PAUSED"
    elif (
        engine._risk._loss_cooldown_until > time.time()
        or ctx["signal_phase"] == "cooldown"
    ):
        ctx["bot_status"] = "COOLDOWN"
    elif ctx["signal_phase"] == "warmup":
        ctx["bot_status"] = "WARM-UP"
    elif not mkt:
        ctx["bot_status"] = "WAITING MARKET"
    else:
        ctx["bot_status"] = "MONITORING"

    return ctx


async def _enrich_live_ctx(ctx: dict) -> dict:
    """Add DB-sourced data to the live context."""
    try:
        today_start = int(
            datetime.datetime.combine(
                datetime.date.today(), datetime.time.min
            ).timestamp()
        )
        async with connect() as conn:
            ctx["total_pnl"] = await repo.total_pnl(conn)
            ctx["daily_pnl"] = await repo.pnl_since(conn, today_start)
            ctx["trades_today"] = await repo.count_trades_since(conn, today_start)

            # Win rate — from market_results (all time)
            wins, losses, _ = await repo.win_loss_counts(conn)
            total = wins + losses
            ctx["win_rate"] = (wins / total * 100) if total > 0 else 0

    except Exception:
        pass
    return ctx


def _trade_win_probability(t: repo.TradeRow) -> float | None:
    if t.signal_fair_prob is not None:
        return t.signal_fair_prob
    if t.trade_type == "ENTRY" and t.signal_edge is not None:
        # shortcut: older trades saved edge but not fair probability; fill price approximates the signal-time quote.
        return min(1.0, max(0.0, t.fill_price + t.signal_edge))
    return None


async def _recent_trades_ctx(request: Request, limit: int = 15) -> dict:
    """Build context for the recent trades partial."""
    trades_out = []
    try:
        async with connect() as conn:
            trades = await repo.recent_trades(conn, limit=limit)
        for t in trades:
            trades_out.append({
                "market_slug": t.market_slug,
                "trade_type": t.trade_type,
                "direction": t.direction,
                "fill_price": t.fill_price,
                "token_quantity": t.token_quantity,
                "amount_usd": t.amount_usd,
                "fee_usd": t.fee_usd,
                "win_probability": _trade_win_probability(t),
                "signal_strength": t.signal_strength,
                "is_paper": t.is_paper,
                "time_ago": _time_ago(t.created_at),
            })
    except Exception:
        pass
    return {"request": request, "trades": trades_out}


# ── HTML Routes ────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    ctx = _base_ctx(request, "dashboard")
    live = _live_ctx(request)
    live = await _enrich_live_ctx(live)
    ctx.update(live)
    ctx.update(await _recent_trades_ctx(request))
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/strategy", response_class=HTMLResponse)
async def strategy_page(request: Request):
    ctx = _base_ctx(request, "strategy")
    ctx["strategy_options"] = STRATEGIES
    ctx["selected_strategy"] = CONFIG.strategy
    ctx["paired_config"] = {
        "paired_base_shares": CONFIG.paired_base_shares,
        "paired_tilt_pct": CONFIG.paired_tilt_pct,
        "paired_max_cost_usd": CONFIG.paired_max_cost_usd,
        "paired_max_price": CONFIG.paired_max_price,
        "paired_tilt_gap_pct": CONFIG.paired_tilt_gap * 100,
    }
    ctx["position_config"] = {
        "position_mode": CONFIG.position_mode,
        "fixed_size_type": CONFIG.fixed_size_type,
        "fixed_position_usd": CONFIG.fixed_position_usd,
        "fixed_position_shares": CONFIG.fixed_position_shares,
        "min_position_usd": CONFIG.min_position_usd,
        "max_position_usd": CONFIG.max_position_usd,
        "bankroll": CONFIG.bankroll,
        "min_signal_strength_pct": CONFIG.min_signal_strength * 100,
        "min_edge_pct": CONFIG.min_edge * 100,
        "btc_5m_volatility": CONFIG.btc_5m_volatility,
        "limit_slippage": CONFIG.limit_slippage,
        "max_daily_loss_usd": CONFIG.max_daily_loss_usd,
        "max_consecutive_losses": CONFIG.max_consecutive_losses,
        "min_price_to_pay": CONFIG.min_price_to_pay,
        "max_price_to_pay": CONFIG.max_price_to_pay,
        "hedge_trigger_pct": CONFIG.hedge_trigger_threshold * 100,
        "hedge_enabled": CONFIG.hedge_enabled,
        "max_hedge_usd": CONFIG.max_hedge_usd,
        "regime_window": CONFIG.regime_window,
        "warmup_sec": CONFIG.warmup_sec,
        "cooldown_sec": CONFIG.cooldown_sec,
    }
    return templates.TemplateResponse(request, "strategy.html", ctx)


@router.get("/strategy/paired", response_class=HTMLResponse)
async def paired_strategy_page(request: Request):
    return RedirectResponse("/strategy", status_code=307)


@router.get("/settings", response_class=HTMLResponse)
async def general_settings_page(request: Request):
    ctx = _base_ctx(request, "settings")
    saved_paper_mode = os.environ.get(
        "BOT_PAPER_MODE", "true" if CONFIG.paper_mode else "false"
    ).strip().lower() in {"1", "true", "yes"}
    engine = _engine(request)
    current_paper_mode = getattr(engine, "_paper_mode", CONFIG.paper_mode)
    saved_private_key = os.environ.get("BOT_PRIVATE_KEY", CONFIG.private_key)
    ctx.update({
        "settings_config": {
            "paper_starting_balance": CONFIG.bankroll,
            "runtime_mode": "paper" if saved_paper_mode else "live",
            "current_runtime_mode": "paper" if current_paper_mode else "live",
            "account_configured": bool(saved_private_key),
            "account_active": bool(CONFIG.private_key) and saved_private_key == CONFIG.private_key,
        },
    })
    return templates.TemplateResponse(request, "general_settings.html", ctx)


@router.put("/api/settings/general")
async def save_general_settings(request: Request):
    if not _request_is_local(request):
        raise HTTPException(status_code=403, detail="Settings can only be changed from localhost")
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(status_code=415, detail="Send settings as JSON")
    try:
        values = _parse_general_settings(await request.json())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    env_values = {
        "BOT_PAPER_MODE": str(values["paper_mode"]).lower(),
        "BOT_BANKROLL": str(values["bankroll"]),
    }
    if values["private_key"]:
        env_values["BOT_PRIVATE_KEY"] = values["private_key"]
    try:
        _persist_env_settings(env_values)
    except OSError:
        log.warning("Failed to save general settings", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not save .env settings")

    _apply_config_settings({"bankroll": values["bankroll"]}, engine=_engine(request))
    log.info(
        "General settings saved: requested_mode=%s, account_key_updated=%s",
        values["runtime_mode"], bool(values["private_key"]),
    )
    return JSONResponse({
        "ok": True,
        "runtime_mode": values["runtime_mode"],
        "account_configured": bool(os.environ.get("BOT_PRIVATE_KEY", CONFIG.private_key)),
        "restart_required": True,
    })


@router.put("/api/auto-trading")
async def set_auto_trading(request: Request):
    if not _request_is_local(request):
        raise HTTPException(status_code=403, detail="Auto trading can only be changed from localhost")
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(status_code=415, detail="Send settings as JSON")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Expected a JSON object") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        raise HTTPException(status_code=400, detail="Auto trading must be enabled or disabled")

    engine = _engine(request)
    if engine is None or not _engine_running(request):
        raise HTTPException(status_code=503, detail="Bot engine is not running")

    enabled = payload["enabled"]
    async with engine._order_lock:
        try:
            _persist_env_settings({"BOT_AUTO_TRADING": str(enabled).lower()})
        except OSError as exc:
            log.warning("Failed to save auto-trading setting", exc_info=True)
            raise HTTPException(status_code=500, detail="Could not save auto-trading setting") from exc
        _apply_config_settings({"auto_trading": enabled}, engine=engine)
    log.info("Automatic new entries %s", "enabled" if enabled else "paused")
    return JSONResponse({"ok": True, "enabled": enabled})


@router.put("/api/settings/strategy")
async def save_strategy_settings(request: Request):
    if not _request_is_local(request):
        raise HTTPException(status_code=403, detail="Strategy settings can only be changed from localhost")
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(status_code=415, detail="Send strategy as JSON")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Expected a JSON object") from exc
    strategy = str(payload.get("strategy", "")).strip() if isinstance(payload, dict) else ""
    if strategy not in STRATEGIES:
        raise HTTPException(status_code=400, detail="Choose an available strategy")
    try:
        _persist_env_settings({"BOT_STRATEGY": strategy})
    except OSError:
        log.warning("Failed to save strategy setting", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not save .env strategy setting")
    engine = _engine(request)
    _apply_config_settings({"strategy": strategy}, engine=engine)
    active = engine is not None and getattr(engine, "_signal_strategy", None) == strategy
    log.info("Strategy selected: %s", strategy)
    return JSONResponse({"ok": True, "strategy": strategy, "active": active})


@router.put("/api/settings/position")
async def save_position_settings(request: Request):
    if not _request_is_local(request):
        raise HTTPException(
            status_code=403,
            detail="Position settings can only be changed from localhost",
        )
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(status_code=415, detail="Send settings as JSON")
    try:
        values = _parse_position_settings(await request.json())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        _persist_position_settings(values)
    except OSError:
        log.warning("Failed to save position settings", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not save .env settings")
    _apply_config_settings(values, engine=_engine(request))
    log.info(
        "Position settings updated: mode=%s type=%s",
        values["position_mode"].upper(),
        values["fixed_size_type"].upper(),
    )
    return JSONResponse({"ok": True, "position": values})


@router.put("/api/settings/strategy-config")
async def save_strategy_configuration(request: Request):
    if not _request_is_local(request):
        raise HTTPException(status_code=403, detail="Strategy settings can only be changed from localhost")
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(status_code=415, detail="Send settings as JSON")
    try:
        payload = await request.json()
        values = _parse_position_settings(payload)
        values.update(_parse_paired_settings(payload))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        _persist_position_settings(values)
    except OSError:
        log.warning("Failed to save strategy configuration", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not save .env strategy settings")
    _apply_config_settings(values, engine=_engine(request))
    log.info("Strategy configuration saved: strategy=%s", values["strategy"])
    return JSONResponse({"ok": True, "strategy": values["strategy"]})


@router.put("/api/settings/paired")
async def save_paired_settings(request: Request):
    if not _request_is_local(request):
        raise HTTPException(status_code=403, detail="Strategy settings can only be changed from localhost")
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(status_code=415, detail="Send settings as JSON")
    try:
        values = _parse_paired_settings(await request.json())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        _persist_env_settings({
            "BOT_STRATEGY": values["strategy"],
            "BOT_PAIRED_BASE_SHARES": str(values["paired_base_shares"]),
            "BOT_PAIRED_TILT_PCT": str(values["paired_tilt_pct"]),
            "BOT_PAIRED_MAX_COST_USD": str(values["paired_max_cost_usd"]),
            "BOT_PAIRED_MAX_PRICE": str(values["paired_max_price"]),
            "BOT_PAIRED_TILT_GAP": str(values["paired_tilt_gap"]),
        })
    except OSError:
        log.warning("Failed to save paired strategy settings", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not save paired strategy settings")
    _apply_config_settings(values, engine=_engine(request))
    return JSONResponse({"ok": True, "settings": values})


@router.get("/partials/live", response_class=HTMLResponse)
async def partial_live(request: Request):
    """htmx partial: live status panels + stats."""
    ctx = _live_ctx(request)
    ctx = await _enrich_live_ctx(ctx)
    return templates.TemplateResponse(request, "partials/live.html", ctx)


@router.put("/api/markets/refresh")
async def refresh_market(request: Request):
    if not _request_is_local(request):
        raise HTTPException(status_code=403, detail="Market refresh is only available from localhost")
    engine = _engine(request)
    if engine is None or not _engine_running(request):
        raise HTTPException(status_code=503, detail="Bot engine is not running")
    engine.request_market_refresh()
    return JSONResponse({"ok": True})


@router.get("/partials/recent-trades", response_class=HTMLResponse)
async def partial_recent_trades(request: Request):
    """htmx partial: recent trades table."""
    ctx = await _recent_trades_ctx(request)
    return templates.TemplateResponse(request, "partials/recent_trades.html", ctx)


@router.get("/partials/balances", response_class=HTMLResponse)
async def partial_balances(request: Request):
    """htmx partial: simulated and on-chain USDC cash balances."""
    paper_balance = None
    try:
        async with connect() as conn:
            paper_balance = await repo.paper_cash_balance(conn, CONFIG.bankroll)
    except Exception:
        log.warning("Failed to load paper balance", exc_info=True)

    live_balance = None
    live_status = "Set BOT_PRIVATE_KEY" if not CONFIG.private_key else "Unavailable"
    if CONFIG.private_key:
        try:
            live_balance = await asyncio.to_thread(wallet_usdc_balance)
            live_status = ""
        except Exception:
            log.warning("Failed to load live wallet balance", exc_info=True)

    return templates.TemplateResponse(request, "partials/balances.html", {
        "request": request,
        "paper_balance": paper_balance,
        "live_balance": live_balance,
        "live_status": live_status,
    })


@router.get("/trades", response_class=HTMLResponse)
async def trades_page(
    request: Request,
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    page: int = 1,
):
    ctx = _base_ctx(request, "trades")
    trades_out = []
    win_rate = avg_net_pnl = total_net_pnl = 0.0
    page_size = 50
    page = max(page, 1)
    page_count = total_trades = entries = hedges = 0
    now = datetime.datetime.now().replace(second=0, microsecond=0)
    range_start = start or now.replace(hour=0, minute=0)
    range_end = end or now
    range_start_ts = int(range_start.timestamp())
    range_end_ts = int(range_end.timestamp())
    if range_start_ts > range_end_ts:
        raise HTTPException(status_code=422, detail="Start time must be before end time")
    range_end_ts += 60  # datetime-local inputs use minute precision; include the selected end minute.
    range_summary = None
    try:
        async with connect() as conn:
            total_trades, entries, hedges = await repo.trade_counts(conn)
            page_count = max(1, math.ceil(total_trades / page_size))
            page = min(page, page_count)
            trades = await repo.recent_trades(
                conn, limit=page_size, offset=(page - 1) * page_size
            )
            summary = await repo.stats_summary(conn, 0)
            range_summary = await repo.stats_summary(conn, range_start_ts, range_end_ts)
            win_rate = summary.win_rate * 100
            total_net_pnl = summary.net_pnl
            avg_net_pnl = summary.net_pnl / summary.trades if summary.trades else 0.0
            # Get results for each trade's market
            results = {}
            slugs = list({t.market_slug for t in trades})
            for slug in slugs:
                cur = await conn.execute(
                    """SELECT m.outcome, mr.outcome_correct, mr.hedge_cost_usd
                       FROM markets m
                       LEFT JOIN market_results mr ON mr.market_slug = m.slug
                       WHERE m.slug = ?""",
                    (slug,),
                )
                row = await cur.fetchone()
                if row:
                    results[slug] = (row[0], row[1], float(row[2] or 0.0))

        for t in trades:
            result_row = results.get(t.market_slug)
            outcome = result_row[0] if result_row else None
            r = result_row[1] if result_row else None
            hedge_cost = result_row[2] if result_row else 0.0
            if t.trade_type == "EXIT":
                result = "exit"
            elif t.trade_type == "HEDGE":
                result = "hedge"
            elif hedge_cost > 0:
                result = "hedge"
            elif r == 1:
                result = "win"
            elif r == 0:
                result = "loss"
            else:
                result = None

            trades_out.append({
                "market_slug": t.market_slug,
                "trade_type": t.trade_type,
                "direction": t.direction,
                "fill_price": t.fill_price,
                "token_quantity": t.token_quantity,
                "amount_usd": t.amount_usd,
                "fee_usd": t.fee_usd,
                "pnl": (
                    t.amount_usd - t.fee_usd if t.trade_type == "EXIT" else
                    (t.token_quantity if t.direction == outcome else 0.0)
                    - t.amount_usd - t.fee_usd
                    if outcome else None
                ),
                "win_probability": _trade_win_probability(t),
                "signal_strength": t.signal_strength,
                "is_paper": t.is_paper,
                "time_str": datetime.datetime.fromtimestamp(t.created_at).strftime("%m-%d %H:%M:%S"),
                "result": result,
            })
    except Exception:
        pass

    ctx["trades"] = trades_out
    ctx["total_trades"] = total_trades
    ctx["entries"] = entries
    ctx["hedges"] = hedges
    ctx["page"] = page
    ctx["page_count"] = page_count or 1
    ctx["showing_from"] = (page - 1) * page_size + 1 if total_trades else 0
    ctx["showing_to"] = min(page * page_size, total_trades)
    ctx["win_rate"] = win_rate
    ctx["avg_net_pnl"] = avg_net_pnl
    ctx["total_net_pnl"] = total_net_pnl
    ctx["range_start"] = range_start.strftime("%Y-%m-%dT%H:%M")
    ctx["range_end"] = range_end.strftime("%Y-%m-%dT%H:%M")
    ctx["range_markets"] = range_summary.trades if range_summary else 0
    ctx["range_win_rate"] = range_summary.win_rate * 100 if range_summary else 0.0
    ctx["range_wins"] = range_summary.wins if range_summary else 0
    ctx["range_losses"] = range_summary.losses if range_summary else 0
    ctx["range_hedged"] = range_summary.hedged if range_summary else 0
    ctx["range_net_pnl"] = range_summary.net_pnl if range_summary else 0.0
    return templates.TemplateResponse(request, "trades.html", ctx)


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    ctx = _base_ctx(request, "history")
    try:
        async with connect() as conn:
            days = await repo.get_daily_pnl(conn, days=90)
            total_pnl = await repo.total_pnl(conn)
    except Exception:
        days = []
        total_pnl = 0.0

    total_trades = sum(d.wins + d.losses for d in days)
    total_wins = sum(d.wins for d in days)
    win_rate = (total_wins / total_trades * 100) if total_trades else 0
    best_day = max((d.net_pnl_usd for d in days), default=0)
    worst_day = min((d.net_pnl_usd for d in days), default=0)

    # Add win_rate per day
    enriched_days = []
    for d in days:
        total = d.wins + d.losses
        enriched_days.append(type("Day", (), {
            **{k: getattr(d, k) for k in d.__dataclass_fields__},
            "win_rate": (d.wins / total * 100) if total > 0 else 0,
        })())

    ctx["days"] = enriched_days
    ctx["total_pnl"] = total_pnl
    ctx["total_trades"] = total_trades
    ctx["win_rate"] = win_rate
    ctx["best_day"] = best_day
    ctx["worst_day"] = worst_day
    return templates.TemplateResponse(request, "history.html", ctx)


# ── JSON API ───────────────────────────────────────────────────────────


@router.get("/api/live")
async def api_live(request: Request):
    """Live engine state as JSON."""
    ctx = _live_ctx(request)
    ctx = await _enrich_live_ctx(ctx)
    return JSONResponse({
        "btc_price": ctx["btc_price"],
        "btc_trend": ctx["btc_trend"],
        "market": ctx["market"].slug if ctx["market"] else None,
        "market_remaining": ctx["market_remaining"],
        "signal_phase": ctx["signal_phase"],
        "poly_up": ctx["poly_up"],
        "poly_down": ctx["poly_down"],
        "signal": {
            "direction": ctx["signal"].direction,
            "strength": round(ctx["signal"].strength, 3),
            "edge": round(ctx["signal"].edge, 4),
            "net_edge": round(ctx["signal"].net_edge, 4),
            "fair_prob": round(ctx["signal"].fair_prob, 4),
        } if ctx["signal"] and ctx["signal"].strength > 0 else None,
        "position": {
            "direction": ctx["position"].direction,
            "fill_price": ctx["position"].fill_price,
            "quantity": ctx["position"].token_quantity,
        } if ctx["position"] else None,
        "total_pnl": round(ctx["total_pnl"], 2),
        "daily_pnl": round(ctx["daily_pnl"], 2),
        "trades_today": ctx["trades_today"],
        "win_rate": round(ctx["win_rate"], 1),
    })


@router.get("/api/trades")
async def api_trades(limit: int = 50):
    async with connect() as conn:
        trades = await repo.recent_trades(conn, limit=limit)
    return JSONResponse([
        {
            "market_slug": t.market_slug,
            "trade_type": t.trade_type,
            "direction": t.direction,
            "fill_price": t.fill_price,
            "token_quantity": t.token_quantity,
            "amount_usd": round(t.amount_usd, 2),
            "signal_edge": round(t.signal_edge, 4) if t.signal_edge else None,
            "signal_net_edge": round(t.signal_net_edge, 4) if t.signal_net_edge is not None else None,
            "signal_fair_prob": round(t.signal_fair_prob, 4) if t.signal_fair_prob is not None else None,
            "fee_usd": round(t.fee_usd, 5),
            "signal_strength": round(t.signal_strength, 3) if t.signal_strength else None,
            "is_paper": bool(t.is_paper),
            "created_at": t.created_at,
        }
        for t in trades
    ])


@router.get("/api/daily-pnl")
async def api_daily_pnl(days: int = 30):
    async with connect() as conn:
        rows = await repo.get_daily_pnl(conn, days=days)
    # Reverse so oldest first (for charts)
    rows = list(reversed(rows))
    return JSONResponse([
        {
            "date": d.date,
            "trades_count": d.trades_count,
            "wins": d.wins,
            "losses": d.losses,
            "hedged": d.hedged,
            "net_pnl_usd": round(d.net_pnl_usd, 2),
        }
        for d in rows
    ])


@router.get("/api/hourly-pnl")
async def api_hourly_pnl(hours: int = 24):
    async with connect() as conn:
        rows = await repo.hourly_pnl(conn, hours=hours)
    return JSONResponse(rows)


# ── Stats page ─────────────────────────────────────────────────────────


_ALL_TIME_MONTHLY_THRESHOLD_SEC = 2 * 365 * 86400  # switch to monthly grain if data spans > 2y


async def _build_stats_payload(period: str) -> dict:
    """Shared backend logic for both /stats (HTML) and /api/stats (JSON)."""
    since_ts, grain = repo.period_bounds(period)
    async with connect() as conn:
        if period == "all":
            oldest = await repo.oldest_resolved_at(conn)
            if oldest is not None and (int(time.time()) - oldest) > _ALL_TIME_MONTHLY_THRESHOLD_SEC:
                grain = "month"
        summary = await repo.stats_summary(conn, since_ts)
        buckets = await repo.stats_buckets(conn, since_ts, grain)
    equity = repo.stats_equity(buckets)

    tiles = {
        "net_pnl": round(summary.net_pnl, 2),
        "trades": summary.trades,
        "win_rate": round(summary.win_rate, 4),
        "wins": summary.wins,
        "losses": summary.losses,
        "hedged": summary.hedged,
        "best_market": (
            {"slug": summary.best_market[0], "pnl": round(summary.best_market[1], 2)}
            if summary.best_market is not None else None
        ),
        "worst_market": (
            {"slug": summary.worst_market[0], "pnl": round(summary.worst_market[1], 2)}
            if summary.worst_market is not None else None
        ),
    }
    return {
        "period": period,
        "since_ts": since_ts,
        "grain": grain,
        "tiles": tiles,
        "equity": [{"bucket": e.bucket, "value": e.value} for e in equity],
        "bars": [
            {"bucket": b.bucket, "net_pnl": b.net_pnl, "trades": b.trades} for b in buckets
        ],
        "distribution": {
            "wins": summary.wins,
            "losses": summary.losses,
            "hedged": summary.hedged,
        },
    }


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    ctx = _base_ctx(request, "stats")
    try:
        initial = await _build_stats_payload("day")
    except Exception:
        initial = {
            "period": "day",
            "since_ts": 0,
            "grain": "hour",
            "tiles": {
                "net_pnl": 0.0, "trades": 0, "win_rate": 0.0,
                "wins": 0, "losses": 0, "hedged": 0,
                "best_market": None, "worst_market": None,
            },
            "equity": [],
            "bars": [],
            "distribution": {"wins": 0, "losses": 0, "hedged": 0},
        }
    ctx["initial_stats"] = initial
    return templates.TemplateResponse(request, "stats.html", ctx)


@router.get("/api/stats")
async def api_stats(period: str = "day"):
    try:
        payload = await _build_stats_payload(period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(payload)
