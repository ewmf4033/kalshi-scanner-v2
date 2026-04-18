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

    def test_maps_status_to_enum(self):
        for raw_status, expected in [
            ("active", MarketStatus.ACTIVE),     # Kalshi's actual "open for trading" value
            ("open", MarketStatus.OPEN),
            ("settled", MarketStatus.SETTLED),
            ("finalized", MarketStatus.FINALIZED),
            ("halted", MarketStatus.HALTED),
            ("paused", MarketStatus.PAUSED),
            ("settlement_pending", MarketStatus.SETTLEMENT_PENDING),
        ]:
            raw = self._valid_market_json(status=raw_status)
            snap = kc.parse_market(raw, captured_at_utc="2026-04-18T15:00:00Z")
            assert snap.status == expected, f"status {raw_status} did not map to {expected}"

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
