from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import btcbot.execution as execution


def test_secure_client_resolves_and_reuses_polymarket_wallet(monkeypatch):
    from polymarket import SecureClient

    key = "0x" + "1" * 64
    client = SimpleNamespace(wallet_type="DEPOSIT_WALLET", close=MagicMock())
    create = MagicMock(return_value=client)
    monkeypatch.setattr(execution, "CONFIG", replace(execution.CONFIG, private_key=key))
    monkeypatch.setattr(execution, "_CLOB_CLIENT", None)
    monkeypatch.setattr(SecureClient, "create", create)

    assert execution._build_clob_client() is client
    assert execution._build_clob_client() is client
    create.assert_called_once_with(private_key=key)


def test_live_balance_uses_authenticated_collateral_balance(monkeypatch):
    client = SimpleNamespace(
        get_balance_allowance=MagicMock(return_value=SimpleNamespace(balance=1_234_567))
    )
    monkeypatch.setattr(execution, "_build_clob_client", lambda: client)

    assert execution.wallet_usdc_balance() == 1.234567
    client.get_balance_allowance.assert_called_once_with(asset_type="COLLATERAL")
