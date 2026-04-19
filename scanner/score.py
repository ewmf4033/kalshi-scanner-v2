"""
score.py — Scan orchestrator.

Responsibilities:
    1. Select candidate markets from ingest output (top N by volume,
       closing within N days).
    2. Call all three LLMs in parallel for each candidate.
    3. Wrap each LLM output in a fully-stamped Prediction (metadata
       set by orchestrator, NEVER by the LLM).
    4. Persist the scan as JSONL in raw/scans/.
    5. Return a ScanResult summary.

Contract:
    run_scan(now_utc=None) -> ScanResult
        - If now_utc is None, uses current UTC time.
        - Writes raw/scans/YYYY-MM-DD_HHMM_scan.jsonl.
        - Returns counts + output path + timing.

Design notes:
    - Parallelism: ThreadPoolExecutor. All (market × model) pairs are
      submitted to a single pool (default 10 workers). LLM calls are
      I/O-bound so threads are sufficient.
    - Failure isolation: one LLM failure -> one missing Prediction.
      Does NOT block the scan. Logged structurally.
    - Direction: always YES for the scan phase. Consensus/alert layer
      (Day 3) picks which side has edge vs the market.
    - Fee schedule version: pinned at prediction time, per schema.

Config:
    - CANDIDATE_MAX_N: 50 markets per scan
    - CANDIDATE_MAX_DAYS_TO_CLOSE: 14 days
    - MAX_WORKERS: 10 parallel LLM calls
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from core import config, kalshi_client as kc
from core.schema import (
    SCHEMA_VERSION, Direction, EnrichedMarket, ModelName, ModelOutput,
    Prediction,
)
from scanner import ingest, models, render
from scanner.price_math import devig_multiplicative


log = logging.getLogger(__name__)


# --- Tunables ---------------------------------------------------------------
CANDIDATE_MAX_N = 50
CANDIDATE_MAX_DAYS_TO_CLOSE = 14
MAX_WORKERS = 10
PROMPT_VERSION = "scanner-v2.0"
RAW_SCANS_DIR = Path("raw/scans")


# --- Model dispatch table ---------------------------------------------------
# The orchestrator stamps ModelName; the LLM cannot set its own label.
_MODEL_DISPATCH: List[Tuple[ModelName, str, Callable[[EnrichedMarket], Optional[ModelOutput]]]] = [
    (ModelName.CLAUDE,        models.CLAUDE_MODEL, models.call_claude),
    (ModelName.GROK,          models.GROK_MODEL,   models.call_grok),
    (ModelName.GEMINI_SHADOW, models.GEMINI_MODEL, models.call_gemini),
]


@dataclass
class ScanResult:
    scan_id: str
    scan_ts_utc: str
    candidates_considered: int
    candidates_selected: int
    predictions_by_model: dict     # {"claude": 48, "grok": 50, "gemini_shadow": 50}
    failures_by_model: dict        # {"claude": 2, ...}
    output_path: str
    elapsed_seconds: float


# --- Candidate selection ----------------------------------------------------

def select_candidates(
    enriched: List[EnrichedMarket],
    max_n: int = CANDIDATE_MAX_N,
    max_days: int = CANDIDATE_MAX_DAYS_TO_CLOSE,
) -> List[EnrichedMarket]:
    """Pick top-N markets closing within max_days, sorted by 24h volume.

    Why this filter:
        - Near-term markets give faster CLV feedback
        - Volume = liquidity = actual tradeability
        - Bounded cost: 50 × 3 models = 150 LLM calls per scan
    """
    max_minutes = max_days * 1440
    near_term = [e for e in enriched if e.minutes_to_close <= max_minutes]
    ranked = sorted(near_term, key=lambda e: -e.snapshot.volume_24h)
    return ranked[:max_n]


# --- Scoring one (market, model) pair ---------------------------------------

def _prompt_hash(prompt_text: str) -> str:
    """Short hash for audit — lets us verify the exact prompt seen by the LLM."""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]


def score_one(
    em: EnrichedMarket,
    model: ModelName,
    model_version: str,
    call_fn: Callable[[EnrichedMarket], Optional[ModelOutput]],
    scan_id: str,
    scan_ts_utc: str,
) -> Optional[Prediction]:
    """Call one LLM for one market; wrap the output in a Prediction.
    Returns None on any failure. NEVER raises."""
    try:
        output = call_fn(em)
        if output is None:
            return None

        snap = em.snapshot
        # Scanner stage is always YES-side for now; alerter picks direction later.
        direction = Direction.YES
        fill = snap.yes_ask_cents / 100.0
        fair = devig_multiplicative(snap.yes_ask_cents / 100.0, snap.no_ask_cents / 100.0)

        # Hash the actual prompt that was rendered (matches what the LLM saw).
        # Note: we reconstruct here rather than thread it from models.py to keep
        # the interface clean. The _date_preamble addition is deterministic
        # enough that same scan run produces same hash.
        prompt_text = render.render_prompt(em)
        phash = _prompt_hash(prompt_text)

        pred = Prediction(
            scan_id=scan_id,
            schema_version=SCHEMA_VERSION,
            scan_date=date.fromisoformat(scan_ts_utc[:10]),
            scan_ts_utc=scan_ts_utc,
            model=model,                       # stamped by orchestrator
            model_version=model_version,
            prompt_version=PROMPT_VERSION,
            prompt_hash_hex=phash,
            fee_schedule_version=config.FEE_SCHEDULE_VERSION,
            ticker=em.ticker,
            market_snapshot=snap,
            direction=direction,
            fill_price=fill,
            fair_mid_devigged=fair,
            output=output,
            correlation_cluster=None,
            resolution_date_declared=None,
        )
        return pred
    except Exception as e:
        log.error(json.dumps({
            "phase": "score_one",
            "ticker": em.ticker,
            "model": model.value,
            "err": f"{type(e).__name__}: {str(e)[:200]}",
        }))
        return None


# --- Parallel scoring -------------------------------------------------------

def score_parallel(
    candidates: List[EnrichedMarket],
    scan_id: str,
    scan_ts_utc: str,
    max_workers: int = MAX_WORKERS,
) -> List[Prediction]:
    """Submit all (market × model) pairs to a thread pool, collect results."""
    jobs = []
    for em in candidates:
        for model_name, model_version, call_fn in _MODEL_DISPATCH:
            jobs.append((em, model_name, model_version, call_fn))

    preds: List[Prediction] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(score_one, em, mn, mv, fn, scan_id, scan_ts_utc): (em.ticker, mn)
            for em, mn, mv, fn in jobs
        }
        for fut in as_completed(futures):
            ticker, mn = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                log.error(json.dumps({
                    "phase": "score_parallel",
                    "ticker": ticker, "model": mn.value,
                    "err": f"{type(e).__name__}: {str(e)[:200]}",
                }))
                result = None
            if result is not None:
                preds.append(result)
    return preds


# --- Persistence ------------------------------------------------------------

def write_predictions(preds: List[Prediction], path: Path) -> None:
    """Append-only JSONL. Each line is a Prediction.to_dict()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for p in preds:
            f.write(json.dumps(p.to_dict(), default=str) + "\n")


# --- Top-level: one scan ----------------------------------------------------

def run_scan(
    now_utc: Optional[datetime] = None,
    max_n: int = CANDIDATE_MAX_N,
    max_days: int = CANDIDATE_MAX_DAYS_TO_CLOSE,
) -> ScanResult:
    """End-to-end: pull → filter → select → score → persist."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    scan_id = str(uuid.uuid4())
    start = time.time()

    api_key_id = os.environ["KALSHI_API_KEY_ID"]
    pk = kc.load_private_key(os.environ["KALSHI_PRIVATE_KEY_PATH"])

    log.info(json.dumps({"phase": "run_scan", "stage": "pull", "scan_id": scan_id}))
    snaps = list(kc.pull_all_open_markets(api_key_id, pk, captured_at_utc=now_iso))

    log.info(json.dumps({"phase": "run_scan", "stage": "filter",
                         "snapshots": len(snaps)}))
    enriched, _stats = ingest.filter_and_enrich_batch(snaps, now_utc=now_iso)

    candidates = select_candidates(enriched, max_n=max_n, max_days=max_days)
    log.info(json.dumps({"phase": "run_scan", "stage": "select",
                         "enriched": len(enriched), "candidates": len(candidates)}))

    preds = score_parallel(candidates, scan_id, now_iso)

    # Per-model counts
    by_model = {mn.value: 0 for mn, _, _ in _MODEL_DISPATCH}
    for p in preds:
        by_model[p.model.value] += 1
    failures = {mn.value: len(candidates) - by_model[mn.value]
                for mn, _, _ in _MODEL_DISPATCH}

    fname = now_utc.strftime("%Y-%m-%d_%H%M_scan.jsonl")
    out_path = RAW_SCANS_DIR / fname
    write_predictions(preds, out_path)

    elapsed = time.time() - start
    result = ScanResult(
        scan_id=scan_id,
        scan_ts_utc=now_iso,
        candidates_considered=len(enriched),
        candidates_selected=len(candidates),
        predictions_by_model=by_model,
        failures_by_model=failures,
        output_path=str(out_path),
        elapsed_seconds=elapsed,
    )
    log.info(json.dumps({"phase": "run_scan", "stage": "done",
                         "elapsed_s": round(elapsed, 1),
                         "preds_by_model": by_model,
                         "failures": failures,
                         "path": str(out_path)}))
    return result
