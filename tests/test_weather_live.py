from datetime import datetime, timedelta, timezone

import pytest

from weather_research.book_state import OrderBookState
from weather_research.models import ThresholdContract, WeatherRule
from weather_research.observations import (
    StationObservation,
    apply_rule_rounding,
    climatological_date,
    local_day_window,
    recompute_day_extreme,
)
from weather_research.runner import MarketDefinition, WeatherResearchRunner
from weather_research.storage import ResearchStore
from weather_research.ws_protocol import SequenceGapError


def snapshot(seq=1):
    return {
        "type": "orderbook_snapshot", "sid": 2, "seq": seq,
        "msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.0600", "80.00"], ["0.0700", "20.00"]],
            "no_dollars_fp": [["0.9000", "12.00"], ["0.9400", "60.00"]],
        },
    }


def test_snapshot_uses_unified_yes_scale_without_complementing_no():
    book = OrderBookState().apply(snapshot())
    assert book.yes_bid_cents == 7
    assert book.yes_ask_cents == 90
    assert book.yes_ask_size == 12


def test_delta_fixed_point_and_gap_poisoning():
    state = OrderBookState()
    state.apply(snapshot())
    book = state.apply({
        "type": "orderbook_delta", "sid": 2, "seq": 2,
        "msg": {"market_ticker": "T", "price_dollars": "0.0500", "delta_fp": "10.00", "side": "yes"},
    })
    assert book.yes_bid_cents == 7
    with pytest.raises(SequenceGapError):
        state.apply({
            "type": "orderbook_delta", "sid": 2, "seq": 4,
            "msg": {"market_ticker": "T", "price_dollars": "0.0800", "delta_fp": "5.00", "side": "yes"},
        })
    assert state.books == {}


def test_runner_threshold_is_derived_from_reconciliation_evidence(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    runner = WeatherResearchRunner({}, store, safety_margin_cents=0)
    empty_gap = runner.required_gap(90, 100)
    for day in range(1, 241):
        runner.reconcile_day(
            station_id="KNYC", date=f"2026-01-{day:03d}", parsed_value=80,
            settled_value=80, signal_fired=False, would_have_filled=False,
        )
    powered_gap = runner.required_gap(90, 100)
    assert empty_gap > 100
    assert powered_gap < 3
    store.close()


def test_reconciliation_cohorts_are_separate(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    runner = WeatherResearchRunner({}, store)
    runner.reconcile_day(
        station_id="A", date="2026-07-01", parsed_value=80, settled_value=80,
        signal_fired=False, would_have_filled=False,
    )
    runner.reconcile_day(
        station_id="A", date="2026-07-02", parsed_value=80, settled_value=81,
        signal_fired=True, would_have_filled=True,
    )
    bounds = runner.reconciliation_bounds()
    assert set(bounds) == {"baseline", "signal", "fill"}
    assert bounds["signal"] == bounds["fill"]
    store.close()


def test_observation_and_book_emit_read_only_signal(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    rule = WeatherRule("S", "KNYC", "America/New_York", "daily_high", "nearest_int", "final", "NWS")
    runner = WeatherResearchRunner(
        {"S": MarketDefinition(rule, thresholds=(ThresholdContract("T", ">=", 80),))}, store
    )
    runner.ingest_book_message(snapshot())
    signals = runner.ingest_observation("KNYC", 81, datetime.now(timezone.utc))
    assert signals and signals[0].ticker == "T"
    assert store.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
    store.close()


def test_running_extreme_resets_by_local_climatological_date(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    rule = WeatherRule("S", "KNYC", "America/New_York", "daily_high", "nearest_int", "final", "NWS")
    runner = WeatherResearchRunner(
        {"S": MarketDefinition(rule, thresholds=(ThresholdContract("T", ">=", 90),))}, store
    )
    runner.ingest_book_message(snapshot())
    day_one = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)
    day_two = datetime(2026, 7, 28, 5, 5, tzinfo=timezone.utc)
    runner.ingest_observation("KNYC", 95, day_one)
    signals = runner.ingest_observation("KNYC", 68, day_two)
    assert climatological_date(day_one, rule.timezone) != climatological_date(day_two, rule.timezone)
    assert runner.running_extremes[("S", climatological_date(day_two, rule.timezone))] == 68
    assert signals == []
    store.close()


def test_lst_climate_date_does_not_follow_summer_dst_midnight():
    instant = datetime(2026, 7, 28, 4, 30, tzinfo=timezone.utc)
    civil = climatological_date(instant, "America/New_York")
    standard = climatological_date(
        instant,
        "America/New_York",
        time_basis="local_standard",
        standard_utc_offset_minutes=-300,
    )
    assert civil.isoformat() == "2026-07-28"
    assert standard.isoformat() == "2026-07-27"


def test_lst_runner_keeps_0030_edt_in_previous_climate_day(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    rule = WeatherRule(
        "S", "KNYC", "America/New_York", "daily_high", "nearest_int", "final", "NWS",
        time_basis="local_standard", standard_utc_offset_minutes=-300,
    )
    runner = WeatherResearchRunner(
        {"S": MarketDefinition(rule, thresholds=(ThresholdContract("T", ">=", 90),))}, store
    )
    runner.ingest_book_message(snapshot())
    before_lst_midnight = datetime(2026, 7, 28, 4, 30, tzinfo=timezone.utc)
    runner.ingest_observation("KNYC", 95, before_lst_midnight)
    assert runner.current_dates["S"].isoformat() == "2026-07-27"
    store.close()


def test_local_standard_requires_explicit_offset():
    with pytest.raises(ValueError):
        WeatherRule(
            "S", "KNYC", "America/New_York", "daily_high", "nearest_int", "final", "NWS",
            time_basis="local_standard",
        )


def test_full_day_recompute_recovers_missed_peak():
    rows = [
        StationObservation("KNYC", datetime(2026, 7, 27, 14, tzinfo=timezone.utc), 20.0),
        StationObservation("KNYC", datetime(2026, 7, 27, 18, tzinfo=timezone.utc), 35.0),
        StationObservation("KNYC", datetime(2026, 7, 27, 22, tzinfo=timezone.utc), 25.0),
    ]
    assert recompute_day_extreme(rows, "daily_high", "nearest_int") == 95


def test_rule_rounding_is_applied_at_signal_boundary():
    assert apply_rule_rounding(89.996, "nearest_int") == 90
    assert apply_rule_rounding(89.996, "floor") == 89
    assert apply_rule_rounding(89.001, "ceil") == 90


def test_quote_survival_uses_receipt_time_not_stale_observation_time(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    rule = WeatherRule("S", "KNYC", "America/New_York", "daily_high", "nearest_int", "final", "NWS")
    runner = WeatherResearchRunner(
        {"S": MarketDefinition(rule, thresholds=(ThresholdContract("T", ">=", 80),))},
        store,
        min_survival_seconds=3,
    )
    runner.ingest_book_message(snapshot())
    stale_observed_at = datetime.now(timezone.utc) - timedelta(minutes=8)
    runner.ingest_observation("KNYC", 81, stale_observed_at)
    age = store.conn.execute("SELECT quote_age_seconds FROM signals ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert age < 1
    store.close()


def test_local_day_window_starts_at_station_midnight():
    now = datetime(2026, 7, 28, 5, 5, tzinfo=timezone.utc)
    start, end = local_day_window(now, "America/New_York")
    assert start == datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
    assert end == now


def test_lst_day_window_starts_at_fixed_standard_midnight():
    now = datetime(2026, 7, 28, 5, 5, tzinfo=timezone.utc)
    start, end = local_day_window(
        now,
        "America/New_York",
        time_basis="local_standard",
        standard_utc_offset_minutes=-300,
    )
    assert start == datetime(2026, 7, 28, 5, 0, tzinfo=timezone.utc)
    assert end == now