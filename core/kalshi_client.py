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
    "open": MarketStatus.OPEN,
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
    for field in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
        if raw.get(field) is None:
            log.info(json.dumps({
                "phase": "parse_market",
                "ticker": ticker,
                "reason": "none_in_orderbook",
                "field": field,
            }))
            return None

    try:
        snap = MarketSnapshot(
            ticker=ticker,
            event_ticker=raw.get("event_ticker", ""),
            title=raw.get("title", ""),
            status=status,
            yes_bid_cents=int(raw["yes_bid"]),
            yes_ask_cents=int(raw["yes_ask"]),
            no_bid_cents=int(raw["no_bid"]),
            no_ask_cents=int(raw["no_ask"]),
            last_price_cents=(int(raw["last_price"]) if raw.get("last_price") is not None else None),
            volume=float(raw.get("volume", 0) or 0),
            volume_24h=float(raw.get("volume_24h", 0) or 0),
            open_interest=int(raw.get("open_interest", 0) or 0),
            close_time_utc=raw.get("close_time", ""),
            captured_at_utc=captured_at_utc,
            category_raw=raw.get("category", "") or "",
            raw_json=raw,
        )
        return snap
    except (ValueError, TypeError) as e:
        log.warning(json.dumps({
            "phase": "parse_market",
            "ticker": ticker,
            "reason": "schema_validation_failed",
            "error": str(e),
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

def pull_all_open_markets(
    api_key_id: str,
    private_key,
    captured_at_utc: str,
    max_pages: int = 50,
) -> Iterator[MarketSnapshot]:
    """
    Stream MarketSnapshots for all open non-sports markets.

    Yields validated snapshots. Unparseable markets are dropped and logged.
    Sports filtering happens in scanner/ingest.py, not here.
    """
    cursor = None
    pages = 0
    while pages < max_pages:
        params = {"limit": 200, "status": "open"}
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

        # Pace ourselves between pages regardless of whether we hit 429
        time.sleep(1.0 / config.KALSHI_RATE_LIMIT["requests_per_second"])
