"""Unit tests for alert/telegram.py — mocks httpx, no real network."""

from datetime import date
from unittest.mock import patch, MagicMock

from alert import telegram
from core.schema import Alert, AlertTier, Direction


def _alert(tier=AlertTier.CLAUDE_SOLO, direction=Direction.YES,
           ticker="KXTEST-1", edge=10):
    return Alert(
        scan_id="abcd1234-efgh-5678-ijkl-mnop90qrstuv",
        scan_date=date(2026, 4, 18),
        alert_ts_utc="2026-04-18T15:00:00Z",
        tier=tier,
        ticker=ticker,
        direction=direction,
        fill_price=0.21,
        max_acceptable_price=0.23,
        edge_cents=edge,
        consensus_prob_yes=0.30,
        fair_mid_devigged=0.20,
        source_scan_ids=["abcd1234-efgh-5678-ijkl-mnop90qrstuv"],
        reasoning="Test reasoning here",
    )


class TestFormatAlert:
    def test_includes_ticker(self):
        msg = telegram.format_alert(_alert())
        assert "KXTEST-1" in msg

    def test_includes_tier_in_caps(self):
        msg = telegram.format_alert(_alert(tier=AlertTier.CONSENSUS))
        assert "CONSENSUS" in msg

    def test_includes_direction(self):
        msg = telegram.format_alert(_alert(direction=Direction.YES))
        assert "YES" in msg
        msg = telegram.format_alert(_alert(direction=Direction.NO))
        assert "NO" in msg

    def test_includes_edge(self):
        msg = telegram.format_alert(_alert(edge=15))
        assert "+15c" in msg or "15c" in msg

    def test_includes_reasoning(self):
        msg = telegram.format_alert(_alert())
        assert "Test reasoning here" in msg


class TestSendAlert:
    @patch.dict("os.environ", {"TELEGRAM_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("httpx.post")
    def test_sends_consensus_tier(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        result = telegram.send_alert(_alert(tier=AlertTier.CONSENSUS))
        assert result is True
        mock_post.assert_called_once()

    @patch.dict("os.environ", {"TELEGRAM_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("httpx.post")
    def test_sends_claude_solo(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        result = telegram.send_alert(_alert(tier=AlertTier.CLAUDE_SOLO))
        assert result is True

    @patch("httpx.post")
    def test_skips_grok_solo_silently(self, mock_post):
        """GROK_SOLO is tracked in JSONL but never sent to Telegram."""
        result = telegram.send_alert(_alert(tier=AlertTier.GROK_SOLO))
        assert result is False
        mock_post.assert_not_called()

    @patch("httpx.post")
    def test_skips_none_silently(self, mock_post):
        result = telegram.send_alert(_alert(tier=AlertTier.NONE))
        assert result is False
        mock_post.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_token_returns_false(self):
        result = telegram.send_alert(_alert())
        assert result is False

    @patch.dict("os.environ", {"TELEGRAM_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("httpx.post")
    def test_telegram_400_returns_false(self, mock_post):
        mock_post.return_value = MagicMock(status_code=400, text="Bad Request")
        result = telegram.send_alert(_alert())
        assert result is False

    @patch.dict("os.environ", {"TELEGRAM_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("httpx.post")
    def test_network_exception_returns_false(self, mock_post):
        mock_post.side_effect = RuntimeError("network down")
        result = telegram.send_alert(_alert())
        assert result is False


class TestSendAlerts:
    @patch.dict("os.environ", {"TELEGRAM_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    @patch("httpx.post")
    def test_batch_with_mixed_tiers(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        alerts = [
            _alert(tier=AlertTier.CONSENSUS, ticker="A"),
            _alert(tier=AlertTier.CLAUDE_SOLO, ticker="B"),
            _alert(tier=AlertTier.GROK_SOLO, ticker="C"),
            _alert(tier=AlertTier.NONE, ticker="D"),
        ]
        result = telegram.send_alerts(alerts)
        assert result == {"sent": 2, "skipped": 2, "failed": 0}
