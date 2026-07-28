import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from weather_research.discovery import (
    DiscoveryError,
    discover_definition,
    discover_all,
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
    close_time=None,
    title="diagnostic only",
    subtitle="",
):
    if close_time is None:
        close_time = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    row = {
        "ticker": ticker,
        "status": status,
        "strike_type": strike_type,
        "floor_strike": floor,
        "cap_strike": cap,
        "is_provisional": provisional,
        "close_time": close_time,
        "title": title,
        "subtitle": subtitle,
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
        market("MID", strike_type="between", floor=80, cap=84, lower_inclusive=True, upper_inclusive=True)
    ) == BucketContract("MID", 80, 84, True, True)


def test_authenticated_between_payload_uses_closed_display_range_when_booleans_absent():
    row = market(
        "KXHIGHNY-26JUL28-B77.5",
        strike_type="between",
        floor=77,
        cap=78,
        status="active",
        title="Will the **high temp in NYC** be 77-78° on Jul 28, 2026?",
        subtitle="77° to 78°",
    )
    assert market_to_contract(row) == BucketContract(row["ticker"], 77, 78, True, True)


def test_between_without_booleans_or_exact_display_range_fails_closed():
    with pytest.raises(DiscoveryError, match="display range"):
        market_to_contract(market("BAD", strike_type="between", floor=80, cap=84))


def test_between_with_only_one_inclusivity_field_fails_closed():
    with pytest.raises(DiscoveryError, match="both be booleans"):
        market_to_contract(market("BAD", strike_type="between", floor=80, cap=84, lower_inclusive=True))


def test_floor_only_does_not_imply_comparator():
    with pytest.raises(DiscoveryError, match="strike_type"):
        market_to_contract(market("BAD", strike_type="", floor=90))


def test_discovery_accepts_authenticated_active_status():
    result = discover_definition(rule(), [market("GOOD", strike_type="greater", floor=84, status="active")])
    assert result.accepted_tickers == ("GOOD",)


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


def ladder(event, low, high):
    return [
        market(f"{event}-T{low}", strike_type="less", cap=low, status="active"),
        market(f"{event}-B{low}.5", strike_type="between", floor=low, cap=low + 1, status="active", subtitle=f"{low}° to {low + 1}°"),
        market(f"{event}-B{low + 2}.5", strike_type="between", floor=low + 2, cap=low + 3, status="active", subtitle=f"{low + 2}° to {low + 3}°"),
        market(f"{event}-B{low + 4}.5", strike_type="between", floor=low + 4, cap=low + 5, status="active", subtitle=f"{low + 4}° to {low + 5}°"),
        market(f"{event}-B{low + 6}.5", strike_type="between", floor=low + 6, cap=high, status="active", subtitle=f"{low + 6}° to {high}°"),
        market(f"{event}-T{high}", strike_type="greater", floor=high, status="active"),
    ]


def test_authenticated_kxhighny_ladder_partitions_cleanly():
    result = discover_definition(rule(), ladder("KXHIGHNY-26JUL28", 77, 84))
    assert len(result.definition.buckets) == 4
    assert len(result.definition.thresholds) == 2
    assert result.rejected == ()


def test_multiple_event_dates_validate_independently():
    rows = ladder("KXHIGHNY-26JUL28", 77, 84) + ladder("KXHIGHNY-26JUL29", 76, 83)
    result = discover_definition(rule(), rows)
    assert len(result.accepted_tickers) == 12
    assert len(result.definition.buckets) == 8
    assert len(result.definition.thresholds) == 4
    assert result.rejected == ()


def test_integer_bucket_partition_accepts_adjacent_inclusive_ranges():
    validate_bucket_partition([
        BucketContract("A", 84, 85, True, True),
        BucketContract("B", 86, 87, True, True),
    ])


def test_integer_bucket_partition_rejects_overlap_and_gap():
    with pytest.raises(DiscoveryError, match="overlap"):
        validate_bucket_partition([
            BucketContract("A", 84, 85, True, True),
            BucketContract("B", 85, 86, True, True),
        ])
    with pytest.raises(DiscoveryError, match="gap"):
        validate_bucket_partition([
            BucketContract("A", 84, 85, True, False),
            BucketContract("B", 86, 87, True, True),
        ])


class FakeKalshi:
    websocket_url = "wss://example.invalid"

    def __init__(self):
        self.rows = [market("DAY1", strike_type="greater_or_equal", floor=90)]
        self.calls = []

    def list_markets(self, *, series_ticker, statuses=("open", "unopened")):
        self.calls.append((series_ticker, tuple(statuses)))
        assert series_ticker == "KXHIGHNY"
        assert tuple(statuses) == ("open", "unopened")
        return list(self.rows)


def test_discover_all_pushes_status_filter_into_request():
    fake = FakeKalshi()
    result = discover_all(fake, {"KXHIGHNY": MarketDefinition(rule())})
    assert result["KXHIGHNY"].accepted_tickers == ("DAY1",)
    assert fake.calls == [("KXHIGHNY", ("open", "unopened"))]


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
