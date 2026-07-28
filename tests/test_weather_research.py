import pytest

from weather_research.ev import certainty_trade_ev_cents, taker_fee_cents
from weather_research.incentives import IncentiveEconomics, qualifying_score
from weather_research.models import BookTop, BucketContract, ThresholdContract
from weather_research.reconcile import ReconciliationLedger, ReconciliationStats, compare_settlement
from weather_research.signals import eliminated_bucket_signal, monotonicity_violations, realized_threshold_signal
from weather_research.ws_protocol import SequenceGapError, SequenceTracker, orderbook_subscription


def test_subscription_forces_unified_yes_prices():
    msg = orderbook_subscription(1, ["KXHIGHNY-EXAMPLE"])
    assert msg["params"]["use_yes_price"] is True


def test_daily_high_only_locks_upper_comparators():
    locked = realized_threshold_signal(
        ThresholdContract("A", ">=", 90),
        BookTop("A", 91, 94, yes_ask_size=150),
        91,
        "daily_high",
    )
    fabricated = realized_threshold_signal(
        ThresholdContract("B", "<=", 85),
        BookTop("B", 35, 38, yes_ask_size=150),
        71,
        "daily_high",
    )
    assert locked is not None and locked.gross_gap_cents == 6
    assert fabricated is None


def test_daily_low_only_locks_lower_comparators():
    locked = realized_threshold_signal(
        ThresholdContract("A", "<=", 40),
        BookTop("A", 91, 94, yes_ask_size=150),
        39,
        "daily_low",
    )
    wrong_direction = realized_threshold_signal(
        ThresholdContract("B", ">=", 30),
        BookTop("B", 91, 94, yes_ask_size=150),
        39,
        "daily_low",
    )
    assert locked is not None
    assert wrong_direction is None


def test_bucket_elimination_enforces_observation_direction():
    high_signal = eliminated_bucket_signal(
        BucketContract("B", 85, 89, True, True),
        BookTop("B", 7, 10, yes_bid_size=80),
        90,
        "daily_high",
    )
    low_signal = eliminated_bucket_signal(
        BucketContract("C", 32, 36, True, False),
        BookTop("C", 8, 11, yes_bid_size=60),
        31,
        "daily_low",
    )
    assert high_signal is not None and high_signal.side == "no"
    assert low_signal is not None and low_signal.side == "no"


def test_monotonicity_ge_ladder_uses_executable_prices_and_fee_gate():
    rows = monotonicity_violations(
        [ThresholdContract("L", ">=", 80), ThresholdContract("H", ">=", 90)],
        {"L": BookTop("L", 3, 4, 100, 100), "H": BookTop("H", 8, 10, 50, 50)},
    )
    assert rows and rows[0]["gross_lock_cents"] == 4
    assert rows[0]["net_lock_cents"] > 0


def test_monotonicity_le_ladder_does_not_invert_valid_curve():
    rows = monotonicity_violations(
        [
            ThresholdContract("A", "<=", 70),
            ThresholdContract("B", "<=", 80),
            ThresholdContract("C", "<=", 90),
        ],
        {
            "A": BookTop("A", 31, 32, 100, 100),
            "B": BookTop("B", 61, 62, 100, 100),
            "C": BookTop("C", 89, 90, 100, 100),
        },
    )
    assert rows == []


def test_monotonicity_rejects_mixed_comparators():
    with pytest.raises(ValueError):
        monotonicity_violations(
            [ThresholdContract("A", ">=", 70), ThresholdContract("B", "<=", 80)],
            {},
        )


def test_monotonicity_drops_raw_lock_smaller_than_pair_fee():
    rows = monotonicity_violations(
        [ThresholdContract("L", ">=", 80), ThresholdContract("H", ">=", 90)],
        {"L": BookTop("L", 49, 50, 100, 100), "H": BookTop("H", 51, 52, 100, 100)},
    )
    assert rows == []


def test_monotonicity_fee_uses_actual_executable_depth():
    rows = monotonicity_violations(
        [ThresholdContract("L", ">=", 80), ThresholdContract("H", ">=", 90)],
        {"L": BookTop("L", 5, 6, 8, 8), "H": BookTop("H", 10, 12, 8, 8)},
        contracts_per_leg=100,
    )
    assert rows
    assert rows[0]["fee_size"] == 8
    assert rows[0]["pair_fee_cents"] == pytest.approx(1.25)
    assert rows[0]["net_lock_cents"] == pytest.approx(2.75)


def test_exact_fill_conditional_ev_collapse():
    fee = taker_fee_cents(94, contracts=100)
    assert certainty_trade_ev_cents(94, 0.01, fee) == 5 - fee


def test_reconciliation_power_uses_all_station_days():
    assert ReconciliationStats(total=240, errors=0).wilson_upper() < 0.02


def test_reconciliation_uses_half_up_integer_tenths():
    assert compare_settlement(78.05, 78.1)
    assert compare_settlement(78.25, 78.3)
    assert not compare_settlement(78.05, 78.0)


def test_reconciliation_separates_signal_and_would_fill_stats():
    ledger = ReconciliationLedger()
    ledger.add(
        station_id="KNYC",
        date="2026-07-27",
        parsed_value=89.3000000001,
        settled_value=89.3,
        signal_fired=False,
    )
    ledger.add(
        station_id="KNYC",
        date="2026-07-28",
        parsed_value=90.0,
        settled_value=91.0,
        signal_fired=True,
        displayed_depth=10,
        quote_survival_seconds=5,
        gross_gap_cents=12,
        required_gap_cents=10,
    )
    ledger.add(
        station_id="KNYC",
        date="2026-07-29",
        parsed_value=92.0,
        settled_value=93.0,
        signal_fired=True,
        displayed_depth=80,
        quote_survival_seconds=5,
        gross_gap_cents=12,
        required_gap_cents=10,
    )
    assert ledger.stats().total == 3 and ledger.stats().errors == 2
    assert ledger.stats(signal_only=True).total == 2
    assert ledger.stats(signal_only=True).errors == 2
    assert ledger.stats(fill_only=True).total == 1
    assert ledger.stats(fill_only=True).errors == 1
    with pytest.raises(ValueError):
        ledger.stats(signal_only=True, fill_only=True)


def test_sequence_gap_invalidates_book_until_fresh_snapshot():
    tracker = SequenceTracker()
    tracker.accept_snapshot(7, 100)
    tracker.accept_delta(7, 101)
    with pytest.raises(SequenceGapError):
        tracker.accept_delta(7, 103)
    with pytest.raises(SequenceGapError):
        tracker.accept_delta(7, 104)
    tracker.accept_snapshot(7, 200)
    tracker.accept_delta(7, 201)


def test_incentive_uses_measured_denominator_plus_reaction_haircut():
    econ = IncentiveEconomics(10_000, 2_000, 500, 0.25, 100)
    assert 0 < econ.expected_net_cents() < econ.expected_reward_cents()
    assert qualifying_score(500, 0, 0.9) == 500
