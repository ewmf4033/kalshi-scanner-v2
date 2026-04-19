"""Unit tests for alert/digest.py — mocks httpx, no real network."""

from datetime import date
from unittest.mock import patch, MagicMock

from alert import digest
from core.schema import (
    Alert, AlertTier, Direction, Prediction, ModelOutput, ModelName,
    Confidence, Category, MarketSnapshot, MarketStatus, SCHEMA_VERSION,
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
        model=model, model_version="v",
        prompt_version="scanner-v2.0", prompt_hash_hex="abcd1234efgh5678",
        fee_schedule_version=config.FEE_SCHEDULE_VERSION,
        ticker=ticker, market_snapshot=_snap(ticker),
        direction=Direction.YES, fill_price=0.21, fair_mid_devigged=0.20,
        output=ModelOutput(
            model_prob_yes=prob, prob_range_lo=lo, prob_range_hi=hi,
            confidence=conf, catalyst="x", category=Category.MACRO,
        ),
        correlation_cluster=None, resolution_date_declared=None,
    )


def _alert(tier=AlertTier.CLAUDE_SOLO, direction=Direction.YES,
           ticker="KXTEST", edge=10):
    return Alert(
        scan_id="abcd1234-efgh-5678-ijkl-mnop90qrstuv",
        scan_date=date(2026, 4, 18),
        alert_ts_utc="2026-04-18T15:00:00Z",
        tier=tier, ticker=ticker, direction=direction,
        fill_price=0.21, max_acceptable_price=0.23,
        edge_cents=edge, consensus_prob_yes=0.30, fair_mid_devigged=0.20,
        source_scan_ids=["abcd1234-efgh-5678-ijkl-mnop90qrstuv"],
        reasoning="x",
    )


class TestFormatDigest:
    def test_includes_scan_metadata(self):
        msg = digest.format_digest(
            scan_id="abc", scan_ts_utc="2026-04-19T13:00:00Z",
            candidates_considered=6000, candidates_selected=50,
            predictions=[], alerts=[], failures_by_model={},
        )
        assert "2026-04-19" in msg
        assert "6,000" in msg
        assert "50" in msg

    def test_alert_tier_breakdown(self):
        alerts = [
            _alert(tier=AlertTier.CONSENSUS, ticker="A"),
            _alert(tier=AlertTier.CONSENSUS, ticker="B"),
            _alert(tier=AlertTier.CLAUDE_SOLO, ticker="C"),
            _alert(tier=AlertTier.NONE, ticker="D"),
        ]
        msg = digest.format_digest(
            scan_id="x", scan_ts_utc="2026-04-19T13:00:00Z",
            candidates_considered=100, candidates_selected=4,
            predictions=[], alerts=alerts, failures_by_model={},
        )
        assert "🟢 2 CONSENSUS" in msg
        assert "🔵 1 CLAUDE_SOLO" in msg
        assert "⚪ 1 NONE" in msg

    def test_top_edges_sorted_desc(self):
        alerts = [
            _alert(tier=AlertTier.CLAUDE_SOLO, ticker="LOW", edge=5),
            _alert(tier=AlertTier.CONSENSUS, ticker="HIGH", edge=20),
            _alert(tier=AlertTier.CLAUDE_SOLO, ticker="MID", edge=10),
        ]
        msg = digest.format_digest(
            scan_id="x", scan_ts_utc="2026-04-19T13:00:00Z",
            candidates_considered=10, candidates_selected=3,
            predictions=[], alerts=alerts, failures_by_model={},
        )
        # First alert listed should be HIGH
        idx_high = msg.index("HIGH")
        idx_mid = msg.index("MID")
        idx_low = msg.index("LOW")
        assert idx_high < idx_mid < idx_low

    def test_model_contribution_includes_no_edge_rate(self):
        # 3 Claude real, 1 Claude no-edge -> 25% no-edge
        preds = [
            _pred(ModelName.CLAUDE, 0.85, 0.78, 0.92),
            _pred(ModelName.CLAUDE, 0.20, 0.15, 0.25),
            _pred(ModelName.CLAUDE, 0.10, 0.05, 0.15),
            _pred(ModelName.CLAUDE, 0.50, 0.35, 0.65, conf=Confidence.LOW),  # no-edge
        ]
        msg = digest.format_digest(
            scan_id="x", scan_ts_utc="2026-04-19T13:00:00Z",
            candidates_considered=10, candidates_selected=4,
            predictions=preds, alerts=[], failures_by_model={},
        )
        assert "claude: 3 real, 1 no-edge (25%)" in msg

    def test_failures_shown_when_present(self):
        msg = digest.format_digest(
            scan_id="x", scan_ts_utc="2026-04-19T13:00:00Z",
            candidates_considered=10, candidates_selected=5,
            predictions=[], alerts=[],
            failures_by_model={"claude": 2, "grok": 0, "gemini_shadow": 1},
        )
        assert "claude=2" in msg
        assert "gemini_shadow=1" in msg
        assert "grok=0" not in msg  # zero failures hidden

    def test_no_failures_no_warning(self):
        msg = digest.format_digest(
            scan_id="x", scan_ts_utc="2026-04-19T13:00:00Z",
            candidates_considered=10, candidates_selected=5,
            predictions=[], alerts=[], failures_by_model={"claude": 0, "grok": 0},
        )
        assert "⚠️" not in msg


class TestSendDigest:
    @patch.dict("os.environ", {"TELEGRAM_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("httpx.post")
    def test_sends_successfully(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        assert digest.send_digest("test message") is True
        mock_post.assert_called_once()

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_token_returns_false(self):
        assert digest.send_digest("test") is False

    @patch.dict("os.environ", {"TELEGRAM_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("httpx.post")
    def test_400_returns_false(self, mock_post):
        mock_post.return_value = MagicMock(status_code=400, text="Bad Request")
        assert digest.send_digest("test") is False

    @patch.dict("os.environ", {"TELEGRAM_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("httpx.post")
    def test_network_exception_returns_false(self, mock_post):
        mock_post.side_effect = RuntimeError("network down")
        assert digest.send_digest("test") is False
