from weather_research.ev import certainty_trade_ev_cents, taker_fee_cents
from weather_research.incentives import IncentiveEconomics, qualifying_score
from weather_research.models import BookTop, BucketContract, ThresholdContract
from weather_research.reconcile import ReconciliationStats
from weather_research.signals import eliminated_bucket_signal, monotonicity_violations, realized_threshold_signal
from weather_research.ws_protocol import orderbook_subscription


def test_subscription_forces_unified_yes_prices():
    msg = orderbook_subscription(1, ["KXHIGHNY-EXAMPLE"])
    assert msg["params"]["use_yes_price"] is True


def test_realized_threshold_signal():
    signal = realized_threshold_signal(
        ThresholdContract("A", ">=", 90),
        BookTop("A", 91, 94, yes_ask_size=150),
        91,
    )
    assert signal is not None and signal.gross_gap_cents == 6


def test_bucket_elimination_is_first_class_no_signal():
    signal = eliminated_bucket_signal(
        BucketContract("B", 85, 89, True, True),
        BookTop("B", 7, 10, yes_bid_size=80),
        90,
    )
    assert signal is not None and signal.side == "no" and signal.executable_price_cents == 93


def test_monotonicity_uses_executable_prices():
    rows = monotonicity_violations(
        [ThresholdContract("L", ">=", 80), ThresholdContract("H", ">=", 90)],
        {"L": BookTop("L", 9, 10, 100, 100), "H": BookTop("H", 12, 14, 50, 50)},
    )
    assert rows[0]["gross_lock_cents"] == 2 and rows[0]["max_size"] == 50


def test_exact_fill_conditional_ev_collapse():
    fee = taker_fee_cents(94, contracts=100)
    assert certainty_trade_ev_cents(94, 0.01, fee) == 5 - fee


def test_reconciliation_power_uses_all_station_days():
    assert ReconciliationStats(total=240, errors=0).wilson_upper() < 0.02


def test_incentive_uses_measured_denominator_plus_reaction_haircut():
    econ = IncentiveEconomics(10_000, 2_000, 500, 0.25, 100)
    assert 0 < econ.expected_net_cents() < econ.expected_reward_cents()
    assert qualifying_score(500, 0, 0.9) == 500
