"""Pure sizing, calibration, and last-minute decisions for the paired strategy."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

CALIBRATION_BIN = 0.05
MIN_CALIBRATION_SAMPLES = 20
CONFIRM_SAMPLES = 8


def calibration_bins(rows: Iterable[tuple[float, bool]]) -> dict[int, tuple[int, int]]:
    """Return (sample count, wins) for past resolved directional forecasts."""
    counts: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for probability, won in rows:
        if not 0 <= probability <= 1:
            continue
        bucket = min(int((probability + 1e-12) / CALIBRATION_BIN), 19)
        counts[bucket][0] += 1
        counts[bucket][1] += int(won)
    return {bucket: (count, wins) for bucket, (count, wins) in counts.items()}


def calibrated_probability(
    probability: float, bins: Mapping[int, tuple[int, int]], minimum: int = MIN_CALIBRATION_SAMPLES
) -> float | None:
    if not 0 <= probability <= 1:
        return None
    count, wins = bins.get(min(int((probability + 1e-12) / CALIBRATION_BIN), 19), (0, 0))
    if count < minimum:
        return None
    return (wins + 1) / (count + 2)  # Beta(1, 1) smoothing.


def paired_shares(
    base_shares: float,
    up_probability: float | None,
    down_probability: float | None,
    tilt_gap: float = 0.03,
    tilt_pct: float = 10.0,
) -> tuple[float, float]:
    """Give the calibrated higher-probability side the configured extra shares."""
    up, down = base_shares, base_shares
    if up_probability is not None and down_probability is not None:
        if up_probability - down_probability > tilt_gap:
            up *= 1 + tilt_pct / 100
        elif down_probability - up_probability > tilt_gap:
            down *= 1 + tilt_pct / 100
    return up, down


def walk_book(
    levels: Iterable[tuple[float, float]], shares: float, *, selling: bool,
    fee_per_share, slippage: float = 0.0,
) -> tuple[float, float] | None:
    """Walk asks/bids and return (VWAP, net cash) only for a full-size fill."""
    remaining = shares
    gross = 0.0
    fees = 0.0
    for price, size in sorted(levels, key=lambda level: level[0], reverse=selling):
        if not 0 < price < 1 or size <= 0:
            continue
        filled = min(remaining, size)
        gross += filled * price
        fees += fee_per_share(price) * filled
        remaining -= filled
        if remaining <= 1e-8:
            break
    if remaining > 1e-8 or shares <= 0:
        return None
    vwap = gross / shares
    net = gross - fees + (-slippage * shares if selling else slippage * shares)
    return vwap, net


def strong_direction(samples: Iterable[Mapping[str, float]], *, min_move: float) -> str | None:
    """Require eight aligned, persistent samples and a widening CLOB probability gap."""
    rows = list(samples)[-CONFIRM_SAMPLES:]
    if len(rows) < CONFIRM_SAMPLES:
        return None
    if "sample_ts" in rows[0]:
        times = [row["sample_ts"] for row in rows]
        if times[-1] - times[0] < CONFIRM_SAMPLES - 2 or any(
            later - earlier > 2.0 for earlier, later in zip(times, times[1:])
        ):
            return None
    signs = [1 if row["momentum_5"] > 0 else -1 if row["momentum_5"] < 0 else 0 for row in rows]
    direction = signs[0]
    if direction == 0 or any(sign != direction for sign in signs):
        return None
    if any(
        direction * row[key] <= 0
        for row in rows
        for key in ("momentum_15", "momentum_30", "start_delta", "poly_gap")
    ):
        return None
    if any(abs(row["momentum_30"] * 30) < min_move for row in rows):
        return None
    if any(
        abs(row["momentum_5"] * 5) < min_move * 0.05
        or abs(row["momentum_15"] * 15) < min_move * 0.05
        for row in rows
    ):
        return None
    gaps = [direction * row["poly_gap"] for row in rows]
    if gaps[-1] < 0.08 or gaps[-1] - sum(gaps[:4]) / 4 < 0.02:
        return None
    if any(later <= earlier - 0.02 for earlier, later in zip(gaps, gaps[1:])):
        return None
    if gaps[-1] < gaps[-2] - 0.01:
        return None
    return "UP" if direction > 0 else "DOWN"


def should_exit_weak_side(shares: float, sell_net: float, calibrated_win_probability: float | None) -> bool:
    """Only sell when executable net proceeds exceed calibrated settlement EV."""
    return (
        shares > 0 and sell_net >= 0 and calibrated_win_probability is not None
        and sell_net > shares * calibrated_win_probability
    )


def settlement_pnl(up_shares: float, down_shares: float, total_cost: float) -> tuple[float, float]:
    """Return net P&L if UP or DOWN resolves YES."""
    return up_shares - total_cost, down_shares - total_cost
