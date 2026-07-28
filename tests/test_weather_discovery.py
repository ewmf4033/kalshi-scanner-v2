import asyncio

import pytest

from weather_research.discovery import DiscoveryError, discover_definition, market_to_contract
from weather_research.live import LiveConfig, LiveWeatherLogger
from weather_research.models import BucketContract, ThresholdContract, WeatherRule
from weather_research.runner import MarketDefinition


def rule():
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


def market(ticker, *, floor=None, cap=None, status="open", provisional=False):
    return {
        "ticker": ticker,
        "status": status,
        "floor_strike": floor,
        "cap_strike": cap,
        "is_provisional": provisional,
        "title": "diagnostic only",
    }


def test_structured_strikes_map_without_title_parsing():
    lower = market_to_contract(market("LOW", cap=79))
    middle = market_to_contract(market("MID", floor=80, cap=84))
    upper = market_to_contract(market("HIGH", floor=85))
    assert lower == ThresholdContract("LOW", "<=", 79)
    assert middle == BucketContract("MID", 80, 84, True, True)
    assert upper == ThresholdContract("HIGH", ">=", 85)


def test_discovery_rejects_ambiguous_and_provisional_markets():
    result = discover_definition(
        rule(),
        [market("GOOD", floor=90), market("AMBIG"), market("PROV", floor=91, provisional=True)],
    )
    assert result.accepted_tickers == ("GOOD",)
    assert {ticker for ticker, _ in result.rejected} == {"AMBIG", "PROV"}


def test_discovery_fails_closed_when_nothing_is_usable():
    with pytest.raises(DiscoveryError):
        discover_definition(rule(), [market("BAD")])


class FakeKalshi:
    websocket_url = "wss://example.invalid"

    def __init__(self):
        self.rows = [market("DAY1", floor=90)]

    def list_markets(self, *, series_ticker):
        assert series_ticker == "KXHIGHNY"
        return list(self.rows)


def test_catalog_roll_replaces_tickers_and_clears_books(tmp_path):
    fake = FakeKalshi()
    config = LiveConfig(
        definitions={"KXHIGHNY": MarketDefinition(rule())},
        database_path=str(tmp_path / "research.sqlite"),
        auto_discover=True,
        discovery_seconds=30,
    )
    logger = LiveWeatherLogger(config, kalshi=fake)
    assert asyncio.run(logger._refresh_catalog()) is True
    assert config.market_tickers == ["DAY1"]

    logger.runner.quote_first_seen[("DAY1", "yes", 90)] = None
    fake.rows = [market("DAY2", floor=91, status="unopened")]
    assert asyncio.run(logger._refresh_catalog()) is True
    assert config.market_tickers == ["DAY2"]
    assert logger.runner.quote_first_seen == {}
    assert logger.runner.books.books == {}
    logger.store.close()


def test_catalog_unchanged_does_not_reset_state(tmp_path):
    fake = FakeKalshi()
    config = LiveConfig(
        definitions={"KXHIGHNY": MarketDefinition(rule())},
        database_path=str(tmp_path / "research.sqlite"),
        auto_discover=True,
        discovery_seconds=30,
    )
    logger = LiveWeatherLogger(config, kalshi=fake)
    assert asyncio.run(logger._refresh_catalog()) is True
    marker = object()
    logger.runner.books.books["DAY1"] = marker
    assert asyncio.run(logger._refresh_catalog()) is False
    assert logger.runner.books.books["DAY1"] is marker
    logger.store.close()
