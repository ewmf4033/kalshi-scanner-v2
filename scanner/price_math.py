"""
price_math.py — Core pricing and scoring math.

Every function here is unit-tested in tests/test_price_math.py.
No function in this module has any side effect. Pure math.

Functions:
    devig_multiplicative(yes_ask, no_ask) → fair p(YES)
    fill_price(direction, yes_ask, no_ask) → what a taker pays
    gross_pnl(direction, fill_price, outcome_yes) → P&L before fees
    kalshi_fee(ticker, contracts, price, is_taker) → fee in dollars
    clv(direction, fair_mid_at_scan, fair_mid_at_close) → sign-adjusted line move
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_CEILING, getcontext

from core import config

# Decimal precision for money math — 10 digits is way more than enough for fee calcs
getcontext().prec = 28


# ---------------------------------------------------------------------------
# De-vig
# ---------------------------------------------------------------------------

def devig_multiplicative(yes_ask: float, no_ask: float) -> float:
    """
    Multiplicative de-vig: normalize the two asks so they sum to 1.

    p_fair_yes = yes_ask / (yes_ask + no_ask)

    When the book is tight (asks sum to 1.00), this just returns yes_ask.
    When there's vig (asks sum > 1), this removes the overround proportionally.
    """
    if not (0.0 <= yes_ask <= 1.0):
        raise ValueError(f"yes_ask must be in [0,1], got {yes_ask}")
    if not (0.0 <= no_ask <= 1.0):
        raise ValueError(f"no_ask must be in [0,1], got {no_ask}")
    denom = yes_ask + no_ask
    if denom <= 0:
        raise ValueError(f"yes_ask + no_ask must be > 0, got {denom}")
    return yes_ask / denom


# ---------------------------------------------------------------------------
# Fill price
# ---------------------------------------------------------------------------

def fill_price(direction: str, yes_ask: float, no_ask: float) -> float:
    """
    What a TAKER pays to enter the trade. Uses the actual ask on the side you're buying.

    Critical: for NO direction, we use no_ask (NOT 1-yes_bid). Kalshi books can be
    asymmetric and using complementary YES bid as a NO ask proxy understates cost.
    """
    if direction == "YES":
        if not (0.0 <= yes_ask <= 1.0):
            raise ValueError(f"yes_ask must be in [0,1], got {yes_ask}")
        return yes_ask
    elif direction == "NO":
        if not (0.0 <= no_ask <= 1.0):
            raise ValueError(f"no_ask must be in [0,1], got {no_ask}")
        return no_ask
    else:
        raise ValueError(f"direction must be 'YES' or 'NO', got {direction!r}")


# ---------------------------------------------------------------------------
# Gross P&L
# ---------------------------------------------------------------------------

def gross_pnl(direction: str, fill_price: float, outcome_yes: int) -> float:
    """
    Symmetric formula: gross = (1 - fill_price) if win else -fill_price.

    Works identically for YES and NO because fill_price is what you paid on the side
    you bought. Doesn't matter which side — if your side won, contract pays $1.

    Args:
        direction: "YES" or "NO" (used to determine if this trade won)
        fill_price: what you paid in dollars (0.00–1.00)
        outcome_yes: 1 if market resolved YES, 0 if resolved NO
    """
    if direction not in ("YES", "NO"):
        raise ValueError(f"direction must be 'YES' or 'NO', got {direction!r}")
    if outcome_yes not in (0, 1):
        raise ValueError(f"outcome_yes must be 0 or 1, got {outcome_yes}")
    if not (0.0 <= fill_price <= 1.0):
        raise ValueError(f"fill_price must be in [0,1], got {fill_price}")

    won = (direction == "YES" and outcome_yes == 1) or (direction == "NO" and outcome_yes == 0)
    return (1.0 - fill_price) if won else -fill_price


# ---------------------------------------------------------------------------
# Kalshi fees
# ---------------------------------------------------------------------------

def _round_up_cent_decimal(dollars: Decimal) -> Decimal:
    """
    Round up to next cent using Decimal to avoid float representation errors.

    IEEE-754 float bug: 0.07 * 100 * 0.50 * 0.50 = 1.7500000000000002, not 1.75.
    math.ceil of (that * 100) gives 176, not 175. Using Decimal throughout avoids this.
    """
    return dollars.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def kalshi_fee(ticker: str, contracts: int, price: float, is_taker: bool = True) -> float:
    """
    Fee for a Kalshi order. Returns total fee in dollars for the full order.

    Per Kalshi's official fee schedule (FEE_SCHEDULE_VERSION):
        General markets: fee = ceil(0.07 × C × P × (1-P))  [taker]
                         fee = ceil(0.0175 × C × P × (1-P)) [maker]
        S&P/NDX markets: fee = ceil(0.035 × C × P × (1-P))  [taker]
                         fee = ceil(0.00875 × C × P × (1-P)) [maker]

    Rounding is applied to the TOTAL order value, not per-contract.
    Uses Decimal arithmetic to avoid IEEE-754 float errors — those would give $1.76
    instead of the Kalshi-published $1.75 on 100 contracts at $0.50.

    Args:
        ticker: full market ticker (used to check for special-fee markets)
        contracts: number of contracts (must be >= 1)
        price: contract price in dollars (0.00–1.00)
        is_taker: True for immediately-matched orders, False for resting orders

    Returns:
        Total fee in dollars (already rounded up to the cent).
    """
    if contracts < 1:
        raise ValueError(f"contracts must be >= 1, got {contracts}")
    if not (0.0 <= price <= 1.0):
        raise ValueError(f"price must be in [0,1], got {price}")

    if config.is_special_fee_ticker(ticker):
        coef = config.FEE_TAKER_SP_NDX if is_taker else config.FEE_MAKER_SP_NDX
    else:
        coef = config.FEE_TAKER_GENERAL if is_taker else config.FEE_MAKER_GENERAL

    # Decimal throughout — construct from strings to avoid float contamination
    d_coef = Decimal(str(coef))
    d_contracts = Decimal(contracts)
    d_price = Decimal(str(price))
    d_one_minus_price = Decimal("1") - d_price

    raw_fee = d_coef * d_contracts * d_price * d_one_minus_price
    rounded = _round_up_cent_decimal(raw_fee)
    return float(rounded)


# ---------------------------------------------------------------------------
# CLV
# ---------------------------------------------------------------------------

def clv(direction: str, fair_mid_at_scan: float, fair_mid_at_close: float) -> float:
    """
    Closing Line Value — sign-adjusted move in de-vigged fair mid
    between scan time and near-close.

    YES direction: positive CLV = fair moved UP = line came to you = good
    NO direction:  positive CLV = fair moved DOWN = line came to you = good

    CLV is a better edge indicator than P&L at small samples because it
    averages over outcomes. A bettor with true edge tends to see positive CLV
    even on losing tickets.
    """
    if direction not in ("YES", "NO"):
        raise ValueError(f"direction must be 'YES' or 'NO', got {direction!r}")
    if not (0.0 <= fair_mid_at_scan <= 1.0):
        raise ValueError(f"fair_mid_at_scan must be in [0,1], got {fair_mid_at_scan}")
    if not (0.0 <= fair_mid_at_close <= 1.0):
        raise ValueError(f"fair_mid_at_close must be in [0,1], got {fair_mid_at_close}")

    delta = fair_mid_at_close - fair_mid_at_scan
    return delta if direction == "YES" else -delta
