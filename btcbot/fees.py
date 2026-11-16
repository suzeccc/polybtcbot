"""Polymarket crypto taker-fee estimates."""

CRYPTO_TAKER_FEE_RATE = 0.07


def taker_fee_per_share(price: float, rate: float = CRYPTO_TAKER_FEE_RATE) -> float:
    # shortcut: preflight uses BTC's current 7% rate; matched fills use the rate reported by the CLOB.
    return rate * price * (1.0 - price)


def taker_fee_usd(price: float, shares: float, rate: float = CRYPTO_TAKER_FEE_RATE) -> float:
    return round(taker_fee_per_share(price, rate) * shares, 5)
