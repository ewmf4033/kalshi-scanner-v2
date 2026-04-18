"""
config.py — Single source of truth for all constants, thresholds, and pinned external facts.

Every value here is documented. If you find yourself hardcoding a number anywhere else
in the codebase, it belongs here instead.

Versioning:
    - FEE_SCHEDULE_VERSION is the Kalshi fee schedule date. Pinned at prediction time.
      If Kalshi publishes a new schedule, bump this constant and re-verify the formula.
    - MODEL_VERSIONS are the exact model strings sent to each provider.
    - PROMPT_VERSION is bumped when any prompt file changes.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
RAW_SCANS = RAW / "scans"
RAW_ALERTS = RAW / "alerts"
RAW_CONSENSUS = RAW / "consensus"
RAW_RESOLUTIONS = RAW / "resolutions"
RAW_SNAPSHOTS = RAW / "snapshots"
PROMPTS_DIR = ROOT / "scanner" / "prompts"

# ---------------------------------------------------------------------------
# External API endpoints
# ---------------------------------------------------------------------------

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ANTHROPIC_MODEL = "claude-sonnet-4-6"
XAI_API_BASE = "https://api.x.ai/v1/chat/completions"
XAI_MODEL = "grok-3-fast"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MODEL = "gemini-2.0-flash"

# Exact version strings written into every Prediction (reproducibility)
MODEL_VERSIONS = {
    "claude": ANTHROPIC_MODEL,
    "grok": XAI_MODEL,
    "gemini_shadow": GEMINI_MODEL,
}

# Prompt version — bump on any prompt content change
PROMPT_VERSION = "v1.0"

# ---------------------------------------------------------------------------
# Kalshi fee schedule
#   Source: https://kalshi.com/docs/kalshi-fee-schedule.pdf
#   Verified: 2026-04-18
#   Effective: 2026-02-05
# ---------------------------------------------------------------------------

FEE_SCHEDULE_VERSION = "2026-02-05"

# General markets (default for macro, weather, commodities, politics, crypto, etc.)
FEE_TAKER_GENERAL = 0.07
FEE_MAKER_GENERAL = 0.0175

# Special fee markets (S&P 500 and NASDAQ-100 only)
FEE_TAKER_SP_NDX = 0.035
FEE_MAKER_SP_NDX = 0.00875  # Implied 4x reduction, verify if ever trading these

# Tickers that use the special fee schedule
SPECIAL_FEE_TICKER_PREFIXES = ("KXSP500", "INX", "INXD", "INXW", "INXM", "INXY",
                                "INXU", "NASDAQ100", "NASDAQ100D", "NASDAQ100W",
                                "NASDAQ100M", "NASDAQ100Y", "NASDAQ100U")

# Settlement fee is zero across all markets. Hold-to-settlement = single fee on entry only.
SETTLEMENT_FEE = 0.0

# ---------------------------------------------------------------------------
# Tradeability filter — loose now, tighten with data at n=50
#   Rationale from v1 audit: spread≤5c and vol≥50 AND OI≥100 killed 60%+ of valid markets.
#   Start permissive, log the distribution, tighten when we have data.
# ---------------------------------------------------------------------------

TRADEABILITY = {
    "max_spread_cents": 10,          # Reject wider spreads as untradeable
    "min_volume_24h": 25,            # OR logic with min_open_interest
    "min_open_interest": 75,
    "min_age_hours": 2,              # Reject newly-listed markets (stale/empty books)
    "min_minutes_to_close": 60,      # CLV becomes meaningless if market closes in <1h
    "min_price_cents": 3,            # Reject effectively-settled longshots
    "max_price_cents": 97,           # Reject effectively-settled favorites
    # Reject explicitly dead states
    "excluded_statuses": ("halted", "paused", "settlement_pending"),
}

# ---------------------------------------------------------------------------
# Kalshi category allowlist
#
# Kalshi's /markets endpoint returns ~90% Sports + Exotics (KXMVE*) tickers
# by default. To surface actually-tradeable markets we pull by SERIES, not
# by market. Each series is tagged with one of these categories — we
# enumerate series, filter to this allowlist, then pull markets per series.
#
# Live-verified against /series endpoint on 2026-04-18. The 11 allowed
# categories cover ~3,300 of the ~9,700 total series. Excluded:
#   Sports (1744), Exotics (10), Entertainment (2358) — celebrity gossip,
#   Mentions (336) — mention-count tallies,
#   Social (73) — engagement-metric markets,
#   Transportation (40) and Education (2) — too niche to justify pipeline,
#   and "" (43) — uncategorized junk.
# ---------------------------------------------------------------------------

KALSHI_CATEGORY_ALLOWLIST = frozenset({
    "Economics",
    "Financials",
    "Climate and Weather",
    "Politics",
    "Elections",
    "Crypto",
    "Commodities",
    "Health",
    "Science and Technology",
    "Companies",
    "World",
})


def is_allowed_category(raw: str) -> bool:
    """True iff the raw Kalshi category string matches an allowed category.
    Case-sensitive match against Kalshi's actual strings — their API is
    consistent on this. Empty / missing categories are excluded."""
    if not raw:
        return False
    return raw in KALSHI_CATEGORY_ALLOWLIST


# ---------------------------------------------------------------------------
# Edge thresholds (minimum edge to alert, by category)
#   Computed against fee-adjusted expected value, not raw edge.
#   If your edge < 2 * (expected fee), you're paying fees without keeping alpha.
# ---------------------------------------------------------------------------

EDGE_THRESHOLD_CENTS = {
    "macro": 5,
    "weather": 5,
    "politics": 7,      # Harder to predict, demand more edge
    "crypto": 7,        # Noisier
    "commodities": 5,
    "tech": 7,
    "other": 7,
}

# ---------------------------------------------------------------------------
# Consensus detection rules
# ---------------------------------------------------------------------------

CONSENSUS = {
    "range_overlap_tolerance": 0.01,  # max(lo1,lo2) <= min(hi1,hi2) + 0.01
    "max_prob_divergence": 0.15,      # If Claude says 0.60 and Grok says 0.80, that's not consensus
    "min_edge_both_models": 3,        # Both models must show ≥3c edge for consensus
}

# ---------------------------------------------------------------------------
# Alert sizing / max_acceptable_price
#   Formula: max_acceptable_price = fill_price + (edge_cents - min_edge_threshold) / 100
#   Rationale: willing to pay up to the point where min-threshold edge remains
# ---------------------------------------------------------------------------

ALERT = {
    "max_price_slippage_cents": 3,  # max_acceptable_price - fill_price buffer
}

# ---------------------------------------------------------------------------
# Scanner scope
# ---------------------------------------------------------------------------

TOP_N_MARKETS = 100

# Skip sports entirely — separate system
SKIP_PREFIXES = (
    "KXMVE", "KXNCAA", "KXNBA", "KXNHL", "KXMLB", "KXNFL",
    "KXMMA", "KXSOCCER", "KXWNBA", "KXPGA", "KXNASCAR",
    "KXCFB", "KXCBB", "KXEPL", "KXUFC", "KXF1",
)
SKIP_CATEGORIES = ("sports",)

# ---------------------------------------------------------------------------
# Rate limiting (Kalshi GETs)
# ---------------------------------------------------------------------------

KALSHI_RATE_LIMIT = {
    "requests_per_second": 8,          # Well under Kalshi's actual per-endpoint limit
    "max_retries_on_429": 5,
    "backoff_base_seconds": 1.0,       # 1s, 2s, 4s, 8s, 16s
    "backoff_cap_seconds": 30,
}

# ---------------------------------------------------------------------------
# LLM call settings
# ---------------------------------------------------------------------------

LLM = {
    "claude_max_tokens": 8192,
    "claude_temperature": 0.0,         # Deterministic where possible
    "grok_max_tokens": 8192,
    "grok_temperature": 0.2,
    "gemini_max_tokens": 8192,
    "gemini_temperature": 0.2,
    "request_timeout_seconds": 180,
    "retry_on_error": 2,               # Retry LLM calls twice before giving up
    "inter_model_delay_seconds": 0,    # No artificial delay unless needed
}

# ---------------------------------------------------------------------------
# De-vig method — v1 uses multiplicative, Shin deferred
# ---------------------------------------------------------------------------

DEVIG_METHOD = "multiplicative"  # Options: "multiplicative", "shin" (future)


def is_special_fee_ticker(ticker: str) -> bool:
    """Returns True if the ticker falls under S&P 500 / NASDAQ-100 special fee schedule."""
    return ticker.startswith(SPECIAL_FEE_TICKER_PREFIXES)
