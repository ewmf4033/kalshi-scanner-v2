"""
synth/consensus.py — Combine multiple model predictions into one decision.

Responsibilities:
    1. Group Predictions by ticker
    2. For each ticker's set of model votes, decide the consensus
       prob_yes via inverse-variance weighting
    3. Drop "no-edge" votes (model_prob_yes ≈ 0.5 with low confidence)
       — they pollute the average
    4. Emit a Decision dataclass per ticker — input to alert.py

Why inverse-variance weighting:
    Each LLM gives a point estimate + range. A tight range = high
    confidence = should be weighted more. Inverse-variance is the
    optimal Bayesian combine for normal-ish posteriors.

    weight_i = 1 / variance_i
    consensus_prob = sum(weight_i * prob_i) / sum(weight_i)

Why drop no-edge:
    Our prompts instruct LLMs to output 0.5/[0.35,0.65]/low when they
    lack data. Those votes are honest "I don't know" sentinels — they
    should not pull the consensus toward 0.5. They are not data; they
    are absence of data.

What we do NOT do here:
    - No alert formatting / threshold checking — that's alert/format.py
    - No persistence — caller decides what to do with Decisions
    - No Telegram sending — alert/telegram.py
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.schema import (
    Prediction, ModelName, Confidence, Direction, ModelOutput,
)


# Sentinel detection: outputs that match our "no-edge fallback" template
NO_EDGE_PROB_CENTER = 0.5
NO_EDGE_PROB_TOLERANCE = 0.02   # 0.48–0.52 counts as no-edge if confidence=low


@dataclass(frozen=True)
class Decision:
    """
    Per-ticker combined decision. Input to alert/format.py.

    Holds the consensus point estimate and the per-model votes that
    contributed (or were dropped). Caller (alert layer) picks direction
    and computes edge against market fair price.
    """
    ticker: str
    n_votes_total: int           # how many models scored this ticker
    n_votes_used: int            # how many were not dropped as no-edge
    consensus_prob_yes: Optional[float]  # None if all votes dropped
    consensus_range_lo: Optional[float]
    consensus_range_hi: Optional[float]
    per_model_probs: Dict[str, float]   # for audit / display
    per_model_weights: Dict[str, float]
    dropped_models: List[str]    # which models were dropped as no-edge


def is_no_edge(out: ModelOutput) -> bool:
    """A vote is 'no-edge' if it matches our prompt's no-edge sentinel:
    point ≈ 0.5 AND confidence == low. Other low-confidence votes that
    happen to land near 0.5 by analysis are kept (their range is honest)."""
    if out.confidence != Confidence.LOW:
        return False
    return abs(out.model_prob_yes - NO_EDGE_PROB_CENTER) <= NO_EDGE_PROB_TOLERANCE


def _confidence_to_variance(out: ModelOutput) -> float:
    """Variance proxy from the model's stated range.
    Range as ~95% CI: stdev ≈ width / 4. Variance = stdev^2.
    Floor at small positive number to avoid divide-by-zero."""
    width = out.prob_range_hi - out.prob_range_lo
    stdev = max(width / 4.0, 0.01)
    return stdev * stdev


def combine_predictions(preds: List[Prediction]) -> Decision:
    """Inverse-variance weighted combine of one ticker's votes.
    All preds must share the same ticker — caller's responsibility."""
    if not preds:
        raise ValueError("combine_predictions requires at least one Prediction")
    ticker = preds[0].ticker
    if any(p.ticker != ticker for p in preds):
        raise ValueError(f"all predictions must share ticker, got mixed")

    per_model_probs: Dict[str, float] = {}
    per_model_weights: Dict[str, float] = {}
    dropped: List[str] = []
    used: List[Tuple[str, float, float]] = []  # (model_name, prob, variance)

    for p in preds:
        mn = p.model.value
        per_model_probs[mn] = p.output.model_prob_yes
        if is_no_edge(p.output):
            dropped.append(mn)
            per_model_weights[mn] = 0.0
            continue
        var = _confidence_to_variance(p.output)
        used.append((mn, p.output.model_prob_yes, var))

    if not used:
        return Decision(
            ticker=ticker,
            n_votes_total=len(preds),
            n_votes_used=0,
            consensus_prob_yes=None,
            consensus_range_lo=None,
            consensus_range_hi=None,
            per_model_probs=per_model_probs,
            per_model_weights=per_model_weights,
            dropped_models=dropped,
        )

    # Inverse-variance weighting
    weights = [(mn, 1.0 / var, prob) for mn, prob, var in used]
    total_w = sum(w for _, w, _ in weights)
    consensus = sum(w * prob for _, w, prob in weights) / total_w

    for mn, w, _ in weights:
        per_model_weights[mn] = round(w / total_w, 4)

    # Combined uncertainty:
    #   1. Inverse-variance combined stdev of the mean
    #   2. Spread between model point estimates
    # Take the larger — disagreement among models is its own uncertainty
    combined_se = (1.0 / total_w) ** 0.5
    used_probs = [prob for _, _, prob in weights]
    spread = (max(used_probs) - min(used_probs)) / 2.0 if len(used_probs) > 1 else 0.0
    half_width = max(combined_se, spread, 0.025)  # floor at 5pp wide
    lo = max(0.01, round(consensus - half_width, 4))
    hi = min(0.99, round(consensus + half_width, 4))

    return Decision(
        ticker=ticker,
        n_votes_total=len(preds),
        n_votes_used=len(used),
        consensus_prob_yes=round(consensus, 4),
        consensus_range_lo=lo,
        consensus_range_hi=hi,
        per_model_probs=per_model_probs,
        per_model_weights=per_model_weights,
        dropped_models=dropped,
    )


def combine_scan(predictions: List[Prediction]) -> List[Decision]:
    """Group predictions by ticker, combine each group, return all Decisions."""
    by_ticker: Dict[str, List[Prediction]] = defaultdict(list)
    for p in predictions:
        by_ticker[p.ticker].append(p)
    return [combine_predictions(group) for group in by_ticker.values()]
