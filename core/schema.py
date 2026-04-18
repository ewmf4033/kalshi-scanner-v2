"""
schema.py — Frozen dataclass contracts for the entire pipeline.

Design principles:
    1. Every record is immutable (frozen=True). If you need to update, create a new record.
    2. __post_init__ validation catches bad data at construction time, not at save time.
    3. LLMs CANNOT populate metadata fields (model, scan_id, timestamps). Orchestrator owns these.
    4. Every Prediction has enough context to be rescored from scratch months later.
    5. All money values are Decimal, not float. Brier and probabilities are float (fine for stats).

Versioning:
    SCHEMA_VERSION is bumped on any breaking change. Old records remain readable with
    explicit version-dispatch at the load layer (not in schema).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

SCHEMA_VERSION = "v2.0.0"

# ---------------------------------------------------------------------------
# Enums — constrained string types
# ---------------------------------------------------------------------------

class Direction(str, Enum):
    YES = "YES"
    NO = "NO"


class ModelName(str, Enum):
    """
    The set of model identities the orchestrator is allowed to assign.
    LLMs CANNOT write to this field. Attempts to construct a Prediction with
    any other model string raise ValueError.
    """
    CLAUDE = "claude"
    GROK = "grok"
    GEMINI_SHADOW = "gemini_shadow"


class AlertTier(str, Enum):
    CONSENSUS = "consensus"
    CLAUDE_SOLO = "claude_solo"
    GROK_SOLO = "grok_solo"  # tracked, not sent to Telegram
    NONE = "none"             # prediction did not become an alert


class Category(str, Enum):
    MACRO = "macro"
    WEATHER = "weather"
    POLITICS = "politics"
    CRYPTO = "crypto"
    COMMODITIES = "commodities"
    TECH = "tech"
    OTHER = "other"


class FilterReason(str, Enum):
    """Why a MarketSnapshot was accepted or rejected by the tradeability filter.
    PASSED is explicit so ingest counters can use a single enum for all buckets."""
    PASSED = "passed"
    SPREAD_TOO_WIDE = "spread_too_wide"
    LOW_LIQUIDITY = "low_liquidity"
    PRICE_OUT_OF_RANGE = "price_out_of_range"
    TOO_CLOSE_TO_SETTLEMENT = "too_close_to_settlement"
    TOO_NEW = "too_new"
    DEVIG_FAILED = "devig_failed"


class MarketStatus(str, Enum):
    """
    Kalshi market statuses. Verified against live API 2026-04-18:
    Kalshi's /markets endpoint returns "active" for currently-trading markets,
    even when the URL filter is status=open. This is a Kalshi quirk — the "open"
    URL param filters correctly, but the response field says "active".
    """
    ACTIVE = "active"           # Trading, what v1 would have called "open"
    OPEN = "open"               # Legacy / alternate
    SETTLED = "settled"
    FINALIZED = "finalized"
    HALTED = "halted"
    PAUSED = "paused"
    SETTLEMENT_PENDING = "settlement_pending"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_scan_id() -> str:
    return str(uuid.uuid4())


def prompt_hash(prompt_text: str) -> str:
    """SHA-256 of the exact prompt sent to the model. First 16 hex chars for compactness."""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]


def _validate_prob(x: float, name: str) -> None:
    if not isinstance(x, (int, float)):
        raise ValueError(f"{name} must be numeric, got {type(x).__name__}")
    if not (0.0 <= x <= 1.0):
        raise ValueError(f"{name} must be in [0,1], got {x}")


def _validate_cents(x: int, name: str) -> None:
    if not isinstance(x, int):
        raise ValueError(f"{name} must be int (cents), got {type(x).__name__}")
    if not (0 <= x <= 100):
        raise ValueError(f"{name} must be in [0,100] cents, got {x}")


def _validate_iso_datetime(s: str, name: str) -> None:
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as e:
        raise ValueError(f"{name} must be ISO-8601 datetime, got {s!r}: {e}")


# ---------------------------------------------------------------------------
# MarketSnapshot — raw state of a market at scan time
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketSnapshot:
    """
    Captured exactly as Kalshi returned it. Used for rescoring.
    All price fields in integer cents to avoid float issues.
    """
    ticker: str
    event_ticker: str
    title: str
    status: MarketStatus

    yes_bid_cents: int
    yes_ask_cents: int
    no_bid_cents: int
    no_ask_cents: int
    last_price_cents: Optional[int]

    volume: float
    volume_24h: float
    open_interest: int

    open_time_utc: str      # ISO-8601, when market opened for trading
    close_time_utc: str     # ISO-8601
    captured_at_utc: str    # ISO-8601
    category_raw: str       # Kalshi's category string — not normalized

    raw_json: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.ticker:
            raise ValueError("ticker required")
        _validate_cents(self.yes_bid_cents, "yes_bid_cents")
        _validate_cents(self.yes_ask_cents, "yes_ask_cents")
        _validate_cents(self.no_bid_cents, "no_bid_cents")
        _validate_cents(self.no_ask_cents, "no_ask_cents")
        if self.last_price_cents is not None:
            _validate_cents(self.last_price_cents, "last_price_cents")
        if self.open_interest < 0:
            raise ValueError(f"open_interest cannot be negative, got {self.open_interest}")
        if self.volume < 0 or self.volume_24h < 0:
            raise ValueError("volume cannot be negative")
        _validate_iso_datetime(self.open_time_utc, "open_time_utc")
        _validate_iso_datetime(self.close_time_utc, "close_time_utc")
        _validate_iso_datetime(self.captured_at_utc, "captured_at_utc")

    @property
    def spread_cents(self) -> int:
        return self.yes_ask_cents - self.yes_bid_cents

    @property
    def mid_cents(self) -> float:
        return (self.yes_bid_cents + self.yes_ask_cents) / 2.0


# ---------------------------------------------------------------------------
# EnrichedMarket — MarketSnapshot + derived fields after tradeability filter
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnrichedMarket:
    """
    A MarketSnapshot that passed tradeability filters, with derived fields.

    fair_prob_yes is de-vigged via the multiplicative method — raw mid is
    NEVER used as a probability. This class exists specifically to prevent
    the v1 implied_prob-overwrite bug from recurring.

    Composition over inheritance: we wrap the snapshot rather than copy
    its fields. Audit trail preserved — enriched.snapshot.raw_json is
    always the raw Kalshi response.
    """
    snapshot: MarketSnapshot
    fair_prob_yes: float           # de-vigged p(YES), strictly in (0, 1)
    minutes_to_close: int          # >= 0
    hours_since_open: float        # >= 0
    category: Category             # normalized from snapshot.category_raw

    def __post_init__(self):
        if not (0.0 < self.fair_prob_yes < 1.0):
            raise ValueError(
                f"fair_prob_yes must be strictly in (0, 1), got {self.fair_prob_yes}"
            )
        if self.minutes_to_close < 0:
            raise ValueError(f"minutes_to_close must be >= 0, got {self.minutes_to_close}")
        if self.hours_since_open < 0:
            raise ValueError(f"hours_since_open must be >= 0, got {self.hours_since_open}")

    @property
    def ticker(self) -> str:
        return self.snapshot.ticker

    @property
    def mid_cents(self) -> float:
        return self.snapshot.mid_cents

    @property
    def spread_cents(self) -> int:
        return self.snapshot.spread_cents


# ---------------------------------------------------------------------------
# ModelOutput — exactly what the LLM is allowed to produce, and nothing more
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelOutput:
    """
    The ONLY fields an LLM is allowed to control.
    Everything else (model name, timestamps, ticker, prices) is set by orchestrator.

    model_prob_yes: the model's single point estimate of P(YES)
    prob_range_lo / prob_range_hi: model's uncertainty band, both in [0,1]
    confidence: model's self-rated confidence
    catalyst: short rationale
    category: one of the Category enum values (normalized by orchestrator if sloppy)
    """
    model_prob_yes: float
    prob_range_lo: float
    prob_range_hi: float
    confidence: Confidence
    catalyst: str
    category: Category

    def __post_init__(self):
        _validate_prob(self.model_prob_yes, "model_prob_yes")
        _validate_prob(self.prob_range_lo, "prob_range_lo")
        _validate_prob(self.prob_range_hi, "prob_range_hi")
        if self.prob_range_lo > self.prob_range_hi:
            raise ValueError(
                f"prob_range_lo ({self.prob_range_lo}) > prob_range_hi ({self.prob_range_hi})"
            )
        if not (self.prob_range_lo <= self.model_prob_yes <= self.prob_range_hi):
            raise ValueError(
                f"model_prob_yes ({self.model_prob_yes}) must be within "
                f"[{self.prob_range_lo}, {self.prob_range_hi}]"
            )
        if not self.catalyst or not self.catalyst.strip():
            raise ValueError("catalyst required")


# ---------------------------------------------------------------------------
# Prediction — one scan, one ticker, one model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Prediction:
    """
    The atomic unit of record. One Prediction per (scan_date, ticker, model).
    Contains everything needed to rescore from scratch.
    """
    # ---- Identity ----
    scan_id: str                  # UUID4 linking this prediction to its alert and resolution
    schema_version: str           # Bumped on breaking changes

    # ---- When/where ----
    scan_date: date
    scan_ts_utc: str              # ISO-8601
    model: ModelName              # Orchestrator-set, not LLM-set
    model_version: str            # e.g. "claude-sonnet-4-6-20250514"
    prompt_version: str           # e.g. "scanner-v1.0"
    prompt_hash_hex: str          # SHA-256 (first 16 hex) of exact prompt sent
    fee_schedule_version: str     # e.g. "2026-02-05" — pinned at prediction time

    # ---- What was the market ----
    ticker: str
    market_snapshot: MarketSnapshot

    # ---- What we'd pay ----
    direction: Direction
    fill_price: float             # yes_ask if YES, no_ask if NO; in [0,1]
    fair_mid_devigged: float      # de-vigged P(YES), scanner/price_math.py

    # ---- What the model said ----
    output: ModelOutput
    correlation_cluster: Optional[str]  # Deterministic from orchestrator, never LLM-set
    resolution_date_declared: Optional[date]  # What the model thought; close_time_utc is ground truth

    # ---- Operational ----
    excluded: bool = False                      # Human override for scoring
    exclusion_reason: Optional[str] = None

    def __post_init__(self):
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version mismatch: {self.schema_version} vs {SCHEMA_VERSION}"
            )
        if not self.scan_id or len(self.scan_id) < 32:
            raise ValueError(f"scan_id must be a UUID-like string, got {self.scan_id!r}")
        if self.ticker != self.market_snapshot.ticker:
            raise ValueError(
                f"ticker mismatch: {self.ticker} vs snapshot {self.market_snapshot.ticker}"
            )
        if not isinstance(self.model, ModelName):
            raise ValueError(
                f"model must be ModelName enum, got {type(self.model).__name__}. "
                f"LLMs are not allowed to set this field."
            )
        if not isinstance(self.direction, Direction):
            raise ValueError(f"direction must be Direction enum, got {type(self.direction).__name__}")
        _validate_prob(self.fill_price, "fill_price")
        _validate_prob(self.fair_mid_devigged, "fair_mid_devigged")
        _validate_iso_datetime(self.scan_ts_utc, "scan_ts_utc")
        if not self.model_version:
            raise ValueError("model_version required (e.g. 'claude-sonnet-4-6-20250514')")
        if not self.prompt_hash_hex or len(self.prompt_hash_hex) < 8:
            raise ValueError("prompt_hash_hex required")
        if not self.fee_schedule_version:
            raise ValueError("fee_schedule_version required")
        if self.excluded and not self.exclusion_reason:
            raise ValueError("excluded=True requires exclusion_reason")

    def to_dict(self) -> dict:
        """JSON-serializable representation."""
        d = asdict(self)
        d["scan_date"] = self.scan_date.isoformat()
        if self.resolution_date_declared:
            d["resolution_date_declared"] = self.resolution_date_declared.isoformat()
        d["model"] = self.model.value
        d["direction"] = self.direction.value
        d["market_snapshot"]["status"] = self.market_snapshot.status.value
        d["output"]["confidence"] = self.output.confidence.value
        d["output"]["category"] = self.output.category.value
        return d


# ---------------------------------------------------------------------------
# Alert — the decision to recommend (or silently track) a trade
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Alert:
    """
    An Alert wraps one or more Predictions into an actionable recommendation.
    - CONSENSUS: built from a Claude prediction AND a Grok prediction that agree
    - CLAUDE_SOLO: built from a Claude prediction when Grok disagrees or is silent
    - GROK_SOLO: built from a Grok prediction when Claude disagrees or is silent
                 (tracked for scoring, NEVER sent to Telegram)
    """
    scan_id: str
    scan_date: date
    alert_ts_utc: str
    tier: AlertTier

    ticker: str
    direction: Direction

    # Composite numbers — derived from the underlying predictions
    fill_price: float                 # The actionable price (yes_ask or no_ask)
    max_acceptable_price: float       # Above this, don't take the trade
    edge_cents: int                   # (model_prob - fair_mid) * 100, rounded
    consensus_prob_yes: float         # Avg of underlying models if consensus, else solo model's prob
    fair_mid_devigged: float          # Same for all models (market state is shared)

    # Traceability
    source_scan_ids: list[str]        # Which Predictions are behind this Alert
    reasoning: str

    def __post_init__(self):
        if not isinstance(self.tier, AlertTier):
            raise ValueError(f"tier must be AlertTier enum")
        if not isinstance(self.direction, Direction):
            raise ValueError(f"direction must be Direction enum")
        _validate_prob(self.fill_price, "fill_price")
        _validate_prob(self.max_acceptable_price, "max_acceptable_price")
        _validate_prob(self.consensus_prob_yes, "consensus_prob_yes")
        _validate_prob(self.fair_mid_devigged, "fair_mid_devigged")
        _validate_iso_datetime(self.alert_ts_utc, "alert_ts_utc")
        if self.max_acceptable_price < self.fill_price:
            raise ValueError(
                f"max_acceptable_price ({self.max_acceptable_price}) "
                f"< fill_price ({self.fill_price}): negative room for slippage"
            )
        if not self.source_scan_ids:
            raise ValueError("source_scan_ids required (which predictions produced this alert)")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scan_date"] = self.scan_date.isoformat()
        d["tier"] = self.tier.value
        d["direction"] = self.direction.value
        return d


# ---------------------------------------------------------------------------
# Settlement — raw settlement data from Kalshi
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settlement:
    """Snapshot of a market at/near settlement. Used for scoring and CLV."""
    ticker: str
    outcome_yes: int                  # 0 or 1
    settlement_ts_utc: str
    close_time_utc: str

    # Near-close market state, for CLV
    close_yes_bid_cents: Optional[int]
    close_yes_ask_cents: Optional[int]
    close_no_bid_cents: Optional[int]
    close_no_ask_cents: Optional[int]
    close_fair_mid_devigged: Optional[float]

    raw_json: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.outcome_yes not in (0, 1):
            raise ValueError(f"outcome_yes must be 0 or 1, got {self.outcome_yes}")
        _validate_iso_datetime(self.settlement_ts_utc, "settlement_ts_utc")
        _validate_iso_datetime(self.close_time_utc, "close_time_utc")
        for name, val in [
            ("close_yes_bid_cents", self.close_yes_bid_cents),
            ("close_yes_ask_cents", self.close_yes_ask_cents),
            ("close_no_bid_cents", self.close_no_bid_cents),
            ("close_no_ask_cents", self.close_no_ask_cents),
        ]:
            if val is not None:
                _validate_cents(val, name)
        if self.close_fair_mid_devigged is not None:
            _validate_prob(self.close_fair_mid_devigged, "close_fair_mid_devigged")


# ---------------------------------------------------------------------------
# Resolution — a Prediction + a Settlement = a scored outcome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Resolution:
    """
    The scored outcome of a Prediction. This is what feeds the report.
    Alert tier is captured so we can segment performance: consensus vs solo vs unalerted.
    """
    scan_id: str                      # Links back to Prediction
    ticker: str
    scan_date: date
    model: ModelName
    alert_tier: AlertTier             # Was this prediction alerted, and at what tier

    # Outcome
    outcome_yes: int
    settlement_ts_utc: str

    # Scoring
    brier: float                      # (model_prob - outcome)^2
    brier_market_baseline: float      # (fair_mid_devigged - outcome)^2
    brier_edge: float                 # market - model; positive = model beat market
    log_loss: float

    # CLV
    clv: Optional[float]              # Near-close fair_mid minus scan-time fair_mid, direction-adj

    # P&L (honest)
    gross_pnl: float                  # 1 - fill if win, -fill if lose (symmetric for YES/NO)
    fees_entry: float                 # Entry fee per Kalshi fee_schedule_version
    fees_exit: float                  # 0.0 for hold-to-settlement (the default)
    net_pnl: float                    # gross - (fees_entry + fees_exit)

    # Provenance
    fee_schedule_version: str
    resolved_at_utc: str

    def __post_init__(self):
        if self.outcome_yes not in (0, 1):
            raise ValueError(f"outcome_yes must be 0 or 1")
        if not isinstance(self.model, ModelName):
            raise ValueError(f"model must be ModelName enum")
        if not isinstance(self.alert_tier, AlertTier):
            raise ValueError(f"alert_tier must be AlertTier enum")
        _validate_iso_datetime(self.settlement_ts_utc, "settlement_ts_utc")
        _validate_iso_datetime(self.resolved_at_utc, "resolved_at_utc")
        if self.fees_entry < 0 or self.fees_exit < 0:
            raise ValueError("fees cannot be negative")
        # Sanity: net = gross - fees (allow tiny float slop)
        expected_net = self.gross_pnl - (self.fees_entry + self.fees_exit)
        if abs(self.net_pnl - expected_net) > 1e-6:
            raise ValueError(
                f"net_pnl inconsistency: {self.net_pnl} vs expected {expected_net}"
            )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scan_date"] = self.scan_date.isoformat()
        d["model"] = self.model.value
        d["alert_tier"] = self.alert_tier.value
        return d
