"""
Unit tests for scanner/score.py.

Mocks the LLM clients so we never spend real money during pytest.
Separate integration test script runs the live path.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import json
import pytest

from scanner import score
from core.schema import (
    EnrichedMarket, MarketSnapshot, MarketStatus, Category, ModelName,
    ModelOutput, Confidence, Direction, SCHEMA_VERSION,
)


def _em(ticker="KXCPI-26MAY-T0.4", volume_24h=1000.0, minutes_to_close=7*1440):
    snap = MarketSnapshot(
        ticker=ticker, event_ticker="KXCPI-26MAY",
        title=f"Market {ticker}", status=MarketStatus.ACTIVE,
        yes_bid_cents=18, yes_ask_cents=21, no_bid_cents=79, no_ask_cents=82,
        last_price_cents=20, volume=volume_24h*2, volume_24h=volume_24h, open_interest=500,
        open_time_utc="2026-02-20T15:00:00Z",
        close_time_utc="2026-06-10T12:25:00Z",
        captured_at_utc="2026-04-18T15:00:00Z",
        category_raw="Economics", raw_json={},
    )
    return EnrichedMarket(
        snapshot=snap, fair_prob_yes=0.2039,
        minutes_to_close=minutes_to_close, hours_since_open=1417.0,
        category=Category.MACRO,
    )


def _mock_output():
    return ModelOutput(
        model_prob_yes=0.25, prob_range_lo=0.18, prob_range_hi=0.35,
        confidence=Confidence.MEDIUM, catalyst="cpi gap 0.67 stdev",
        category=Category.MACRO, reasoning="stepwise",
    )


class TestSelectCandidates:
    def test_limits_to_max_n(self):
        ems = [_em(ticker=f"T{i}", volume_24h=i*100) for i in range(100)]
        out = score.select_candidates(ems, max_n=10, max_days=14)
        assert len(out) == 10
        # Top-10 by volume means the highest-numbered ones
        assert [e.ticker for e in out] == [f"T{i}" for i in range(99, 89, -1)]

    def test_excludes_beyond_max_days(self):
        near = _em(ticker="NEAR", minutes_to_close=3*1440)
        far  = _em(ticker="FAR",  minutes_to_close=30*1440)
        out = score.select_candidates([near, far], max_n=10, max_days=14)
        assert [e.ticker for e in out] == ["NEAR"]

    def test_sorted_by_volume_desc(self):
        low  = _em(ticker="LOW",  volume_24h=10)
        high = _em(ticker="HIGH", volume_24h=1000)
        out = score.select_candidates([low, high], max_n=10, max_days=14)
        assert [e.ticker for e in out] == ["HIGH", "LOW"]


class TestScoreOne:
    def test_produces_valid_prediction(self):
        em = _em()
        mock_call = MagicMock(return_value=_mock_output())
        pred = score.score_one(
            em, ModelName.CLAUDE, "claude-test-version",
            mock_call,
            scan_id="abcd1234-efgh-5678-ijkl-mnop90qrstuv",
            scan_ts_utc="2026-04-18T15:00:00Z",
        )
        assert pred is not None
        assert pred.model == ModelName.CLAUDE
        assert pred.model_version == "claude-test-version"
        assert pred.direction == Direction.YES
        assert pred.fill_price == 0.21  # yes_ask 21c -> 0.21
        assert pred.ticker == em.ticker
        assert pred.schema_version == SCHEMA_VERSION
        assert len(pred.prompt_hash_hex) == 16
        assert pred.output.model_prob_yes == 0.25

    def test_none_on_llm_failure(self):
        em = _em()
        mock_call = MagicMock(return_value=None)
        pred = score.score_one(
            em, ModelName.CLAUDE, "v", mock_call,
            scan_id="abcd1234-efgh-5678-ijkl-mnop90qrstuv",
            scan_ts_utc="2026-04-18T15:00:00Z",
        )
        assert pred is None

    def test_none_on_exception(self):
        em = _em()
        mock_call = MagicMock(side_effect=RuntimeError("blown up"))
        pred = score.score_one(
            em, ModelName.CLAUDE, "v", mock_call,
            scan_id="abcd1234-efgh-5678-ijkl-mnop90qrstuv",
            scan_ts_utc="2026-04-18T15:00:00Z",
        )
        assert pred is None

    def test_model_name_always_stamped_by_orchestrator(self):
        """v1 label bug: the LLM's claim about its own identity cannot make it into the record."""
        em = _em()
        # The mock's ModelOutput doesn't have a 'model' field, so LLMs can't set it.
        # score_one always uses the ModelName passed in.
        mock_call = MagicMock(return_value=_mock_output())
        for mn in [ModelName.CLAUDE, ModelName.GROK, ModelName.GEMINI_SHADOW]:
            pred = score.score_one(
                em, mn, "version-string", mock_call,
                scan_id="abcd1234-efgh-5678-ijkl-mnop90qrstuv",
                scan_ts_utc="2026-04-18T15:00:00Z",
            )
            assert pred.model == mn


class TestScoreParallel:
    def test_all_markets_all_models(self):
        ems = [_em(ticker=f"T{i}") for i in range(3)]
        with patch("scanner.score._MODEL_DISPATCH", [
            (ModelName.CLAUDE, "cv", lambda em: _mock_output()),
            (ModelName.GROK,   "gv", lambda em: _mock_output()),
        ]):
            preds = score.score_parallel(
                ems,
                scan_id="abcd1234-efgh-5678-ijkl-mnop90qrstuv",
                scan_ts_utc="2026-04-18T15:00:00Z",
                max_workers=2,
            )
        assert len(preds) == 6
        assert sum(1 for p in preds if p.model == ModelName.CLAUDE) == 3
        assert sum(1 for p in preds if p.model == ModelName.GROK) == 3

    def test_individual_failures_isolated(self):
        ems = [_em(ticker=f"T{i}") for i in range(3)]

        call_count = {"n": 0}
        def flaky(em):
            call_count["n"] += 1
            # Fail only the second call
            if call_count["n"] == 2:
                return None
            return _mock_output()

        with patch("scanner.score._MODEL_DISPATCH", [
            (ModelName.CLAUDE, "cv", flaky),
        ]):
            preds = score.score_parallel(
                ems,
                scan_id="abcd1234-efgh-5678-ijkl-mnop90qrstuv",
                scan_ts_utc="2026-04-18T15:00:00Z",
                max_workers=1,  # serial so call_count is deterministic
            )
        # 3 markets, one failure -> 2 preds
        assert len(preds) == 2


class TestWritePredictions:
    def test_jsonl_round_trip(self, tmp_path):
        em = _em()
        pred = score.score_one(
            em, ModelName.CLAUDE, "v", lambda x: _mock_output(),
            scan_id="abcd1234-efgh-5678-ijkl-mnop90qrstuv",
            scan_ts_utc="2026-04-18T15:00:00Z",
        )
        out = tmp_path / "scan.jsonl"
        score.write_predictions([pred], out)
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["ticker"] == em.ticker
        assert data["model"] == "claude"
        assert data["output"]["model_prob_yes"] == 0.25
