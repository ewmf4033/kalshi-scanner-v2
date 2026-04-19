"""
alert/format.py — Convert Decisions into Alerts.

Responsibilities:
    1. Take a Decision + the underlying Predictions for one ticker
    2. Compute edge: |consensus_prob - fair_mid_devigged|
    3. Pick direction: YES if consensus > market, NO otherwise
    4. Determine tier per schema rules:
       CONSENSUS    — Claude + Grok agree on direction with edge > threshold
       CLAUDE_SOLO  — Only Claude has edge (Grok dropped/disagrees)
       GROK_SOLO    — Only Grok has edge (Claude dropped/disagrees) — tracked, not sent
       NONE         — no actionable edge anywhere
    5. Apply category-specific edge thresholds from config
    6. Compose human-readable reasoning with attribution

Why not just use consensus_prob:
    The schema explicitly distinguishes CONSENSUS from CLAUDE_SOLO because
    they have different reliability profiles. Two-model agreement is
    meaningfully different from one model's confident take.

Output: Alert with tier=NONE for non-actionable, otherwise full Alert.
"""

from __future__ import annotations

from datetime import datetime, timezone, date
from typing import List, Optional, Tuple

from core import config
from core.schema import (
    Alert, AlertTier, Direction, ModelName, Prediction, Category,
)
from synth.consensus import Decision


# Category → edge threshold lookup with safe default
_EDGE_FALLBACK_CENTS = 6


def _edge_threshold_cents(cat: Category) -> int:
    """Lookup category-specific edge threshold. Falls back if category absent."""
    cat_str = cat.value
    return config.EDGE_THRESHOLD_CENTS.get(cat_str, _EDGE_FALLBACK_CENTS)


def _pick_direction_and_fill(
    consensus_prob: float,
    market_fair: float,
    yes_ask_cents: int,
    no_ask_cents: int,
) -> Tuple[Direction, float, float]:
    """
    Direction = side where the market is mispricing relative to consensus.
        consensus > market → YES is underpriced → trade YES
        consensus < market → YES is overpriced → trade NO
    The 0.5 line is irrelevant. A 0.20 consensus vs a 0.05 market is YES.
    A 0.80 consensus vs a 0.95 market is NO.

    fill_price = ask for the side we're taking.
    max_acceptable = fill + 2c slippage buffer, capped at 0.99.
    """
    if consensus_prob > market_fair:
        direction = Direction.YES
        fill = yes_ask_cents / 100.0
    else:
        direction = Direction.NO
        fill = no_ask_cents / 100.0

    max_acc = min(0.99, round(fill + 0.02, 4))
    return direction, fill, max_acc


def _direction_for_model(model_prob: float, market_fair: float) -> Optional[Direction]:
    """What direction would this single model trade? None if it's effectively neutral."""
    diff = model_prob - market_fair
    if abs(diff) < 0.005:   # essentially flat — no direction
        return None
    return Direction.YES if diff > 0 else Direction.NO


def build_alert(
    decision: Decision,
    predictions: List[Prediction],
) -> Alert:
    """
    Build one Alert from a Decision + the underlying Predictions.
    Always returns an Alert — tier=NONE if not actionable.

    All predictions must share the ticker.
    """
    if not predictions:
        raise ValueError("build_alert requires at least one prediction")
    ticker = predictions[0].ticker
    if any(p.ticker != ticker for p in predictions):
        raise ValueError("predictions must share ticker")

    snap = predictions[0].market_snapshot
    fair_mid = predictions[0].fair_mid_devigged
    scan_id = predictions[0].scan_id
    scan_date_v = predictions[0].scan_date
    category = predictions[0].output.category

    threshold_cents = _edge_threshold_cents(category)

    # Pull per-model results for tier logic
    by_model = {p.model: p for p in predictions}
    claude_pred = by_model.get(ModelName.CLAUDE)
    grok_pred = by_model.get(ModelName.GROK)

    claude_dir = (
        _direction_for_model(claude_pred.output.model_prob_yes, fair_mid)
        if claude_pred and claude_pred.model.value not in decision.dropped_models
        else None
    )
    grok_dir = (
        _direction_for_model(grok_pred.output.model_prob_yes, fair_mid)
        if grok_pred and grok_pred.model.value not in decision.dropped_models
        else None
    )

    # No usable consensus = no alert
    if decision.consensus_prob_yes is None:
        return _none_alert(scan_id, scan_date_v, ticker, snap, fair_mid,
                           "all models returned no-edge")

    consensus_prob = decision.consensus_prob_yes
    direction, fill, max_acc = _pick_direction_and_fill(
        consensus_prob, fair_mid, snap.yes_ask_cents, snap.no_ask_cents,
    )

    # Edge in cents — always positive (direction-picker handled the sign)
    edge_decimal = abs(consensus_prob - fair_mid)
    edge_cents = round(edge_decimal * 100)

    # Tier decision per schema
    if edge_cents < threshold_cents:
        return _none_alert(scan_id, scan_date_v, ticker, snap, fair_mid,
                           f"edge {edge_cents}c below threshold {threshold_cents}c "
                           f"for category {category.value}",
                           consensus_prob=consensus_prob)

    # Edge is sufficient. What tier?
    tier = _decide_tier(claude_dir, grok_dir, direction)
    if tier == AlertTier.NONE:
        return _none_alert(scan_id, scan_date_v, ticker, snap, fair_mid,
                           "edge present but no model agrees with the consensus direction",
                           consensus_prob=consensus_prob)

    # Compose reasoning
    reasoning = _format_reasoning(decision, predictions, direction, edge_cents,
                                  tier, fair_mid, consensus_prob)

    return Alert(
        scan_id=scan_id,
        scan_date=scan_date_v,
        alert_ts_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        tier=tier,
        ticker=ticker,
        direction=direction,
        fill_price=fill,
        max_acceptable_price=max_acc,
        edge_cents=edge_cents,
        consensus_prob_yes=round(consensus_prob, 4),
        fair_mid_devigged=round(fair_mid, 4),
        source_scan_ids=[scan_id],
        reasoning=reasoning,
    )


def _decide_tier(
    claude_dir: Optional[Direction],
    grok_dir: Optional[Direction],
    consensus_dir: Direction,
) -> AlertTier:
    """Per schema: CONSENSUS = both agree; CLAUDE_SOLO = only Claude agrees;
    GROK_SOLO = only Grok agrees; NONE = neither agrees with consensus."""
    claude_ok = claude_dir == consensus_dir
    grok_ok = grok_dir == consensus_dir
    if claude_ok and grok_ok:
        return AlertTier.CONSENSUS
    if claude_ok:
        return AlertTier.CLAUDE_SOLO
    if grok_ok:
        return AlertTier.GROK_SOLO
    return AlertTier.NONE


def _format_reasoning(
    decision: Decision,
    predictions: List[Prediction],
    direction: Direction,
    edge_cents: int,
    tier: AlertTier,
    fair_mid: float,
    consensus_prob: float,
) -> str:
    parts = [
        f"{tier.value.upper()}: {direction.value} @ "
        f"consensus {consensus_prob*100:.1f}% vs market {fair_mid*100:.1f}% "
        f"(edge {edge_cents:+d}c)",
    ]
    # Per-model attribution
    by_model = {p.model.value: p for p in predictions}
    for mn in ["claude", "grok", "gemini_shadow"]:
        if mn not in by_model:
            continue
        p = by_model[mn]
        weight = decision.per_model_weights.get(mn, 0.0)
        marker = "DROPPED" if mn in decision.dropped_models else f"w={weight:.2f}"
        parts.append(
            f"  {mn}: {p.output.model_prob_yes*100:.1f}% "
            f"[{p.output.prob_range_lo*100:.0f}-{p.output.prob_range_hi*100:.0f}] "
            f"{p.output.confidence.value} ({marker})"
        )
    # Attach Claude's catalyst as the headline reasoning
    claude_p = by_model.get("claude")
    if claude_p:
        parts.append(f"Catalyst: {claude_p.output.catalyst}")
    return "\n".join(parts)


def _none_alert(
    scan_id: str, scan_date_v: date, ticker: str, snap, fair_mid: float,
    reason: str, consensus_prob: Optional[float] = None,
) -> Alert:
    """Build a NONE-tier Alert for tracking purposes — never sent to Telegram."""
    return Alert(
        scan_id=scan_id,
        scan_date=scan_date_v,
        alert_ts_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        tier=AlertTier.NONE,
        ticker=ticker,
        direction=Direction.YES,  # placeholder, not actionable
        fill_price=snap.yes_ask_cents / 100.0,
        max_acceptable_price=min(0.99, round(snap.yes_ask_cents / 100.0 + 0.02, 4)),
        edge_cents=0,
        consensus_prob_yes=round(consensus_prob if consensus_prob is not None else fair_mid, 4),
        fair_mid_devigged=round(fair_mid, 4),
        source_scan_ids=[scan_id],
        reasoning=f"NONE: {reason}",
    )


def build_alerts(
    decisions: List[Decision],
    predictions: List[Prediction],
) -> List[Alert]:
    """Build alerts for an entire scan."""
    from collections import defaultdict
    preds_by_ticker = defaultdict(list)
    for p in predictions:
        preds_by_ticker[p.ticker].append(p)

    alerts = []
    for d in decisions:
        ticker_preds = preds_by_ticker.get(d.ticker, [])
        if not ticker_preds:
            continue
        alerts.append(build_alert(d, ticker_preds))
    return alerts
