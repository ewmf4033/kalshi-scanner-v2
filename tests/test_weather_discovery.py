import asyncio

import pytest

from weather_research.discovery import (
    DiscoveryError,
    discover_definition,
    market_to_contract,
    validate_bucket_partition,
)
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


def market(
    ticker,
    *,
    strike_type="greater_or_equal",
    floor=None,
    cap=None,
    status="open",
    provisional=False,
    lower_inclusive=None,
    upper_inclusive=None,
):
    row = {
        "ticker": ticker,
        "status": status,
        "strike_type": strike_type,
        "floor_strike": floor,
        "cap_strike": cap,
        "is_provisional": provisional,
        "title": "diagnostic only",
    }
    if lower_inclusive is not None:
        row["lower_inclusive"] = lower_inclusive
    if upper_inclusive is not None:
        row["upper_inclusive"] = upper_inclusive
    return row


def test_explicit_strike_types_map_without_title_or_null_inference():
    assert market_to_contract(market("GT", strike_type="greater", floor=79)) == ThresholdContract("GT", ">", 79)
    assert market_to_contract(market("GE", strike_type="greater_or_equal", floor=80)) == ThresholdContract("GE", ">=", 80)
    assert market_to_contract(market("LT", strike_type="less", cap=84)) == ThresholdContract("LT", "<", 84)
    assert market_to_contract(market("LE", strike_type="less_or_equal", cap=85)) == ThresholdContract("LE", "<=", 85)
    assert market_to_contract(
        market(
            "MID",
            strike_type="between",
            floor=80,
            cap=84,
            lower_inclusive=True,
            upper_inclusive=True,
        )
    ) == BucketContract("MID", 80, 84, True, True)


def test_floor_only_does_not_imply_comparator():
    with pytest.raises(DiscoveryError, match="strike_type"):
        market_to_contract(market("BAD", strike_type="", floor=90))


def test_between_requires_explicit_inclusivity():
    with pytest.raises(DiscoveryError, match="lower_inclusive"):
        market_to_contract(market("BAD", strike_type="between", floor=80, cap=84))


def test_discovery_rejects_ambiguous_and_provisional_markets():
    result = discover_definition(
        rule(),
        [
            market("GOOD", strike_type="greater_or_equal", floor=90),
            market("AMBIG", strike_type="mystery", floor=91),
            market("PROV", strike_type="greater_or_equal", floor=91, provisional=True),
        ],
    )
    assert result.accepted_tickers == ("GOOD",)
    assert {ticker for ticker, _ in result.rejected} == {"AMBIG", "PROV"}


def test_discovery_fails_closed_when_nothing_is_usable():
    with pytest.raises(DiscoveryError):
        discover_definition(rule(), [market("BAD", strike_type="")])


def test_integer_bucket_partition_accepts_adjacent_inclusive_ranges():
    validate_bucket_partition(
        [
            BucketContract("A", 84, 85, True, True),
            BucketContract("B", 86, 87, True, True),
        ]
    )


def test_integer_bucket_partition_rejects_overlap_and_gap():
    with pytest.raises(DiscoveryError, match="overlap"):
        validate_bucket_partition(
            [
                BucketContract("A", 84, 85, True, True),
                BucketContract("B", 85, 86, True, True),
            ]
        )
    with pytest.raises(DiscoveryError, match="gap"):
        validate_bucket_partition(
            [
                BucketContract("A", 84, 85, True, False),
                BucketContract("B", 86, 87, True, True),
            ]
        )


class FakeKalshi:
    websocket_url = "wss://example.invalid"

    def __init__(self):
        self.rows = [market("DAY1", strike_type="greater_or_equal", floor=90)]

    def list_markets(self, *, series_ticker, statuses=("open", "unopened")):
        assert series_ticker == "KXHIGHNY"
        assert tuple(statuses) == ("open", "unopened")
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
    fake.rows = [market("DAY2", strike_type="greater", floor=91, status="unopened")]
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
