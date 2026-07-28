from datetime import datetime, timezone

import pytest

from weather_research.book_state import OrderBookState
from weather_research.models import ThresholdContract, WeatherRule
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
