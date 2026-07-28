from datetime import date, datetime, timezone

from weather_research.discovery import discover_definition
from weather_research.models import BookTop, BucketContract, WeatherRule
from weather_research.runner import MarketDefinition, WeatherResearchRunner
from weather_research.storage import ResearchStore


def rule() -> WeatherRule:
    return WeatherRule(
        series_ticker="KXHIGHNY",
        station_id="KNYC",
        timezone="America/New_York",
        observation_type="daily_high",
        rounding="nearest_int",
        revision_policy="final",
        source_name="NWS CLINYC",
        time_basis="local_standard",
        standard_utc_offset_minutes=-300,
    )


def test_discovery_attaches_event_date_from_ticker():
    result = discover_definition(
        rule(),
        [
            {
                "ticker": "KXHIGHNY-26JUL29-B76.5",
                "status": "active",
                "strike_type": "between",
                "floor_strike": 76,
                "cap_strike": 77,
                "lower_inclusive": None,
                "upper_inclusive": None,
                "subtitle": "76° to 77°",
                "title": "Will the high temp in NYC be 76-77° on Jul 29, 2026?",
                "close_time": "2026-07-30T04:59:00Z",
            }
        ],
        now=datetime(2026, 7, 28, 18, tzinfo=timezone.utc),
        horizon_hours=72,
    )
    assert result.definition.buckets[0].event_date == date(2026, 7, 29)


def test_today_running_high_cannot_eliminate_tomorrows_bucket(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    today = date(2026, 7, 28)
    tomorrow = date(2026, 7, 29)
    ticker = "KXHIGHNY-26JUL29-B76.5"
    contract = BucketContract(ticker, 76, 77, True, True, event_date=tomorrow)
    runner = WeatherResearchRunner(
        {"KXHIGHNY": MarketDefinition(rule(), buckets=(contract,))},
        store,
    )
    runner.books.books[ticker] = BookTop(ticker, 7, 10, yes_bid_size=80, yes_ask_size=80)
    runner.running_extremes[("KXHIGHNY", today)] = 84
    runner.current_dates["KXHIGHNY"] = today

    assert runner._evaluate_ticker(ticker, datetime.now(timezone.utc)) == []
    assert store.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    store.close()


def test_matching_event_date_still_emits_elimination_signal(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    today = date(2026, 7, 28)
    ticker = "KXHIGHNY-26JUL28-B76.5"
    contract = BucketContract(ticker, 76, 77, True, True, event_date=today)
    runner = WeatherResearchRunner(
        {"KXHIGHNY": MarketDefinition(rule(), buckets=(contract,))},
        store,
    )
    runner.books.books[ticker] = BookTop(ticker, 7, 10, yes_bid_size=80, yes_ask_size=80)
    runner.running_extremes[("KXHIGHNY", today)] = 84
    runner.current_dates["KXHIGHNY"] = today

    signals = runner._evaluate_ticker(ticker, datetime.now(timezone.utc))
    assert len(signals) == 1
    assert signals[0].ticker == ticker
    store.close()
