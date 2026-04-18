"""
ingest.py — Tradeability filter + enrichment.

Takes raw MarketSnapshot objects from kalshi_client and applies config-driven
tradeability filters. Markets that pass are enriched with:
    - De-vigged fair probability (multiplicative method)
    - Time-to-close in minutes
    - Time-since-open in hours
    - Normalized Category enum

The filter is STRICT by design. A market that barely passes one check is fine;
a market that fails any check is dropped with a structured log line. No "soft"
warnings, no "try to recover" logic. Junk in, None out.

Why this module exists:
    The v1 scanner had a filter bug where implied_prob was computed correctly
    via de-vig then silently overwritten with raw mid_price. EnrichedMarket's
    dataclass validation makes that bug structurally impossible — the only way
    to get a fair_prob_yes onto the wire is via evaluate() here.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Iterable, Optional, Tuple, Dict, List

from core import config
from core.schema import (
    MarketSnapshot,
    EnrichedMarket,
    FilterReason,
    Category,
)
from scanner import price_math as pm

log = logging.getLogger("ingest")


# ---------------------------------------------------------------------------
# Category normalization
# ---------------------------------------------------------------------------

# Kalshi's category_raw is inconsistent — sometimes empty, sometimes "Economics",
# sometimes something like "Climate and Weather". We normalize to our enum via
# substring match, lowest common denominator. Anything unmatched goes to OTHER.
_CATEGORY_KEYWORDS = {
    Category.MACRO:       ["economics", "economy", "macro", "cpi", "gdp", "ppi",
                           "payroll", "unemployment", "fed", "fomc", "interest rate",
                           "inflation"],
    Category.WEATHER:     ["weather", "climate", "temperature", "hurricane",
                           "snowfall", "precipitation"],
    Category.POLITICS:    ["politics", "election", "congress", "president",
                           "policy", "legislation"],
    Category.CRYPTO:      ["crypto", "bitcoin", "ethereum", "btc", "eth",
                           "cryptocurrency"],
    Category.COMMODITIES: ["commodit", "oil", "gas", "wti", "brent", "natgas",
                           "copper", "gold", "silver", "wheat", "corn", "soybean",
                           "coffee", "sugar"],
    Category.TECH:        ["tech", "technology", "ai", "ml", "software"],
}


def categorize(raw: str) -> Category:
    """Map Kalshi's category_raw string to our normalized Category enum.
    Case-insensitive, substring-based. Returns OTHER for anything unmatched."""
    if not raw:
        return Category.OTHER
    low = raw.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in low:
                return category
    return Category.OTHER


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _minutes_between(earlier_iso: str, later_iso: str) -> float:
    return (_parse_iso(later_iso) - _parse_iso(earlier_iso)).total_seconds() / 60.0


# ---------------------------------------------------------------------------
# Single-market evaluation
# ---------------------------------------------------------------------------

def evaluate(
    snap: MarketSnapshot,
    now_utc: str,
) -> Tuple[FilterReason, Optional[EnrichedMarket]]:
    """
    Apply tradeability filters and enrich. Returns (reason, enriched_or_None).

    Ordering is intentional: cheapest checks first (arithmetic on existing
    fields), network/compute-heavy enrichment last.

    The reason enum is always populated — PASSED if the market survives,
    otherwise the first failing check.
    """
    t = config.TRADEABILITY

    # 1. Spread check
    if snap.spread_cents > t["max_spread_cents"]:
        _log_reject(snap, FilterReason.SPREAD_TOO_WIDE,
                    spread=snap.spread_cents, max=t["max_spread_cents"])
        return FilterReason.SPREAD_TOO_WIDE, None

    # 2. Liquidity check (vol24h OR OI must meet threshold)
    if snap.volume_24h < t["min_volume_24h"] and snap.open_interest < t["min_open_interest"]:
        _log_reject(snap, FilterReason.LOW_LIQUIDITY,
                    vol24h=snap.volume_24h, oi=snap.open_interest)
        return FilterReason.LOW_LIQUIDITY, None

    # 3. Price-range check (tails are systematically noisy / fee-dominated)
    mid = snap.mid_cents
    if mid < t["min_price_cents"] or mid > t["max_price_cents"]:
        _log_reject(snap, FilterReason.PRICE_OUT_OF_RANGE,
                    mid=mid, lo=t["min_price_cents"], hi=t["max_price_cents"])
        return FilterReason.PRICE_OUT_OF_RANGE, None

    # 4. Time-to-close check
    try:
        minutes_to_close = _minutes_between(now_utc, snap.close_time_utc)
    except (ValueError, TypeError):
        _log_reject(snap, FilterReason.TOO_CLOSE_TO_SETTLEMENT,
                    reason="bad_close_time")
        return FilterReason.TOO_CLOSE_TO_SETTLEMENT, None
    if minutes_to_close < t["min_minutes_to_close"]:
        _log_reject(snap, FilterReason.TOO_CLOSE_TO_SETTLEMENT,
                    mins=minutes_to_close, min=t["min_minutes_to_close"])
        return FilterReason.TOO_CLOSE_TO_SETTLEMENT, None

    # 5. Time-since-open check (brand-new markets have thin, unreliable books)
    try:
        hours_since_open = _minutes_between(snap.open_time_utc, now_utc) / 60.0
    except (ValueError, TypeError):
        _log_reject(snap, FilterReason.TOO_NEW, reason="bad_open_time")
        return FilterReason.TOO_NEW, None
    if hours_since_open < t["min_age_hours"]:
        _log_reject(snap, FilterReason.TOO_NEW,
                    hrs=hours_since_open, min=t["min_age_hours"])
        return FilterReason.TOO_NEW, None

    # 6. De-vig (most expensive, last)
    try:
        fair_prob_yes = pm.devig_multiplicative(
            yes_ask=snap.yes_ask_cents / 100.0,
            no_ask=snap.no_ask_cents / 100.0,
        )
    except (ValueError, ZeroDivisionError) as e:
        _log_reject(snap, FilterReason.DEVIG_FAILED, error=str(e))
        return FilterReason.DEVIG_FAILED, None

    # Additional structural check: de-vig can return exactly 0 or 1 in pathological
    # cases (e.g. if one side is free). EnrichedMarket requires strict (0, 1).
    if not (0.0 < fair_prob_yes < 1.0):
        _log_reject(snap, FilterReason.DEVIG_FAILED,
                    fair_prob=fair_prob_yes)
        return FilterReason.DEVIG_FAILED, None

    enriched = EnrichedMarket(
        snapshot=snap,
        fair_prob_yes=fair_prob_yes,
        minutes_to_close=int(minutes_to_close),
        hours_since_open=hours_since_open,
        category=categorize(snap.category_raw),
    )
    return FilterReason.PASSED, enriched


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def filter_and_enrich_batch(
    snapshots: Iterable[MarketSnapshot],
    now_utc: str,
) -> Tuple[List[EnrichedMarket], Dict[FilterReason, int]]:
    """
    Apply evaluate() to every snapshot. Returns (enriched_list, stats_dict).

    stats_dict is keyed by FilterReason — every reason enum value is present,
    even if the count is 0. Makes downstream reporting trivial.
    """
    stats: Dict[FilterReason, int] = {r: 0 for r in FilterReason}
    enriched_list: List[EnrichedMarket] = []

    for snap in snapshots:
        reason, enriched = evaluate(snap, now_utc=now_utc)
        stats[reason] += 1
        if enriched is not None:
            enriched_list.append(enriched)

    return enriched_list, stats


# ---------------------------------------------------------------------------
# Structured logging helper
# ---------------------------------------------------------------------------

def _log_reject(snap: MarketSnapshot, reason: FilterReason, **fields):
    """Emit a structured JSON log line for a rejected market."""
    payload = {
        "phase": "ingest_evaluate",
        "ticker": snap.ticker,
        "reason": reason.value,
    }
    payload.update(fields)
    log.info(json.dumps(payload, default=str))
