"""
alert/telegram.py — Send alerts to Telegram bot @masta_op2_bot.

Responsibilities:
    1. Format Alert objects as human-readable Telegram messages
    2. POST to Telegram bot API
    3. Filter: only CONSENSUS and CLAUDE_SOLO are sent.
       GROK_SOLO is tracked in JSONL but never delivered (per schema).
       NONE is never sent.
    4. Failure isolated — one Telegram error doesn't abort the scan.

Env:
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import json
import logging
import os
from typing import List

import httpx

from core.schema import Alert, AlertTier


log = logging.getLogger(__name__)


TELEGRAM_API_BASE = "https://api.telegram.org"
SEND_TIMEOUT = 10.0


# Tiers that get delivered to Telegram. NONE and GROK_SOLO never sent.
DELIVERABLE_TIERS = {AlertTier.CONSENSUS, AlertTier.CLAUDE_SOLO}


def format_alert(a: Alert) -> str:
    """Format a single alert as a plain-text Telegram message.
    Plain text — no Markdown — because Telegram's Markdown parser
    chokes on common chars in market reasoning (asterisks, underscores,
    brackets). Plain text is robust."""
    tier_emoji = {
        AlertTier.CONSENSUS:   "🟢",
        AlertTier.CLAUDE_SOLO: "🔵",
        AlertTier.GROK_SOLO:   "🟡",
        AlertTier.NONE:        "⚪",
    }
    emoji = tier_emoji.get(a.tier, "⚪")
    direction_emoji = "📈" if a.direction.value == "YES" else "📉"
    edge_sign = "+" if a.edge_cents >= 0 else ""
    consensus_pct = a.consensus_prob_yes * 100
    market_pct = a.fair_mid_devigged * 100

    lines = [
        f"{emoji} {a.tier.value.upper()} — {a.ticker}",
        f"{direction_emoji} {a.direction.value} @ {a.fill_price:.2f} (max: {a.max_acceptable_price:.2f})",
        f"📊 Model: {consensus_pct:.1f}% | Market: {market_pct:.1f}% | Edge: {edge_sign}{a.edge_cents}c",
        "",
        a.reasoning,
    ]
    return "\n".join(lines)


def send_alert(a: Alert) -> bool:
    """POST one alert to Telegram. Returns True on success, False on failure.
    Never raises — failure is logged and swallowed."""
    if a.tier not in DELIVERABLE_TIERS:
        return False  # silently skip — not an error

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error(json.dumps({
            "phase": "send_alert",
            "ticker": a.ticker,
            "err": "TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set",
        }))
        return False

    text = format_alert(a)
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"

    try:
        r = httpx.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=SEND_TIMEOUT,
        )
        if r.status_code != 200:
            log.error(json.dumps({
                "phase": "send_alert",
                "ticker": a.ticker,
                "status": r.status_code,
                "body": r.text[:200],
            }))
            return False
        return True
    except Exception as e:
        log.error(json.dumps({
            "phase": "send_alert",
            "ticker": a.ticker,
            "err": f"{type(e).__name__}: {str(e)[:200]}",
        }))
        return False


def send_alerts(alerts: List[Alert]) -> dict:
    """Send a batch. Returns counts: {sent, skipped, failed}."""
    sent = skipped = failed = 0
    for a in alerts:
        if a.tier not in DELIVERABLE_TIERS:
            skipped += 1
            continue
        if send_alert(a):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "skipped": skipped, "failed": failed}
