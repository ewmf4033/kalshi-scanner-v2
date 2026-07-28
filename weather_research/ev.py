from __future__ import annotations

from math import ceil


def taker_fee_cents(price_cents: int, contracts: int = 1, multiplier: float = 0.07) -> float:
    """Kalshi fee in cents per contract, including total-fill cent rounding."""
    if not 0 <= price_cents <= 100:
        raise ValueError("price_cents must be between 0 and 100")
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    p = price_cents / 100
    total_dollars = multiplier * contracts * p * (1 - p)
    rounded_total_cents = ceil(total_dollars * 100 - 1e-12)
    return rounded_total_cents / contracts


def certainty_trade_ev_cents(
    price_cents: int,
    error_rate: float,
    fee_cents: float,
    slippage_cents: float = 0.0,
) -> float:
    """Exact EV for a contract believed certain: gross gap minus mapping error and costs."""
    if not 0 <= error_rate <= 1:
        raise ValueError("error_rate must be in [0, 1]")
    return (100 - price_cents) - (100 * error_rate) - fee_cents - slippage_cents


def minimum_gap_cents(
    error_upper_bound: float,
    price_cents: int,
    contracts: int,
    slippage_cents: float = 0.0,
    safety_margin_cents: float = 1.0,
) -> float:
    fee = taker_fee_cents(price_cents, contracts)
    return 100 * error_upper_bound + fee + slippage_cents + safety_margin_cents
