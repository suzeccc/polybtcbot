"""Position settings dashboard routes remain validated and local-only."""

from fastapi.testclient import TestClient
import pytest

from btcbot.web.app import app
from btcbot.web.routes import (
    _parse_general_settings, _parse_paired_settings, _parse_position_settings,
)


def _valid_settings(**overrides):
    return {
        "position_mode": "fixed",
        "fixed_size_type": "usd",
        "fixed_position_usd": 2,
        "fixed_position_shares": 5,
        "paired_base_shares": 5,
        "paired_max_cost_usd": 10,
        "paired_max_price": 0.65,
        "paired_tilt_gap_pct": 3,
        "min_position_usd": 2,
        "max_position_usd": 5,
        "bankroll": 100,
        "min_signal_strength_pct": 30,
        "min_edge_pct": 5,
        "btc_5m_volatility": 30,
        "limit_slippage": 0.02,
        "max_daily_loss_usd": 50,
        "max_consecutive_losses": 5,
        "min_price_to_pay": 0,
        "max_price_to_pay": 0.65,
        "hedge_trigger_pct": 15,
        "hedge_enabled": False,
        "max_hedge_usd": 30,
        "regime_window": 20,
        "warmup_sec": 60,
        "cooldown_sec": 60,
        **overrides,
    }


def test_strategy_page_shows_position_controls():
    client = TestClient(app)
    response = client.get("/strategy")
    assert response.status_code == 200
    assert 'name="position_mode"' in response.text
    assert 'name="fixed_position_shares"' in response.text
    assert 'name="strategy"' in response.text
    assert response.text.count('name="strategy"') == 1
    assert "BTC Momentum + Regime" in response.text
    assert "Tilted Pair + Dynamic Exit" in response.text
    assert 'name="paired_base_shares"' in response.text
    assert 'id="momentum-config"' in response.text
    assert 'id="paired-config"' in response.text
    assert "strategy-tabs" not in response.text
    assert 'name="min_signal_strength_pct"' in response.text
    assert 'name="min_price_to_pay"' in response.text
    assert 'name="max_daily_loss_usd"' in response.text
    assert 'name="hedge_enabled"' in response.text
    assert 'name="limit_slippage"' in response.text
    assert 'class="strategy-layout" data-guard-unsaved novalidate' in response.text
    assert "Save changes" in response.text
    assert "Changes save with the button on the right." in response.text
    assert "Selection saves immediately" not in response.text
    assert "Hedges keep the existing trigger and timing" in response.text


def test_legacy_paired_url_returns_to_single_strategy_page():
    response = TestClient(app).get("/strategy/paired", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/strategy"


def test_general_settings_page_separates_account_and_runtime_settings(monkeypatch):
    secret = "0x" + "a" * 64
    monkeypatch.setenv("BOT_PRIVATE_KEY", secret)
    response = TestClient(app).get("/settings")
    assert response.status_code == 200
    assert "General Settings" in response.text
    assert 'href="/settings" class="active"' in response.text
    assert 'name="runtime_mode"' in response.text
    assert 'name="paper_starting_balance"' in response.text
    assert 'name="private_key" type="password"' in response.text
    assert "Live mode can place real orders" in response.text
    assert 'data-guard-unsaved' in response.text
    assert secret not in response.text


def test_general_settings_validates_live_confirmation_and_private_key(monkeypatch):
    monkeypatch.setenv("BOT_PRIVATE_KEY", "")
    with pytest.raises(ValueError, match="Confirm that Live mode"):
        _parse_general_settings({
            "runtime_mode": "live", "paper_starting_balance": 100,
            "confirm_live": False,
        })
    with pytest.raises(ValueError, match="32-byte hexadecimal"):
        _parse_general_settings({
            "runtime_mode": "paper", "paper_starting_balance": 100,
            "private_key": "not-a-key", "confirm_live": False,
        })
    with pytest.raises(ValueError, match="Add a Polymarket private key"):
        _parse_general_settings({
            "runtime_mode": "live", "paper_starting_balance": 100,
            "confirm_live": True,
        })
    values = _parse_general_settings({
        "runtime_mode": "live", "paper_starting_balance": 250.5,
        "private_key": "0x" + "a" * 64, "confirm_live": True,
    })
    assert values["paper_mode"] is False
    assert values["bankroll"] == 250.5


def test_general_settings_save_credentials_without_echoing_them(tmp_path, monkeypatch):
    import btcbot.config as config_module
    import btcbot.engine as engine_module
    import btcbot.execution as execution_module
    import btcbot.paper as paper_module
    import btcbot.risk as risk_module
    import btcbot.web.routes as routes_module

    for module, attribute in [
        (config_module, "_CONFIG"), (routes_module, "CONFIG"),
        (engine_module, "CONFIG"), (execution_module, "CONFIG"),
        (paper_module, "CONFIG"), (risk_module, "CONFIG"),
    ]:
        monkeypatch.setattr(module, attribute, getattr(module, attribute))
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_BANKROLL=100\n", encoding="utf-8")
    monkeypatch.setattr(routes_module, "_ENV_FILE", env_file)
    monkeypatch.setenv("BOT_PRIVATE_KEY", "")

    secret = "0x" + "b" * 64
    response = TestClient(app, client=("127.0.0.1", 50000)).put(
        "/api/settings/general",
        json={
            "runtime_mode": "live", "paper_starting_balance": 250,
            "private_key": secret, "confirm_live": True,
        },
    )

    saved_env = env_file.read_text(encoding="utf-8")
    assert response.status_code == 200
    assert "BOT_PAPER_MODE=false" in saved_env
    assert "BOT_BANKROLL=250.0" in saved_env
    assert f"BOT_PRIVATE_KEY={secret}" in saved_env
    assert secret not in response.text
    assert response.json()["restart_required"] is True
    assert engine_module.CONFIG.bankroll == 250


def test_general_settings_live_save_requires_confirmation_and_local_client():
    payload = {
        "runtime_mode": "live", "paper_starting_balance": 100,
        "confirm_live": False,
    }
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.put("/api/settings/general", json=payload).status_code == 400
    assert TestClient(app).put("/api/settings/general", json=payload).status_code == 403


def test_position_settings_accepts_fixed_and_dynamic_values():
    settings = _parse_position_settings(
        _valid_settings(position_mode="DYNAMIC", fixed_size_type="shares", fixed_position_usd=10)
    )
    assert settings["position_mode"] == "dynamic"
    assert settings["fixed_position_shares"] == 5
    assert settings["min_signal_strength"] == 0.3
    assert settings["hedge_trigger_threshold"] == 0.15
    assert settings["paired_tilt_gap"] == 0.03
    assert settings["strategy"] in {"btc_momentum_regime", "paired_tilt_exit"}


def test_paired_settings_validates_only_strategy_specific_fields():
    values = _parse_paired_settings({
        "strategy": "paired_tilt_exit",
        "paired_base_shares": 6,
        "paired_tilt_pct": 25,
        "paired_max_cost_usd": 12,
        "paired_max_price": 0.60,
        "paired_tilt_gap_pct": 4,
    })
    assert values == {
        "strategy": "paired_tilt_exit",
        "paired_base_shares": 6,
        "paired_tilt_pct": 25,
        "paired_max_cost_usd": 12,
        "paired_max_price": 0.60,
        "paired_tilt_gap": 0.04,
    }


def test_position_settings_rejects_invalid_limits():
    with pytest.raises(ValueError, match="cannot exceed"):
        _parse_position_settings(_valid_settings(min_position_usd=6))


def test_position_settings_write_is_blocked_for_non_local_clients():
    client = TestClient(app)
    response = client.put(
        "/api/settings/position",
        json=_valid_settings(),
    )
    assert response.status_code == 403


def test_local_save_persists_and_applies_new_position_config(tmp_path, monkeypatch):
    import btcbot.config as config_module
    import btcbot.engine as engine_module
    import btcbot.execution as execution_module
    import btcbot.paper as paper_module
    import btcbot.risk as risk_module
    import btcbot.web.routes as routes_module

    config_refs = [
        (config_module, "_CONFIG"),
        (routes_module, "CONFIG"),
        (engine_module, "CONFIG"),
        (execution_module, "CONFIG"),
        (paper_module, "CONFIG"),
        (risk_module, "CONFIG"),
    ]
    for module, attribute in config_refs:
        monkeypatch.setattr(module, attribute, getattr(module, attribute))

    env_file = tmp_path / ".env"
    env_file.write_text("# Keep unrelated settings\nBOT_PRIVATE_KEY=placeholder\n", encoding="utf-8")
    monkeypatch.setattr(routes_module, "_ENV_FILE", env_file)
    env_keys = (
        "BOT_POSITION_MODE", "BOT_FIXED_SIZE_TYPE", "BOT_FIXED_POSITION_USD",
        "BOT_FIXED_POSITION_SHARES", "BOT_MIN_POSITION_USD", "BOT_MAX_POSITION_USD",
        "BOT_MAX_HEDGE_USD",
    )
    for key in env_keys:
        monkeypatch.setenv(key, "old-value")

    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.put(
        "/api/settings/position",
        json=_valid_settings(fixed_size_type="shares", fixed_position_shares=7),
    )

    assert response.status_code == 200
    assert "BOT_PRIVATE_KEY=placeholder" in env_file.read_text(encoding="utf-8")
    assert engine_module.CONFIG.position_mode == "fixed"
    assert risk_module.CONFIG.fixed_position_shares == 7
    assert risk_module.CONFIG.min_signal_strength == 0.3
    assert engine_module.CONFIG.paper_mode is True


def test_local_pair_save_persists_settings_and_selected_strategy(tmp_path, monkeypatch):
    import btcbot.config as config_module
    import btcbot.engine as engine_module
    import btcbot.execution as execution_module
    import btcbot.paper as paper_module
    import btcbot.risk as risk_module
    import btcbot.web.routes as routes_module

    for module, attribute in [
        (config_module, "_CONFIG"), (routes_module, "CONFIG"),
        (engine_module, "CONFIG"), (execution_module, "CONFIG"),
        (paper_module, "CONFIG"), (risk_module, "CONFIG"),
    ]:
        monkeypatch.setattr(module, attribute, getattr(module, attribute))
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_PRIVATE_KEY=placeholder\n", encoding="utf-8")
    monkeypatch.setattr(routes_module, "_ENV_FILE", env_file)

    response = TestClient(app, client=("127.0.0.1", 50000)).put(
        "/api/settings/paired",
        json={
            "strategy": "paired_tilt_exit", "paired_base_shares": 6,
            "paired_tilt_pct": 25,
            "paired_max_cost_usd": 12, "paired_max_price": 0.60,
            "paired_tilt_gap_pct": 4,
        },
    )

    assert response.status_code == 200
    assert "BOT_STRATEGY=paired_tilt_exit" in env_file.read_text(encoding="utf-8")
    assert "BOT_PAIRED_TILT_PCT=25.0" in env_file.read_text(encoding="utf-8")
    assert engine_module.CONFIG.strategy == "paired_tilt_exit"
    assert engine_module.CONFIG.paired_base_shares == 6
    assert engine_module.CONFIG.paired_tilt_pct == 25


def test_strategy_dropdown_save_persists_selected_config_without_separate_page(tmp_path, monkeypatch):
    import btcbot.config as config_module
    import btcbot.engine as engine_module
    import btcbot.execution as execution_module
    import btcbot.paper as paper_module
    import btcbot.risk as risk_module
    import btcbot.web.routes as routes_module

    for module, attribute in [
        (config_module, "_CONFIG"), (routes_module, "CONFIG"),
        (engine_module, "CONFIG"), (execution_module, "CONFIG"),
        (paper_module, "CONFIG"), (risk_module, "CONFIG"),
    ]:
        monkeypatch.setattr(module, attribute, getattr(module, attribute))
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_PRIVATE_KEY=placeholder\n", encoding="utf-8")
    monkeypatch.setattr(routes_module, "_ENV_FILE", env_file)

    response = TestClient(app, client=("127.0.0.1", 50000)).put(
        "/api/settings/strategy-config",
        json=_valid_settings(
            strategy="paired_tilt_exit", paired_tilt_pct=25,
            paired_base_shares=6, paired_max_cost_usd=12,
            paired_max_price=0.60, paired_tilt_gap_pct=4,
        ),
    )

    saved_env = env_file.read_text(encoding="utf-8")
    assert response.status_code == 200
    assert response.json()["strategy"] == "paired_tilt_exit"
    assert "BOT_STRATEGY=paired_tilt_exit" in saved_env
    assert "BOT_PAIRED_TILT_PCT=25.0" in saved_env
    assert "BOT_POSITION_MODE=fixed" in saved_env
    assert engine_module.CONFIG.strategy == "paired_tilt_exit"
    assert engine_module.CONFIG.paired_base_shares == 6
