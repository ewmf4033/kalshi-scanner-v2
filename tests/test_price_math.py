"""
Unit tests for scanner/price_math.py.

These tests run BEFORE the implementation. If the implementation doesn't pass them,
the implementation is wrong — not the tests.

Each test is hand-calculated. If you want to verify: do the math yourself, see if it matches.
"""

import math
import pytest
from scanner import price_math as pm
from core import config


# ===========================================================================
# DEVIG — multiplicative method
# ===========================================================================

class TestDevigMultiplicative:
    """
    Multiplicative de-vig: p_yes = yes_ask / (yes_ask + no_ask)
    Rationale: when the book is tight (yes_ask + no_ask = 1.00), de-vig = yes_ask.
    When the book is wide (sum > 1), we renormalize.
    """

    def test_perfectly_efficient_market(self):
        """yes_ask=0.50, no_ask=0.50 → fair p_yes=0.50."""
        assert pm.devig_multiplicative(yes_ask=0.50, no_ask=0.50) == pytest.approx(0.50)

    def test_favored_yes_no_vig(self):
        """yes_ask=0.70, no_ask=0.30 → fair p_yes=0.70 (no vig present)."""
        assert pm.devig_multiplicative(yes_ask=0.70, no_ask=0.30) == pytest.approx(0.70)

    def test_favored_yes_with_vig(self):
        """yes_ask=0.72, no_ask=0.32 (4c vig) → fair = 0.72/1.04 ≈ 0.6923."""
        assert pm.devig_multiplicative(yes_ask=0.72, no_ask=0.32) == pytest.approx(0.72 / 1.04)

    def test_symmetric_vig(self):
        """yes_ask=0.52, no_ask=0.52 (4c vig) → fair = 0.52/1.04 = 0.50."""
        assert pm.devig_multiplicative(yes_ask=0.52, no_ask=0.52) == pytest.approx(0.50)

    def test_extreme_favorite(self):
        """yes_ask=0.92, no_ask=0.10 → fair = 0.92/1.02 ≈ 0.9020."""
        assert pm.devig_multiplicative(yes_ask=0.92, no_ask=0.10) == pytest.approx(0.92 / 1.02)

    def test_zero_sum_raises(self):
        """If both asks are 0, we have no market — error, don't return junk."""
        with pytest.raises(ValueError):
            pm.devig_multiplicative(yes_ask=0.0, no_ask=0.0)

    def test_out_of_range_raises(self):
        """Asks must be in [0, 1]."""
        with pytest.raises(ValueError):
            pm.devig_multiplicative(yes_ask=1.5, no_ask=0.3)
        with pytest.raises(ValueError):
            pm.devig_multiplicative(yes_ask=0.3, no_ask=-0.1)


# ===========================================================================
# FILL PRICE
# ===========================================================================

class TestFillPrice:
    """
    Fill price for a taker is the ask on the side you're buying.
    For YES direction: pay yes_ask
    For NO direction:  pay no_ask  (NOT 1-yes_bid — books are not perfectly complementary)
    """

    def test_fill_yes(self):
        assert pm.fill_price(direction="YES", yes_ask=0.55, no_ask=0.47) == 0.55

    def test_fill_no(self):
        """If yes_ask=0.55 and no_ask=0.47, book sum=1.02 (2c vig). Buying NO costs 0.47, not 1-yes_bid."""
        assert pm.fill_price(direction="NO", yes_ask=0.55, no_ask=0.47) == 0.47

    def test_asymmetric_book(self):
        """
        Real Kalshi example: yes_bid=0.41, yes_ask=0.44, no_bid=0.54, no_ask=0.59.
        Buying NO costs 0.59 (its own ask), NOT 1-0.41=0.59 (coincidence, could differ).
        """
        assert pm.fill_price(direction="YES", yes_ask=0.44, no_ask=0.59) == 0.44
        assert pm.fill_price(direction="NO", yes_ask=0.44, no_ask=0.59) == 0.59

    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError):
            pm.fill_price(direction="MAYBE", yes_ask=0.5, no_ask=0.5)


# ===========================================================================
# GROSS P&L — symmetric formula
# ===========================================================================

class TestGrossPnL:
    """
    Symmetric formula: gross = (1 - fill_price) if win else -fill_price.
    Works for BOTH YES and NO because fill_price is what you paid on the side you bought.
    """

    def test_yes_wins(self):
        """Paid 0.40 for YES, outcome=YES → +$0.60 profit."""
        assert pm.gross_pnl(direction="YES", fill_price=0.40, outcome_yes=1) == pytest.approx(0.60)

    def test_yes_loses(self):
        """Paid 0.40 for YES, outcome=NO → -$0.40 loss."""
        assert pm.gross_pnl(direction="YES", fill_price=0.40, outcome_yes=0) == pytest.approx(-0.40)

    def test_no_wins(self):
        """Paid 0.42 for NO, outcome=NO → +$0.58 profit. NOT +$0.42."""
        assert pm.gross_pnl(direction="NO", fill_price=0.42, outcome_yes=0) == pytest.approx(0.58)

    def test_no_loses(self):
        """Paid 0.42 for NO, outcome=YES → -$0.42 loss."""
        assert pm.gross_pnl(direction="NO", fill_price=0.42, outcome_yes=1) == pytest.approx(-0.42)

    def test_extreme_favorite_win(self):
        """Paid 0.95 for YES, outcome=YES → +$0.05 profit (thin margin)."""
        assert pm.gross_pnl(direction="YES", fill_price=0.95, outcome_yes=1) == pytest.approx(0.05)

    def test_extreme_favorite_lose(self):
        """Paid 0.95 for YES, outcome=NO → -$0.95 loss (catastrophe)."""
        assert pm.gross_pnl(direction="YES", fill_price=0.95, outcome_yes=0) == pytest.approx(-0.95)


# ===========================================================================
# KALSHI FEES — verified against official table (kalshi.com/docs/kalshi-fee-schedule.pdf)
# ===========================================================================

class TestKalshiFees:
    """
    Verified against the official fee table in the Feb 5 2026 schedule.
    General formula: fee = round_up(0.07 × C × P × (1-P)) for taker, 0.0175 for maker.
    S&P/NDX formula: fee = round_up(0.035 × C × P × (1-P))
    Rounding is on the TOTAL ORDER, not per-contract.
    """

    # --- Taker fees, general markets (1 contract) ---
    def test_taker_1_contract_at_50_cents(self):
        """Official table: 1 contract at $0.50 → $0.02 fee."""
        assert pm.kalshi_fee("KXCPI-26MAY", contracts=1, price=0.50, is_taker=True) == pytest.approx(0.02)

    def test_taker_1_contract_at_5_cents(self):
        """Official table: 1 contract at $0.05 → $0.01 fee (round up from $0.0033)."""
        assert pm.kalshi_fee("KXCPI-26MAY", contracts=1, price=0.05, is_taker=True) == pytest.approx(0.01)

    def test_taker_1_contract_at_99_cents(self):
        """Official table: 1 contract at $0.99 → $0.01 fee (round up from $0.000693)."""
        assert pm.kalshi_fee("KXCPI-26MAY", contracts=1, price=0.99, is_taker=True) == pytest.approx(0.01)

    # --- Taker fees, general markets (100 contracts) ---
    def test_taker_100_contracts_at_50_cents(self):
        """Official table: 100 contracts at $0.50 → $1.75 fee (0.07*100*0.25=1.75, no rounding)."""
        assert pm.kalshi_fee("KXCPI-26MAY", contracts=100, price=0.50, is_taker=True) == pytest.approx(1.75)

    def test_taker_100_contracts_at_20_cents(self):
        """Official table: 100 contracts at $0.20 → $1.12 fee (0.07*100*0.16=1.12)."""
        assert pm.kalshi_fee("KXCPI-26MAY", contracts=100, price=0.20, is_taker=True) == pytest.approx(1.12)

    def test_taker_100_contracts_at_95_cents(self):
        """Official table: 100 contracts at $0.95 → $0.34 fee (0.07*100*0.0475=0.3325, round up)."""
        assert pm.kalshi_fee("KXCPI-26MAY", contracts=100, price=0.95, is_taker=True) == pytest.approx(0.34)

    # --- Special-fee markets (S&P 500 / NASDAQ-100) ---
    def test_taker_sp500_50_cents(self):
        """Official S&P table: 1 contract at $0.50 → $0.01 fee (0.035*0.25=0.00875, round up)."""
        assert pm.kalshi_fee("KXSP500-26MAY", contracts=1, price=0.50, is_taker=True) == pytest.approx(0.01)

    def test_taker_sp500_100_contracts_at_50_cents(self):
        """Official S&P table: 100 contracts at $0.50 → $0.88 fee (0.035*100*0.25=0.875, round up)."""
        assert pm.kalshi_fee("KXSP500-26MAY", contracts=100, price=0.50, is_taker=True) == pytest.approx(0.88)

    # --- Maker fees ---
    def test_maker_100_contracts_at_50_cents(self):
        """Maker: 0.0175*100*0.25=0.4375, round up to $0.44."""
        assert pm.kalshi_fee("KXCPI-26MAY", contracts=100, price=0.50, is_taker=False) == pytest.approx(0.44)

    # --- Edge cases ---
    def test_fee_at_zero_price(self):
        """Price=0 means fee=0 (P*(1-P)=0)."""
        assert pm.kalshi_fee("KXCPI", contracts=100, price=0.0, is_taker=True) == pytest.approx(0.0)

    def test_fee_at_one_price(self):
        """Price=1 means fee=0."""
        assert pm.kalshi_fee("KXCPI", contracts=100, price=1.0, is_taker=True) == pytest.approx(0.0)

    def test_zero_contracts_raises(self):
        """Can't have a trade with zero contracts."""
        with pytest.raises(ValueError):
            pm.kalshi_fee("KXCPI", contracts=0, price=0.50, is_taker=True)


# ===========================================================================
# CLV — closing line value
# ===========================================================================

class TestCLV:
    """
    CLV = change in de-vigged fair mid between scan time and near-close,
          sign-adjusted for the direction you bought.

    YES direction: positive CLV = fair mid moved UP after you entered = good
    NO direction:  positive CLV = fair mid moved DOWN after you entered = good
    """

    def test_yes_line_moved_in_favor(self):
        """Bought YES when fair was 0.40, close fair is 0.55 → +0.15 CLV."""
        clv = pm.clv(direction="YES", fair_mid_at_scan=0.40, fair_mid_at_close=0.55)
        assert clv == pytest.approx(0.15)

    def test_yes_line_moved_against(self):
        """Bought YES when fair was 0.40, close fair is 0.30 → -0.10 CLV."""
        clv = pm.clv(direction="YES", fair_mid_at_scan=0.40, fair_mid_at_close=0.30)
        assert clv == pytest.approx(-0.10)

    def test_no_line_moved_in_favor(self):
        """Bought NO when fair was 0.60, close fair is 0.45 → +0.15 CLV (for NO)."""
        clv = pm.clv(direction="NO", fair_mid_at_scan=0.60, fair_mid_at_close=0.45)
        assert clv == pytest.approx(0.15)

    def test_no_line_moved_against(self):
        """Bought NO when fair was 0.60, close fair is 0.70 → -0.10 CLV."""
        clv = pm.clv(direction="NO", fair_mid_at_scan=0.60, fair_mid_at_close=0.70)
        assert clv == pytest.approx(-0.10)

    def test_no_change_zero_clv(self):
        clv = pm.clv(direction="YES", fair_mid_at_scan=0.50, fair_mid_at_close=0.50)
        assert clv == pytest.approx(0.0)


# ===========================================================================
# INTEGRATION — a full trade lifecycle
# ===========================================================================

class TestFullTradeLifecycle:
    """
    Example: market KXCPI-26MAY-T0.4
    Scan time: yes_bid=0.62, yes_ask=0.65, no_bid=0.33, no_ask=0.36
    Claude predicts p_yes=0.75
    → Edge = 0.75 - fair_mid ≈ real alpha
    → Direction = YES (since 0.75 > fair_mid)
    → Fill at yes_ask = 0.65, pay 1 contract
    → Market settles YES (outcome_yes=1)
    → Near-close fair_mid = 0.90
    """

    def test_full_cycle(self):
        yes_ask = 0.65
        no_ask = 0.36

        fair_mid = pm.devig_multiplicative(yes_ask=yes_ask, no_ask=no_ask)
        # 0.65 / (0.65+0.36) = 0.65/1.01 ≈ 0.6436
        assert fair_mid == pytest.approx(0.65 / 1.01)

        fill = pm.fill_price(direction="YES", yes_ask=yes_ask, no_ask=no_ask)
        assert fill == 0.65

        gross = pm.gross_pnl(direction="YES", fill_price=fill, outcome_yes=1)
        assert gross == pytest.approx(0.35)

        fee = pm.kalshi_fee("KXCPI-26MAY-T0.4", contracts=1, price=fill, is_taker=True)
        # 0.07 * 1 * 0.65 * 0.35 = 0.0159 → round up to 0.02
        assert fee == pytest.approx(0.02)

        net = gross - fee
        assert net == pytest.approx(0.33)

        # CLV: bought YES at fair 0.6436, near-close fair 0.90 → CLV = 0.256
        clv_val = pm.clv(direction="YES", fair_mid_at_scan=fair_mid, fair_mid_at_close=0.90)
        assert clv_val == pytest.approx(0.90 - fair_mid)

    def test_loss_cycle(self):
        """Same setup but outcome=NO. Lose fill + pay fee."""
        fee = pm.kalshi_fee("KXCPI", contracts=1, price=0.65, is_taker=True)
        gross = pm.gross_pnl(direction="YES", fill_price=0.65, outcome_yes=0)
        assert gross == pytest.approx(-0.65)
        net = gross - fee
        assert net == pytest.approx(-0.67)
