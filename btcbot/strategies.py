"""Registered signal strategies available to the engine and dashboard."""


def _btc_momentum_regime():
    from .signal import SignalGenerator

    return SignalGenerator()


# Generators follow SignalGenerator's reset, feed-update, evaluate, and
# market-start-state contract.
STRATEGIES = {
    "btc_momentum_regime": ("BTC Momentum + Regime", _btc_momentum_regime),
    "paired_tilt_exit": ("Tilted Pair + Dynamic Exit", _btc_momentum_regime),
}


def create_signal_generator(strategy_id: str):
    try:
        return STRATEGIES[strategy_id][1]()
    except KeyError as exc:
        raise ValueError(f"Unknown strategy: {strategy_id}") from exc
