from datetime import datetime, timezone

from weather_research.models import WeatherRule
from weather_research.observations import (
    StationObservation,
    recompute_all_candidate_extremes,
    rounding_candidates,
)
from weather_research.runner import MarketDefinition, WeatherResearchRunner
from weather_research.storage import ResearchStore


def test_cff_candidate_rounds_back_to_cli_integer_grid():
    candidates = rounding_candidates(32.4)
    assert candidates.temperature_f_round_cf == 90.0
    assert candidates.temperature_f_round_c == 89.6
    assert candidates.temperature_f_round_cff == 90.0


def test_three_candidate_extremes_are_independent():
    rows = [
        StationObservation("KNYC", datetime(2026, 7, 27, 18, tzinfo=timezone.utc), 31.9),
        StationObservation("KNYC", datetime(2026, 7, 27, 19, tzinfo=timezone.utc), 32.4),
    ]
    assert recompute_all_candidate_extremes(rows, "daily_high") == (90.0, 89.6, 90.0)


def test_storage_persists_third_point_and_running_extreme(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    rule = WeatherRule("S", "KNYC", "America/New_York", "daily_high", "nearest_int", "final", "NWS")
    runner = WeatherResearchRunner({"S": MarketDefinition(rule)}, store)
    rows = [
        StationObservation("KNYC", datetime(2026, 7, 27, 18, tzinfo=timezone.utc), 31.9),
        StationObservation("KNYC", datetime(2026, 7, 27, 19, tzinfo=timezone.utc), 32.4),
    ]
    runner.ingest_day_observations("S", rows)
    row = store.conn.execute(
        "SELECT temperature_f_round_cff,running_extreme_cff,selected_temperature_f "
        "FROM observations"
    ).fetchone()
    assert row == (90.0, 90.0, 90.0)
    store.close()


def test_reconciliation_tracks_cff_separately(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    runner = WeatherResearchRunner({}, store)
    runner.reconcile_day(
        station_id="KNYC",
        date="2026-07-27",
        parsed_cf_value=89,
        parsed_c_value=89.6,
        parsed_cff_value=90,
        selected_parsed_value=89,
        settled_value=90,
        signal_fired=False,
        would_have_filled=False,
    )
    assert store.reconciliation_counts(candidate="cf") == (1, 1)
    assert store.reconciliation_counts(candidate="c") == (1, 1)
    assert store.reconciliation_counts(candidate="cff") == (1, 0)
    assert store.reconciliation_counts(candidate="selected") == (1, 1)
    store.close()
