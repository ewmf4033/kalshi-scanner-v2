"""
alert/digest.py — Daily scan summary delivered via Telegram.

Sent ONCE per scan, after individual alerts go out. Different from
per-alert messages: this is the operator's morning glance — "did the
scan run, what came out, is anything broken."

Format: single plain-text Telegram message.
Failure: logged but never raises — scan completes regardless.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from typing import List

import httpx

from core.schema import Alert, AlertTier, ModelName, Prediction


log = logging.getLogger(__name__)
TELEGRAM_API_BASE = "https://api.telegram.org"
SEND_TIMEOUT = 10.0


def format_digest(
    scan_id: str,
    scan_ts_utc: str,
    candidates_considered: int,
    candidates_selected: int,
    predictions: List[Prediction],
    alerts: List[Alert],
    failures_by_model: dict,
) -> str:
    """Build the human-readable summary text."""
    # Per-tier counts
    tier_counts = Counter(a.tier for a in alerts)
    n_consensus = tier_counts.get(AlertTier.CONSENSUS, 0)
    n_claude = tier_counts.get(AlertTier.CLAUDE_SOLO, 0)
    n_grok = tier_counts.get(AlertTier.GROK_SOLO, 0)
    n_none = tier_counts.get(AlertTier.NONE, 0)

    # Top edges (any actionable tier)
    actionable = [a for a in alerts if a.tier in (
        AlertTier.CONSENSUS, AlertTier.CLAUDE_SOLO, AlertTier.GROK_SOLO,
    )]
    actionable.sort(key=lambda a: -a.edge_cents)
    top_edges = actionable[:3]

    # Per-model contribution
    by_model: dict = {mn.value: {"real": 0, "dropped": 0} for mn in ModelName}
    # Real = prediction not at no-edge sentinel
    for p in predictions:
        is_no_edge = (
            p.output.confidence.value == "low"
            and abs(p.output.model_prob_yes - 0.5) <= 0.02
        )
        bucket = "dropped" if is_no_edge else "real"
        by_model[p.model.value][bucket] += 1

    # Build message
    lines = []
    lines.append(f"📊 Kalshi v2 Daily — {scan_ts_utc[:10]}")
    lines.append("")
    lines.append(
        f"Markets considered: {candidates_considered:,} | "
        f"Selected: {candidates_selected}"
    )
    fail_str = " | ".join(f"{m}={c}" for m, c in failures_by_model.items() if c > 0)
    if fail_str:
        lines.append(f"⚠️ LLM failures: {fail_str}")

    lines.append("")
    lines.append(f"Alerts: 🟢 {n_consensus} CONSENSUS · 🔵 {n_claude} CLAUDE_SOLO · "
                 f"🟡 {n_grok} GROK_SOLO · ⚪ {n_none} NONE")

    if top_edges:
        lines.append("")
        lines.append("Top edges:")
        for a in top_edges:
            tier_short = {
                AlertTier.CONSENSUS:   "consensus",
                AlertTier.CLAUDE_SOLO: "claude",
                AlertTier.GROK_SOLO:   "grok",
            }.get(a.tier, "?")
            lines.append(
                f"  {a.ticker} — {a.direction.value} @ {a.fill_price:.2f}, "
                f"+{a.edge_cents}c ({tier_short})"
            )

    lines.append("")
    lines.append("Model contribution:")
    for mn in [ModelName.CLAUDE, ModelName.GROK, ModelName.GEMINI_SHADOW]:
        stats = by_model.get(mn.value, {"real": 0, "dropped": 0})
        total = stats["real"] + stats["dropped"]
        if total == 0:
            continue
        no_edge_pct = round(100 * stats["dropped"] / total)
        lines.append(
            f"  {mn.value}: {stats['real']} real, {stats['dropped']} no-edge ({no_edge_pct}%)"
        )

    return "\n".join(lines)


def send_digest(message: str) -> bool:
    """POST the digest to Telegram. Returns True on success."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error(json.dumps({
            "phase": "send_digest",
            "err": "TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set",
        }))
        return False

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    try:
        r = httpx.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=SEND_TIMEOUT,
        )
        if r.status_code != 200:
            log.error(json.dumps({
                "phase": "send_digest",
                "status": r.status_code,
                "body": r.text[:200],
            }))
            return False
        return True
    except Exception as e:
        log.error(json.dumps({
            "phase": "send_digest",
            "err": f"{type(e).__name__}: {str(e)[:200]}",
        }))
        return False
