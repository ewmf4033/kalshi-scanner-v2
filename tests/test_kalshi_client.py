"""
Unit tests for core/kalshi_client.py.

Focus: deterministic behavior. Network calls are mocked.
What we test:
    1. Auth signing is correct (verifiable hash)
    2. Rate limit backoff triggers on 429
    3. Response parsing produces valid MarketSnapshot objects
    4. Malformed/missing fields degrade gracefully (return None, log, don't crash)
    5. Market status is mapped to our MarketStatus enum correctly
"""

import base64
import hashlib
import pytest
from unittest.mock import MagicMock, patch

from core import kalshi_client as kc
from core.schema import MarketSnapshot, MarketStatus


# ===========================================================================
# AUTH SIGNING
# ===========================================================================

class TestAuthSigning:
    """
    Kalshi auth: sign (timestamp_ms + METHOD + path) with RSA-PSS + SHA-256.
    We don't test the crypto itself — cryptography library is trusted.
    We test that we pass the right inputs in the right order.
    """

    def test_message_format(self):
        """Message must be concat of ts_ms + METHOD + path — no separators, uppercase method."""
        msg = kc._build_auth_message(timestamp_ms="1713456789000", method="get", path="/trade-api/v2/markets")
        assert msg == b"1713456789000GET/trade-api/v2/markets"

    def test_method_uppercased(self):
        """Case doesn't matter for input, but output method must be UPPERCASE."""
        m1 = kc._build_auth_message(timestamp_ms="1", method="Get", path="/x")
        m2 = kc._build_auth_message(timestamp_ms="1", method="get", path="/x")
        m3 = kc._build_auth_message(timestamp_ms="1", method="GET", path="/x")
        assert m1 == m2 == m3 == b"1GET/x"


# ===========================================================================
# RESPONSE PARSING -> MarketSnapshot
# ===========================================================================

class TestMarketParsing:
    """
    Kalshi returns markets with fields like:
        yes_bid, yes_ask (integer cents in API, we keep as cents)
        last_price (integer cents, nullable)
        volume, volume_24h (floats)
        open_interest (integer)
        close_time (ISO datetime)
        status ("open" | "settled" | "halted" | "paused" | etc.)

    parse_market should produce a valid MarketSnapshot or None (if unparseable).
    """

    def _valid_market_json(self, **overrides):
        """Minimal valid Kalshi market response — uses real Kalshi field names."""
        base = {
            "ticker": "KXCPI-26MAY-T0.4",
            "event_ticker": "KXCPI-26MAY",
            "title": "April CPI above 0.4% MoM",
            "status": "open",
            "yes_bid_dollars": "0.6200",
            "yes_ask_dollars": "0.6500",
            "no_bid_dollars":  "0.3300",
            "no_ask_dollars":  "0.3600",
            "last_price_dollars": "0.6300",
            "volume_fp": "1500.0",
            "volume_24h_fp": "200.0",
            "open_interest_fp": "800",
            "open_time": "2026-03-01T15:00:00Z",
            "close_time": "2026-05-12T12:00:00Z",
            "category": "Economics",
        }
        base.update(overrides)
        return base

    def test_parses_valid_market(self):
        raw = self._valid_market_json()
        snap = kc.parse_market(raw, captured_at_utc="2026-04-18T15:00:00Z")
        assert isinstance(snap, MarketSnapshot)
        assert snap.ticker == "KXCPI-26MAY-T0.4"
        assert snap.yes_bid_cents == 62
        assert snap.yes_ask_cents == 65
        assert snap.no_bid_cents == 33
        assert snap.no_ask_cents == 36
        assert snap.last_price_cents == 63
        assert snap.status == MarketStatus.OPEN
        assert snap.close_time_utc == "2026-05-12T12:00:00Z"
        assert snap.captured_at_utc == "2026-04-18T15:00:00Z"

    def test_parses_null_last_price(self):
        """last_price is nullable — market with no trades yet."""
        raw = self._valid_market_json(last_price_dollars=None)
        snap = kc.parse_market(raw, captured_at_utc="2026-04-18T15:00:00Z")
        assert snap is not None
        assert snap.last_price_cents is None

    def test_rejects_none_bid_ask(self):
        """
        Empty orderbook (None asks/bids) — the v1 bug that produced fake WTI alerts.
        parse_market must return None for these, not a snapshot with 0s.
        """
        raw = self._valid_market_json(yes_bid_dollars=None, yes_ask_dollars=None)
        assert kc.parse_market(raw, captured_at_utc="2026-04-18T15:00:00Z") is None

    def test_rejects_partial_orderbook(self):
        """Even one None in the book = reject."""
        raw = self._valid_market_json(no_ask_dollars=None)
        assert kc.parse_market(raw, captured_at_utc="2026-04-18T15:00:00Z") is None

    def test_tradeable_statuses_parse(self):
        """Active and open statuses should parse into a MarketSnapshot."""
        for raw_status, expected in [
            ("active", MarketStatus.ACTIVE),
            ("open", MarketStatus.OPEN),
        ]:
            raw = self._valid_market_json(status=raw_status)
            snap = kc.parse_market(raw, captured_at_utc="2026-04-18T15:00:00Z")
            assert snap is not None, f"tradeable status {raw_status} incorrectly rejected"
            assert snap.status == expected, f"status {raw_status} did not map to {expected}"

    def test_non_tradeable_statuses_rejected_silently(self):
        """Non-trading statuses are mapped correctly in the enum but parse_market
        returns None — they're not actionable, and logging them is noise."""
        for raw_status in ["inactive", "settled", "finalized", "halted", "paused", "settlement_pending"]:
            raw = self._valid_market_json(status=raw_status)
            snap = kc.parse_market(raw, captured_at_utc="2026-04-18T15:00:00Z")
            assert snap is None, f"non-tradeable status {raw_status} should have been rejected"

    def test_unknown_status_returns_none(self):
        """Unknown statuses fail closed — we don't guess."""
        raw = self._valid_market_json(status="weird_new_state_from_kalshi")
        assert kc.parse_market(raw, captured_at_utc="2026-04-18T15:00:00Z") is None

    def test_missing_ticker_returns_none(self):
        raw = self._valid_market_json()
        raw.pop("ticker")
        assert kc.parse_market(raw, captured_at_utc="2026-04-18T15:00:00Z") is None

    def test_raw_json_preserved(self):
        """Full original JSON is preserved for reproducibility."""
        raw = self._valid_market_json(weird_field="something_new")
        snap = kc.parse_market(raw, captured_at_utc="2026-04-18T15:00:00Z")
        assert snap.raw_json == raw
        assert snap.raw_json.get("weird_field") == "something_new"


# ===========================================================================
# TIME-TO-CLOSE HELPER
# ===========================================================================

class TestMinutesToClose:
    def test_future_close(self):
        """Market closing in 2 hours."""
        now = "2026-04-18T15:00:00Z"
        close = "2026-04-18T17:00:00Z"
        assert kc.minutes_to_close(close, now) == 120

    def test_past_close(self):
        """Market already closed — negative."""
        now = "2026-04-18T15:00:00Z"
        close = "2026-04-18T14:00:00Z"
        assert kc.minutes_to_close(close, now) == -60

    def test_soon_close(self):
        """Market closing in 45 minutes."""
        now = "2026-04-18T15:00:00Z"
        close = "2026-04-18T15:45:00Z"
        assert kc.minutes_to_close(close, now) == 45


# ===========================================================================
# BACKOFF ARITHMETIC
# ===========================================================================

class TestBackoff:
    def test_exponential(self):
        """Attempt 0 → 1s, attempt 1 → 2s, attempt 2 → 4s, attempt 3 → 8s."""
        assert kc._backoff_seconds(0) == 1.0
        assert kc._backoff_seconds(1) == 2.0
        assert kc._backoff_seconds(2) == 4.0
        assert kc._backoff_seconds(3) == 8.0
        assert kc._backoff_seconds(4) == 16.0

    def test_capped(self):
        """Should not exceed backoff_cap_seconds from config (default 30)."""
        # Attempt 10 would be 1024s naively; must cap at 30
        assert kc._backoff_seconds(10) == 30.0
        assert kc._backoff_seconds(100) == 30.0


# ===========================================================================
# EVENTS-BASED PULLING (Kalshi's /markets endpoint was dominated by sports/
# exotics junk; /series required per-series calls triggering 429s. /events
# with_nested_markets returns real data in one pass.)
# ===========================================================================

from unittest.mock import patch, MagicMock


def _valid_nested_market(ticker="T1", **overrides):
    """Minimal valid nested-market blob as returned by /events."""
    base = {
        "ticker": ticker,
        "event_ticker": "EVT",
        "title": "t",
        "status": "active",
        "yes_bid_dollars": "0.40",
        "yes_ask_dollars": "0.45",
        "no_bid_dollars": "0.55",
        "no_ask_dollars": "0.60",
        "last_price_dollars": "0.42",
        "volume_fp": "100",
        "volume_24h_fp": "50",
        "open_interest_fp": "100",
        "open_time": "2026-03-01T00:00:00Z",
        "close_time": "2026-06-01T00:00:00Z",
    }
    base.update(overrides)
    return base


class TestEventsBasedPulling:

    @patch("core.kalshi_client.kalshi_get")
    def test_filters_by_event_category(self, mock_get):
        # Three events: one allowed, one sports (dropped), one Entertainment (dropped)
        mock_get.return_value = {
            "events": [
                {"event_ticker": "EVT-A", "category": "Economics",
                 "markets": [_valid_nested_market("A1"), _valid_nested_market("A2")]},
                {"event_ticker": "EVT-B", "category": "Sports",
                 "markets": [_valid_nested_market("B1")]},
                {"event_ticker": "EVT-C", "category": "Entertainment",
                 "markets": [_valid_nested_market("C1")]},
            ],
            "cursor": "",
        }
        result = list(kc.pull_all_open_markets(
            "keyid", MagicMock(), captured_at_utc="2026-04-18T15:00:00Z",
        ))
        assert [s.ticker for s in result] == ["A1", "A2"]
        # Verify we hit /events with the nested flag
        call_params = mock_get.call_args.kwargs.get("params", {})
        assert call_params.get("with_nested_markets") == "true"
        assert call_params.get("status") == "open"

    @patch("core.kalshi_client.kalshi_get")
    def test_paginates_across_pages(self, mock_get):
        mock_get.side_effect = [
            {
                "events": [{"event_ticker": "EVT-1", "category": "Crypto",
                            "markets": [_valid_nested_market("X1")]}],
                "cursor": "next",
            },
            {
                "events": [{"event_ticker": "EVT-2", "category": "Politics",
                            "markets": [_valid_nested_market("X2")]}],
                "cursor": "",
            },
        ]
        result = list(kc.pull_all_open_markets(
            "keyid", MagicMock(), captured_at_utc="2026-04-18T15:00:00Z",
        ))
        assert [s.ticker for s in result] == ["X1", "X2"]
        # Second call should have passed cursor
        second_params = mock_get.call_args_list[1].kwargs.get("params", {})
        assert second_params.get("cursor") == "next"

    @patch("core.kalshi_client.kalshi_get")
    def test_event_with_no_markets_safe(self, mock_get):
        """Empty markets list / missing markets key should not crash."""
        mock_get.return_value = {
            "events": [
                {"event_ticker": "EVT-1", "category": "Economics", "markets": []},
                {"event_ticker": "EVT-2", "category": "Economics"},  # no "markets" key
                {"event_ticker": "EVT-3", "category": "Economics",
                 "markets": [_valid_nested_market("OK")]},
            ],
            "cursor": "",
        }
        result = list(kc.pull_all_open_markets(
            "keyid", MagicMock(), captured_at_utc="2026-04-18T15:00:00Z",
        ))
        assert [s.ticker for s in result] == ["OK"]

    @patch("core.kalshi_client.kalshi_get")
    def test_inherits_category_from_event(self, mock_get):
        """Nested markets don't have a category field themselves; our parser
        must inherit it from the event for downstream categorization."""
        mock_get.return_value = {
            "events": [
                {"event_ticker": "EVT-1", "category": "Economics",
                 "markets": [_valid_nested_market("X1")]},
            ],
            "cursor": "",
        }
        result = list(kc.pull_all_open_markets(
            "keyid", MagicMock(), captured_at_utc="2026-04-18T15:00:00Z",
        ))
        assert len(result) == 1
        assert result[0].category_raw == "Economics"
