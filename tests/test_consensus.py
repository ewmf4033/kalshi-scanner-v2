"""Unit tests for synth/consensus.py."""

import pytest
from datetime import date
from synth import consensus
from core.schema import (
    Prediction, ModelOutput, ModelName, Confidence, Category,
    Direction, MarketSnapshot, MarketStatus, SCHEMA_VERSION,
)
from core import config


def _snap(ticker="T1"):
    return MarketSnapshot(
        ticker=ticker, event_ticker="E", title="t", status=MarketStatus.ACTIVE,
        yes_bid_cents=18, yes_ask_cents=21, no_bid_cents=79, no_ask_cents=82,
        last_price_cents=20, volume=500.0, volume_24h=246.0, open_interest=1581,
        open_time_utc="2026-02-20T15:00:00Z",
        close_time_utc="2026-06-10T12:25:00Z",
        captured_at_utc="2026-04-18T15:00:00Z",
        category_raw="Economics", raw_json={},
    )


def _pred(model, prob, lo, hi, conf=Confidence.MEDIUM, ticker="T1"):
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
        market_snapshot=_snap(ticker),
        direction=Direction.YES,
        fill_price=0.21,
        fair_mid_devigged=0.20,
        output=ModelOutput(
            model_prob_yes=prob, prob_range_lo=lo, prob_range_hi=hi,
            confidence=conf, catalyst="x", category=Category.MACRO,
        ),
        correlation_cluster=None,
        resolution_date_declared=None,
    )


class TestNoEdgeDetection:
    def test_exact_no_edge_sentinel_detected(self):
        out = ModelOutput(
            model_prob_yes=0.5, prob_range_lo=0.35, prob_range_hi=0.65,
            confidence=Confidence.LOW, catalyst="x", category=Category.MACRO,
        )
        assert consensus.is_no_edge(out) is True

    def test_near_no_edge_detected(self):
        out = ModelOutput(
            model_prob_yes=0.51, prob_range_lo=0.30, prob_range_hi=0.70,
            confidence=Confidence.LOW, catalyst="x", category=Category.MACRO,
        )
        assert consensus.is_no_edge(out) is True

    def test_low_confidence_but_real_estimate_kept(self):
        """Low confidence + far from 0.5 = real estimate, not no-edge."""
        out = ModelOutput(
            model_prob_yes=0.20, prob_range_lo=0.10, prob_range_hi=0.30,
            confidence=Confidence.LOW, catalyst="x", category=Category.MACRO,
        )
        assert consensus.is_no_edge(out) is False

    def test_medium_confidence_at_50_kept(self):
        """Medium confidence + 0.5 means model thinks it's actually 50/50, not no-edge."""
        out = ModelOutput(
            model_prob_yes=0.5, prob_range_lo=0.40, prob_range_hi=0.60,
            confidence=Confidence.MEDIUM, catalyst="real coinflip", category=Category.MACRO,
        )
        assert consensus.is_no_edge(out) is False


class TestCombinePredictions:
    def test_three_real_votes_weighted_average(self):
        # Claude tight range (high weight), Grok medium, Gemini wide
        preds = [
            _pred(ModelName.CLAUDE,        0.85, 0.80, 0.90, Confidence.HIGH),
            _pred(ModelName.GROK,          0.70, 0.55, 0.85, Confidence.MEDIUM),
            _pred(ModelName.GEMINI_SHADOW, 0.60, 0.40, 0.80, Confidence.LOW),
        ]
        d = consensus.combine_predictions(preds)
        assert d.ticker == "T1"
        assert d.n_votes_total == 3
        assert d.n_votes_used == 3
        # Claude's tight range should pull consensus toward 0.85
        assert 0.78 < d.consensus_prob_yes < 0.86
        # Claude's weight should be highest
        assert d.per_model_weights["claude"] > d.per_model_weights["grok"]
        assert d.per_model_weights["grok"] > d.per_model_weights["gemini_shadow"]

    def test_no_edge_votes_dropped(self):
        """One real Claude prediction + 2 no-edge sentinels -> consensus = Claude alone."""
        preds = [
            _pred(ModelName.CLAUDE,        0.85, 0.80, 0.90, Confidence.HIGH),
            _pred(ModelName.GROK,          0.50, 0.35, 0.65, Confidence.LOW),  # no-edge
            _pred(ModelName.GEMINI_SHADOW, 0.50, 0.35, 0.65, Confidence.LOW),  # no-edge
        ]
        d = consensus.combine_predictions(preds)
        assert d.n_votes_used == 1
        assert "grok" in d.dropped_models
        assert "gemini_shadow" in d.dropped_models
        # Consensus = Claude's value
        assert d.consensus_prob_yes == 0.85

    def test_all_no_edge_returns_none(self):
        preds = [
            _pred(ModelName.CLAUDE,        0.50, 0.35, 0.65, Confidence.LOW),
            _pred(ModelName.GROK,          0.50, 0.35, 0.65, Confidence.LOW),
            _pred(ModelName.GEMINI_SHADOW, 0.50, 0.35, 0.65, Confidence.LOW),
        ]
        d = consensus.combine_predictions(preds)
        assert d.n_votes_used == 0
        assert d.consensus_prob_yes is None
        assert len(d.dropped_models) == 3

    def test_disagreement_widens_range(self):
        """If models disagree, consensus range should reflect that."""
        # Two confident-but-disagreeing predictions
        preds = [
            _pred(ModelName.CLAUDE, 0.30, 0.25, 0.35, Confidence.HIGH),
            _pred(ModelName.GROK,   0.70, 0.65, 0.75, Confidence.HIGH),
        ]
        d = consensus.combine_predictions(preds)
        # Consensus near 0.5 (avg of 0.3 and 0.7)
        assert 0.48 < d.consensus_prob_yes < 0.52
        # Range should reflect the spread (40pp apart) — at least 20pp wide
        width = d.consensus_range_hi - d.consensus_range_lo
        assert width >= 0.30, f"expected disagreement to widen range, got width={width}"

    def test_mixed_ticker_raises(self):
        preds = [_pred(ModelName.CLAUDE, 0.5, 0.4, 0.6, ticker="A"),
                 _pred(ModelName.GROK,   0.5, 0.4, 0.6, ticker="B")]
        with pytest.raises(ValueError, match="ticker"):
            consensus.combine_predictions(preds)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            consensus.combine_predictions([])


class TestCombineScan:
    def test_groups_by_ticker(self):
        preds = []
        for t in ["A", "B", "C"]:
            for m in [ModelName.CLAUDE, ModelName.GROK, ModelName.GEMINI_SHADOW]:
                preds.append(_pred(m, 0.5, 0.4, 0.6, Confidence.MEDIUM, ticker=t))
        decisions = consensus.combine_scan(preds)
        assert len(decisions) == 3
        assert {d.ticker for d in decisions} == {"A", "B", "C"}
        for d in decisions:
            assert d.n_votes_total == 3
