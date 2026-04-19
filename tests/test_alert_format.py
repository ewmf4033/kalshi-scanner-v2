"""Unit tests for alert/format.py."""

from datetime import date
import pytest

from alert import format as af
from synth.consensus import Decision, combine_predictions
from core.schema import (
    Alert, AlertTier, Direction, ModelName, Confidence, Category,
    Prediction, ModelOutput, MarketSnapshot, MarketStatus, SCHEMA_VERSION,
)
from core import config


def _snap(yes_bid=18, yes_ask=21, no_bid=79, no_ask=82, ticker="T1"):
    return MarketSnapshot(
        ticker=ticker, event_ticker="E", title="t", status=MarketStatus.ACTIVE,
        yes_bid_cents=yes_bid, yes_ask_cents=yes_ask,
        no_bid_cents=no_bid, no_ask_cents=no_ask,
        last_price_cents=20, volume=500.0, volume_24h=246.0, open_interest=1581,
        open_time_utc="2026-02-20T15:00:00Z",
        close_time_utc="2026-06-10T12:25:00Z",
        captured_at_utc="2026-04-18T15:00:00Z",
        category_raw="Economics", raw_json={},
    )


def _pred(model, prob, lo, hi, conf=Confidence.MEDIUM,
          fair=0.20, ticker="T1", category=Category.MACRO,
          yes_bid=18, yes_ask=21, no_bid=79, no_ask=82):
    return Prediction(
        scan_id="abcd1234-efgh-5678-ijkl-mnop90qrstuv",
        schema_version=SCHEMA_VERSION,
        scan_date=date(2026, 4, 18),
        scan_ts_utc="2026-04-18T15:00:00Z",
        model=model,
        model_version="test-version",
        prompt_version="scanner-v2.0",
        prompt_hash_hex="abcd1234efgh5678",
        fee_schedule_version=config.FEE_SCHEDULE_VERSION,
        ticker=ticker,
        market_snapshot=_snap(yes_bid, yes_ask, no_bid, no_ask, ticker),
        direction=Direction.YES,
        fill_price=yes_ask / 100.0,
        fair_mid_devigged=fair,
        output=ModelOutput(
            model_prob_yes=prob, prob_range_lo=lo, prob_range_hi=hi,
            confidence=conf, catalyst="x", category=category,
        ),
        correlation_cluster=None,
        resolution_date_declared=None,
    )


class TestEdgeThreshold:
    def test_known_categories(self):
        assert af._edge_threshold_cents(Category.MACRO) == config.EDGE_THRESHOLD_CENTS["macro"]
        assert af._edge_threshold_cents(Category.WEATHER) == config.EDGE_THRESHOLD_CENTS["weather"]

    def test_unknown_category_uses_fallback(self):
        # OTHER may not be in EDGE_THRESHOLD_CENTS; should fall back gracefully
        result = af._edge_threshold_cents(Category.OTHER)
        assert result == config.EDGE_THRESHOLD_CENTS.get("other", af._EDGE_FALLBACK_CENTS)


class TestPickDirectionAndFill:
    def test_yes_when_consensus_above_market(self):
        """consensus 0.85 vs market 0.20 -> YES is underpriced, trade YES"""
        d, fill, max_acc = af._pick_direction_and_fill(
            consensus_prob=0.85, market_fair=0.20,
            yes_ask_cents=21, no_ask_cents=82,
        )
        assert d == Direction.YES
        assert fill == 0.21
        assert max_acc == 0.23

    def test_no_when_consensus_below_market(self):
        """consensus 0.10 vs market 0.20 -> YES is overpriced, trade NO"""
        d, fill, max_acc = af._pick_direction_and_fill(
            consensus_prob=0.10, market_fair=0.20,
            yes_ask_cents=21, no_ask_cents=82,
        )
        assert d == Direction.NO
        assert fill == 0.82
        assert max_acc == 0.84

    def test_max_acc_capped_at_99(self):
        d, fill, max_acc = af._pick_direction_and_fill(
            consensus_prob=0.10, market_fair=0.20,
            yes_ask_cents=21, no_ask_cents=98,
        )
        assert max_acc == 0.99

    def test_no_above_half_still_no_when_market_higher(self):
        """The key bug we just fixed: consensus 0.80 < market 0.95 -> NO,
        even though consensus > 0.5."""
        d, _, _ = af._pick_direction_and_fill(
            consensus_prob=0.80, market_fair=0.95,
            yes_ask_cents=80, no_ask_cents=21,
        )
        assert d == Direction.NO

    def test_yes_below_half_when_market_lower(self):
        """consensus 0.20 > market 0.05 -> YES, even though consensus < 0.5."""
        d, _, _ = af._pick_direction_and_fill(
            consensus_prob=0.20, market_fair=0.05,
            yes_ask_cents=21, no_ask_cents=82,
        )
        assert d == Direction.YES


class TestBuildAlertConsensusTier:
    def test_consensus_when_claude_and_grok_agree(self):
        """Both Claude and Grok say YES with strong edge → CONSENSUS tier."""
        # Market fair = 0.20, both models say 0.50 → 30c YES edge
        preds = [
            _pred(ModelName.CLAUDE, 0.50, 0.45, 0.55, Confidence.HIGH, fair=0.20),
            _pred(ModelName.GROK,   0.55, 0.50, 0.60, Confidence.MEDIUM, fair=0.20),
        ]
        decision = combine_predictions(preds)
        alert = af.build_alert(decision, preds)
        assert alert.tier == AlertTier.CONSENSUS
        assert alert.direction == Direction.YES
        assert alert.edge_cents > 0


class TestBuildAlertClaudeSolo:
    def test_claude_solo_when_grok_dropped(self):
        """Claude has edge, Grok no-edged → CLAUDE_SOLO tier."""
        preds = [
            _pred(ModelName.CLAUDE,        0.85, 0.78, 0.90, Confidence.HIGH, fair=0.20),
            _pred(ModelName.GROK,          0.50, 0.35, 0.65, Confidence.LOW,  fair=0.20),  # no-edge
            _pred(ModelName.GEMINI_SHADOW, 0.50, 0.35, 0.65, Confidence.LOW,  fair=0.20),  # no-edge
        ]
        decision = combine_predictions(preds)
        alert = af.build_alert(decision, preds)
        assert alert.tier == AlertTier.CLAUDE_SOLO
        assert alert.direction == Direction.YES
        assert alert.consensus_prob_yes == 0.85
        assert alert.edge_cents == 65  # 85 - 20


class TestBuildAlertNone:
    def test_none_when_all_no_edge(self):
        """All models no-edged → tier NONE, never sent to Telegram."""
        preds = [
            _pred(ModelName.CLAUDE,        0.50, 0.35, 0.65, Confidence.LOW, fair=0.20),
            _pred(ModelName.GROK,          0.50, 0.35, 0.65, Confidence.LOW, fair=0.20),
            _pred(ModelName.GEMINI_SHADOW, 0.50, 0.35, 0.65, Confidence.LOW, fair=0.20),
        ]
        decision = combine_predictions(preds)
        alert = af.build_alert(decision, preds)
        assert alert.tier == AlertTier.NONE

    def test_none_when_edge_below_threshold(self):
        """Small edge below category threshold → NONE."""
        # Macro threshold is 5c. We need consensus close to fair.
        preds = [
            _pred(ModelName.CLAUDE, 0.22, 0.18, 0.26, Confidence.MEDIUM, fair=0.20),  # 2c edge YES
            _pred(ModelName.GROK,   0.21, 0.16, 0.26, Confidence.MEDIUM, fair=0.20),
        ]
        decision = combine_predictions(preds)
        alert = af.build_alert(decision, preds)
        assert alert.tier == AlertTier.NONE
        assert "below threshold" in alert.reasoning


class TestBuildAlertGrokSolo:
    def test_grok_solo_when_claude_disagrees(self):
        """Grok has edge in YES, Claude points NO → GROK_SOLO."""
        # Fair=0.20. Grok says 0.80 (YES), Claude says 0.05 (NO).
        # Inverse-variance combine — depends on ranges.
        # Set Grok with tighter range to win consensus.
        preds = [
            _pred(ModelName.CLAUDE, 0.05, 0.02, 0.10, Confidence.MEDIUM, fair=0.20),  # NO direction
            _pred(ModelName.GROK,   0.80, 0.74, 0.86, Confidence.HIGH,   fair=0.20),  # YES, tight
        ]
        decision = combine_predictions(preds)
        alert = af.build_alert(decision, preds)
        # Consensus likely YES (Grok's tight range dominates)
        # Claude pointed NO; Grok pointed YES; consensus = YES → GROK_SOLO
        assert alert.tier == AlertTier.GROK_SOLO


class TestNoEdgeReasoning:
    def test_reasoning_contains_attribution(self):
        preds = [
            _pred(ModelName.CLAUDE, 0.85, 0.78, 0.90, Confidence.HIGH, fair=0.20),
            _pred(ModelName.GROK,   0.50, 0.35, 0.65, Confidence.LOW,  fair=0.20),
        ]
        decision = combine_predictions(preds)
        alert = af.build_alert(decision, preds)
        assert "claude" in alert.reasoning.lower()
        assert "grok" in alert.reasoning.lower()
        assert "DROPPED" in alert.reasoning  # grok was no-edge


class TestBuildAlertsBatch:
    def test_returns_one_alert_per_decision(self):
        preds_a = [
            _pred(ModelName.CLAUDE, 0.85, 0.78, 0.90, ticker="A"),
            _pred(ModelName.GROK,   0.80, 0.75, 0.85, ticker="A"),
        ]
        preds_b = [
            _pred(ModelName.CLAUDE, 0.50, 0.35, 0.65, Confidence.LOW, ticker="B"),
            _pred(ModelName.GROK,   0.50, 0.35, 0.65, Confidence.LOW, ticker="B"),
        ]
        all_preds = preds_a + preds_b
        decisions = [combine_predictions(preds_a), combine_predictions(preds_b)]
        alerts = af.build_alerts(decisions, all_preds)
        assert len(alerts) == 2
        tickers = {a.ticker for a in alerts}
        assert tickers == {"A", "B"}
