from datetime import datetime, timezone

from weather_research.observations import (
    StationObservation,
    apply_rule_rounding,
    recompute_day_extreme,
    selected_temperature_f,
)


def test_celsius_first_path_selects_79_while_nearest_fahrenheit_selects_78():
    temperature_c = 25.8
    raw_f = temperature_c * 9 / 5 + 32

    assert apply_rule_rounding(raw_f, "nearest_int") == 78.0
    assert apply_rule_rounding(raw_f, "celsius_int_then_convert") == 79.0
    assert selected_temperature_f(temperature_c, "celsius_int_then_convert") == 79.0


def test_full_day_recompute_uses_celsius_first_path():
    observations = [
        StationObservation("KNYC", datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc), 25.2),
        StationObservation("KNYC", datetime(2026, 7, 28, 17, 0, tzinfo=timezone.utc), 25.8),
    ]

    assert recompute_day_extreme(observations, "daily_high", "nearest_int") == 78.0
    assert recompute_day_extreme(
        observations, "daily_high", "celsius_int_then_convert"
    ) == 79.0
