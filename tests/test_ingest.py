"""
Unit tests for scanner/ingest.py.

Tests the tradeability filter + enrichment pipeline. Each filter rejection
reason has a dedicated test. Passing case exercises the full enrichment.

No network calls — all snapshots constructed inline.
"""

from datetime import datetime, timedelta, timezone
import pytest

from core.schema import (
    MarketSnapshot, MarketStatus, Category, FilterReason, EnrichedMarket
)
from scanner import ingest


# ---------------------------------------------------------------------------
# Fixture helper: build a MarketSnapshot that would pass every filter.
# Individual tests override one field to trigger a specific rejection.
# ---------------------------------------------------------------------------

NOW = "2026-04-18T15:00:00Z"


def _snap(**overrides) -> MarketSnapshot:
    """Build a reference MarketSnapshot that passes all tradeability checks."""
    defaults = dict(
        ticker="KXCPI-26MAY-T0.4",
        event_ticker="KXCPI-26MAY",
        title="CPI > 0.4% in May 2026",
        status=MarketStatus.ACTIVE,
        yes_bid_cents=45,
        yes_ask_cents=48,    # spread=3c, mid=46.5c
        no_bid_cents=51,
        no_ask_cents=54,
        last_price_cents=47,
        volume=500.0,
        volume_24h=150.0,    # >= 25
        open_interest=500,   # >= 75
        open_time_utc="2026-04-01T15:00:00Z",  # 17 days ago, well past 2h
        close_time_utc="2026-05-12T12:00:00Z", # 24 days out, well past 60min
        captured_at_utc=NOW,
        category_raw="Economics",
        raw_json={},
    )
    defaults.update(overrides)
    return MarketSnapshot(**defaults)


# ===========================================================================
# PASS CASE
# ===========================================================================

class TestFilterPasses:
    def test_healthy_market_passes_and_enriches(self):
        snap = _snap()
        reason, enriched = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.PASSED
        assert enriched is not None
        assert isinstance(enriched, EnrichedMarket)
        assert enriched.ticker == "KXCPI-26MAY-T0.4"
        assert 0.0 < enriched.fair_prob_yes < 1.0
        assert enriched.minutes_to_close > 60
        assert enriched.hours_since_open > 2.0
        assert enriched.category == Category.MACRO

    def test_fair_prob_yes_is_devigged_not_raw_mid(self):
        """The v1 implied_prob-overwrite bug: raw mid was used as probability.
        In a symmetric-vig market (yes_ask=48, no_ask=54), raw mid is 0.465
        but multiplicative de-vig gives yes_ask/(yes_ask+no_ask) = 48/102 ≈ 0.4706."""
        snap = _snap(yes_ask_cents=48, no_ask_cents=54)
        reason, enriched = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.PASSED
        # Raw mid would be 0.465; de-vigged is 48/(48+54) = 0.4706
        assert enriched.fair_prob_yes == pytest.approx(48/102, abs=1e-4)
        assert enriched.fair_prob_yes != pytest.approx(0.465, abs=1e-4)


# ===========================================================================
# INDIVIDUAL REJECTION REASONS
# ===========================================================================

class TestFilterRejections:
    def test_spread_too_wide(self):
        # spread = 11c, threshold is 10c
        snap = _snap(yes_bid_cents=40, yes_ask_cents=51)
        reason, enriched = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.SPREAD_TOO_WIDE
        assert enriched is None

    def test_spread_exactly_at_threshold_passes(self):
        # spread = 10c exactly, threshold says <= 10c
        snap = _snap(yes_bid_cents=40, yes_ask_cents=50, no_bid_cents=49, no_ask_cents=52)
        reason, enriched = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.PASSED, f"got {reason}"

    def test_low_liquidity_both_below_threshold(self):
        # volume_24h=20 (< 25) AND open_interest=50 (< 75) -> rejected
        snap = _snap(volume_24h=20.0, open_interest=50)
        reason, enriched = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.LOW_LIQUIDITY
        assert enriched is None

    def test_liquidity_passes_on_volume_alone(self):
        # vol_24h=25 (meets), OI=10 (fails) -> passes via OR
        snap = _snap(volume_24h=25.0, open_interest=10)
        reason, _ = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.PASSED

    def test_liquidity_passes_on_oi_alone(self):
        # vol_24h=0 (fails), OI=75 (meets) -> passes via OR
        snap = _snap(volume_24h=0.0, open_interest=75)
        reason, _ = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.PASSED

    def test_price_too_low(self):
        # mid = 2c, threshold is 3c
        snap = _snap(yes_bid_cents=1, yes_ask_cents=3, no_bid_cents=97, no_ask_cents=99)
        reason, enriched = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.PRICE_OUT_OF_RANGE
        assert enriched is None

    def test_price_too_high(self):
        # mid = 98c, threshold is 97c
        snap = _snap(yes_bid_cents=97, yes_ask_cents=99, no_bid_cents=1, no_ask_cents=3)
        reason, enriched = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.PRICE_OUT_OF_RANGE
        assert enriched is None

    def test_too_close_to_settlement(self):
        # Market closes in 30 minutes, threshold is 60
        snap = _snap(close_time_utc="2026-04-18T15:30:00Z")  # 30 min after NOW
        reason, enriched = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.TOO_CLOSE_TO_SETTLEMENT
        assert enriched is None

    def test_already_closed(self):
        # Market closed 1 hour ago
        snap = _snap(close_time_utc="2026-04-18T14:00:00Z")
        reason, enriched = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.TOO_CLOSE_TO_SETTLEMENT
        assert enriched is None

    def test_too_new(self):
        # Market opened 1 hour ago, threshold is 2h
        snap = _snap(open_time_utc="2026-04-18T14:00:00Z")
        reason, enriched = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.TOO_NEW
        assert enriched is None

    def test_opened_exactly_at_threshold_passes(self):
        # Opened exactly 2h ago
        snap = _snap(open_time_utc="2026-04-18T13:00:00Z")
        reason, _ = ingest.evaluate(snap, now_utc=NOW)
        assert reason == FilterReason.PASSED

    def test_devig_failure(self):
        # yes_ask and no_ask both 0 — degenerate market, can't de-vig
        # This is the kind of junk market Kalshi surfaces (the KXMVE* markets)
        snap = _snap(yes_bid_cents=0, yes_ask_cents=0, no_bid_cents=0, no_ask_cents=0)
        reason, enriched = ingest.evaluate(snap, now_utc=NOW)
        # Will fail on price_out_of_range (mid=0 < 3), not devig
        # That's the intended ordering — price check comes first, cheaper
        assert reason == FilterReason.PRICE_OUT_OF_RANGE
        assert enriched is None


# ===========================================================================
# CATEGORY MAPPING
# ===========================================================================

class TestCategoryMapping:
    @pytest.mark.parametrize("raw, expected", [
        ("Economics", Category.MACRO),
        ("economics", Category.MACRO),
        ("Climate and Weather", Category.WEATHER),
        ("weather", Category.WEATHER),
        ("Politics", Category.POLITICS),
        ("Crypto", Category.CRYPTO),
        ("Commodities", Category.COMMODITIES),
        ("Oil", Category.COMMODITIES),
        ("Technology", Category.TECH),
        ("Tech", Category.TECH),
        ("Unknown Kalshi Category", Category.OTHER),
        ("", Category.OTHER),
    ])
    def test_raw_to_enum(self, raw, expected):
        assert ingest.categorize(raw) == expected


# ===========================================================================
# BATCH PROCESSING
# ===========================================================================

class TestBatchFilter:
    def test_filter_batch_yields_only_passing_and_collects_stats(self):
        snaps = [
            _snap(ticker="T-PASS1"),                              # passes
            _snap(ticker="T-SPREAD", yes_bid_cents=20, yes_ask_cents=40),  # fails spread
            _snap(ticker="T-LIQ", volume_24h=0, open_interest=0),        # fails liquidity
            _snap(ticker="T-PASS2", event_ticker="KXNFP-26MAY"),        # passes
        ]
        enriched_list, stats = ingest.filter_and_enrich_batch(snaps, now_utc=NOW)
        assert len(enriched_list) == 2
        assert {e.ticker for e in enriched_list} == {"T-PASS1", "T-PASS2"}
        assert stats[FilterReason.PASSED] == 2
        assert stats[FilterReason.SPREAD_TOO_WIDE] == 1
        assert stats[FilterReason.LOW_LIQUIDITY] == 1

    def test_empty_batch_returns_empty_results(self):
        enriched_list, stats = ingest.filter_and_enrich_batch([], now_utc=NOW)
        assert enriched_list == []
        assert all(v == 0 for v in stats.values())
