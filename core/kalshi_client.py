"""
kalshi_client.py — Minimal, tested wrapper around Kalshi's trade-api.

Responsibilities (one thing each):
    - Sign GET requests per Kalshi auth spec (RSA-PSS + SHA-256)
    - Call the API with exponential backoff on 429
    - Parse a market response into a validated MarketSnapshot or None
    - Compute minutes_to_close for a market

NON-responsibilities:
    - No filtering / tradeability logic (lives in scanner/ingest.py)
    - No business logic
    - No telemetry beyond structured logging
"""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Iterator

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from core import config
from core.schema import MarketSnapshot, MarketStatus

log = logging.getLogger("kalshi_client")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _build_auth_message(timestamp_ms: str, method: str, path: str) -> bytes:
    """Kalshi signs the concatenation of timestamp_ms + UPPERCASE(method) + path."""
    return f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")


def load_private_key(key_path: str):
    """Load the RSA private key Kalshi uses to sign API requests."""
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign(private_key, timestamp_ms: str, method: str, path: str) -> str:
    msg = _build_auth_message(timestamp_ms, method, path)
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def _auth_headers(api_key_id: str, private_key, method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": _sign(private_key, ts, method, path),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Rate limit / backoff
# ---------------------------------------------------------------------------

def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with cap: base * 2^attempt, capped at backoff_cap_seconds."""
    base = config.KALSHI_RATE_LIMIT["backoff_base_seconds"]
    cap = config.KALSHI_RATE_LIMIT["backoff_cap_seconds"]
    return min(base * (2 ** attempt), cap)


# ---------------------------------------------------------------------------
# Generic GET with backoff
# ---------------------------------------------------------------------------

def kalshi_get(
    path: str,
    api_key_id: str,
    private_key,
    params: Optional[dict] = None,
    timeout: float = 30.0,
) -> dict:
    """
    Authenticated GET with exponential backoff on 429.

    Raises after max_retries_on_429.
    Other non-2xx errors raise immediately (not retried — those are real bugs or outages).
    """
    url = f"{config.KALSHI_API_BASE}{path}"
    max_retries = config.KALSHI_RATE_LIMIT["max_retries_on_429"]

    for attempt in range(max_retries + 1):
        headers = _auth_headers(api_key_id, private_key, "GET", path)
        t0 = time.time()
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=timeout)
        except httpx.RequestError as e:
            log.warning(json.dumps({
                "phase": "kalshi_get",
                "path": path,
                "attempt": attempt,
                "error": f"{type(e).__name__}: {e}",
            }))
            if attempt < max_retries:
                time.sleep(_backoff_seconds(attempt))
                continue
            raise

        latency_ms = int((time.time() - t0) * 1000)

        if r.status_code == 429:
            wait = _backoff_seconds(attempt)
            log.warning(json.dumps({
                "phase": "kalshi_get",
                "path": path,
                "status": 429,
                "attempt": attempt,
                "backoff_seconds": wait,
                "latency_ms": latency_ms,
            }))
            if attempt < max_retries:
                time.sleep(wait)
                continue
            r.raise_for_status()

        if r.status_code >= 400:
            log.error(json.dumps({
                "phase": "kalshi_get",
                "path": path,
                "status": r.status_code,
                "latency_ms": latency_ms,
                "body_preview": r.text[:200],
            }))
            r.raise_for_status()

        log.info(json.dumps({
            "phase": "kalshi_get",
            "path": path,
            "status": r.status_code,
            "latency_ms": latency_ms,
            "attempt": attempt,
        }))
        return r.json()

    raise RuntimeError(f"Unreachable: kalshi_get fell through loop for {path}")


# ---------------------------------------------------------------------------
# Market parsing
# ---------------------------------------------------------------------------

_STATUS_MAP = {
    "active": MarketStatus.ACTIVE,   # Live-verified: Kalshi returns "active" for trading markets
    "open": MarketStatus.OPEN,       # Kept for safety / legacy
    "settled": MarketStatus.SETTLED,
    "finalized": MarketStatus.FINALIZED,
    "halted": MarketStatus.HALTED,
    "paused": MarketStatus.PAUSED,
    "settlement_pending": MarketStatus.SETTLEMENT_PENDING,
}


def parse_market(raw: dict, captured_at_utc: str) -> Optional[MarketSnapshot]:
    """
    Convert Kalshi's market JSON into a validated MarketSnapshot.

    Returns None (and logs) if:
        - Any orderbook field is None (empty book — the v1 WTI bug)
        - Unknown status (fail closed)
        - Missing ticker (malformed response)
    """
    ticker = raw.get("ticker")
    if not ticker:
        log.warning(json.dumps({"phase": "parse_market", "reason": "missing_ticker"}))
        return None

    status_raw = raw.get("status", "").lower()
    status = _STATUS_MAP.get(status_raw)
    if status is None:
        log.warning(json.dumps({
            "phase": "parse_market",
            "ticker": ticker,
            "reason": "unknown_status",
            "status_raw": status_raw,
        }))
        return None

    # Reject empty or partial orderbooks upfront. This is the v1 empty-book fix.
    # Kalshi uses *_dollars (string like "0.2400") — None means no book on that side.
    for field in ("yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars", "no_ask_dollars"):
        if raw.get(field) is None:
            log.info(json.dumps({
                "phase": "parse_market",
                "ticker": ticker,
                "reason": "none_in_orderbook",
                "field": field,
            }))
            return None

    try:
        # Kalshi returns prices as dollar strings ("0.2400"), volume/OI as fp strings ("246.00").
        # We convert prices to integer cents and volume/OI to floats/ints.
        yes_bid_c = int(round(float(raw["yes_bid_dollars"]) * 100))
        yes_ask_c = int(round(float(raw["yes_ask_dollars"]) * 100))
        no_bid_c  = int(round(float(raw["no_bid_dollars"])  * 100))
        no_ask_c  = int(round(float(raw["no_ask_dollars"])  * 100))
        last_price_c = (int(round(float(raw["last_price_dollars"]) * 100))
                        if raw.get("last_price_dollars") is not None else None)

        snap = MarketSnapshot(
            ticker=ticker,
            event_ticker=raw.get("event_ticker", ""),
            title=raw.get("title", ""),
            status=status,
            yes_bid_cents=yes_bid_c,
            yes_ask_cents=yes_ask_c,
            no_bid_cents=no_bid_c,
            no_ask_cents=no_ask_c,
            last_price_cents=last_price_c,
            volume=float(raw.get("volume_fp", 0) or 0),
            volume_24h=float(raw.get("volume_24h_fp", 0) or 0),
            open_interest=int(float(raw.get("open_interest_fp", 0) or 0)),
            open_time_utc=raw.get("open_time", ""),
            close_time_utc=raw.get("close_time", ""),
            captured_at_utc=captured_at_utc,
            category_raw=raw.get("category", "") or "",
            raw_json=raw,
        )
        return snap
    except (ValueError, TypeError, KeyError) as e:
        log.warning(json.dumps({
            "phase": "parse_market",
            "ticker": ticker,
            "reason": "schema_validation_failed",
            "error": f"{type(e).__name__}: {e}",
        }))
        return None


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def minutes_to_close(close_time_utc: str, now_utc: str) -> int:
    """Minutes until market close. Negative if already closed."""
    close = _parse_iso(close_time_utc)
    now = _parse_iso(now_utc)
    return int((close - now).total_seconds() // 60)


# ---------------------------------------------------------------------------
# Market pulling — the only public business function
# ---------------------------------------------------------------------------

def pull_allowed_series(
    api_key_id: str,
    private_key,
    max_pages: int = 100,
) -> Iterator[str]:
    """
    Enumerate Kalshi series, yield tickers for categories in the allowlist.

    Why this exists:
        Kalshi's /markets endpoint returns ~99% sports + exotics (KXMVE*)
        by default. Filtering at the market level after-the-fact wastes
        API budget. Series are the right scope — each series has exactly
        one category, and we can scope /markets pulls by series_ticker.

    The /series endpoint doesn't accept a category filter, so we pull
    everything and filter client-side. This is ~10K series ≈ 50 pages
    at 200/page, done once per scan.
    """
    cursor = None
    pages = 0
    while pages < max_pages:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor

        data = kalshi_get("/series", api_key_id, private_key, params=params)
        series = data.get("series", [])
        if not series:
            break

        for s in series:
            cat = s.get("category", "")
            if config.is_allowed_category(cat):
                ticker = s.get("ticker")
                if ticker:
                    yield ticker

        cursor = data.get("cursor")
        pages += 1
        if not cursor:
            break

        time.sleep(1.0 / config.KALSHI_RATE_LIMIT["requests_per_second"])


def pull_markets_for_series(
    series_ticker: str,
    api_key_id: str,
    private_key,
    captured_at_utc: str,
    max_pages: int = 20,
) -> Iterator[MarketSnapshot]:
    """
    Pull all open markets within a single series, yielding validated snapshots.

    Series typically have 1-50 markets; max_pages=20 × 200 markets/page
    handles even the largest (e.g. tournament brackets) with plenty of headroom.
    """
    cursor = None
    pages = 0
    while pages < max_pages:
        params = {"limit": 200, "series_ticker": series_ticker, "status": "open"}
        if cursor:
            params["cursor"] = cursor

        data = kalshi_get("/markets", api_key_id, private_key, params=params)
        markets = data.get("markets", [])
        if not markets:
            break

        for raw in markets:
            snap = parse_market(raw, captured_at_utc=captured_at_utc)
            if snap is not None:
                yield snap

        cursor = data.get("cursor")
        pages += 1
        if not cursor:
            break

        time.sleep(1.0 / config.KALSHI_RATE_LIMIT["requests_per_second"])


def pull_all_open_markets(
    api_key_id: str,
    private_key,
    captured_at_utc: str,
    max_pages: int = 50,  # kept for signature compat; applied per-series
) -> Iterator[MarketSnapshot]:
    """
    Stream MarketSnapshots across all series in the allowed-category universe.

    Architecture: series-scoped pulling.
        1. Enumerate /series, filter to allowlist client-side.
        2. For each allowed series, pull /markets?series_ticker=X.

    This replaces the flat /markets pull, which returned ~99% sports junk
    regardless of URL filters. Live-verified 2026-04-18: flat pull of 10K
    markets yielded zero CPI/Fed/weather/politics markets.

    Note: parse_market already rejects empty orderbooks, unknown statuses,
    etc., so callers still get validated MarketSnapshot objects only.
    """
    for series_ticker in pull_allowed_series(api_key_id, private_key):
        yield from pull_markets_for_series(
            series_ticker,
            api_key_id,
            private_key,
            captured_at_utc=captured_at_utc,
            max_pages=max_pages,
        )
