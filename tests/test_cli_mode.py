"""CLI defaults follow the mode saved by the general settings page."""

import btcbot.config as config_module
import btcbot.cli as cli_module
import uvicorn
from typer.testing import CliRunner


def test_serve_uses_saved_live_mode_when_no_flag_is_passed(monkeypatch):
    captured = {}
    monkeypatch.setattr(config_module, "_CONFIG", None)
    monkeypatch.setenv("BOT_PAPER_MODE", "false")
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: captured.update(kwargs))

    result = CliRunner().invoke(cli_module.app, ["serve"])

    assert result.exit_code == 0, result.output
    assert config_module._CONFIG.paper_mode is False
    assert captured["reload"] is False
