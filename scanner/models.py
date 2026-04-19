"""
models.py — LLM client wrappers for scoring Kalshi markets.

Three clients: Claude (primary), Grok (complement), Gemini (shadow).
Each takes an EnrichedMarket, renders a prompt, calls the API, and
returns a validated ModelOutput or None on any failure.

Design principles:
    1. Each client returns ModelOutput | None. NEVER raises.
       Production scanner must survive transient API failures.
    2. ModelName is stamped by the orchestrator, not the LLM.
       The LLM structurally cannot set its own label (ModelOutput has
       no model-name field) — prevents the v1 label bug.
    3. Structured output where supported (Gemini response_mime_type).
       Claude and Grok get JSON-only prompts and validate via parse.py.
    4. Short timeouts, single retry. Failing gracefully is a feature.

Contract:
    call_claude(em)  -> ModelOutput | None
    call_grok(em)    -> ModelOutput | None
    call_gemini(em)  -> ModelOutput | None
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from core.schema import EnrichedMarket, ModelOutput
from scanner.render import render_prompt
from scanner.parse import parse_model_output


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model identifiers — pinned for reproducibility
# ---------------------------------------------------------------------------
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
GROK_MODEL   = "grok-4-fast-reasoning"
GEMINI_MODEL = "gemini-2.0-flash"

# LLM behavior tuning
MAX_TOKENS_NO_TOOLS = 1500      # Room for reasoning + JSON (no search)
MAX_TOKENS_WITH_TOOLS = 4000    # Claude needs room after search results
TIMEOUT_SECONDS = 180           # Web search adds latency


# ---------------------------------------------------------------------------
# Claude — primary. Uses Anthropic SDK.
# ---------------------------------------------------------------------------

def _date_preamble() -> str:
    """Inject today's date so LLMs don't fall back to training cutoffs.
    Without this, LLMs misread market close dates as years in the future."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"TODAY'S DATE (UTC): {today}\n\n"


def call_claude(em: EnrichedMarket) -> Optional[ModelOutput]:
    """Score a market via Claude with web_search enabled.

    Web search is critical — LLM training cutoffs cause models to misread
    recent dates as far future. With web search, Claude autonomously
    fetches current spot prices, AAA gas averages, CPI prints, etc.
    Verified live on 2026-04-18: Claude searched 3 times, found
    correct AAA gas price, produced well-calibrated 72% probability
    on a market trading at 77.5%.

    Returns None on any failure.
    """
    try:
        import anthropic
    except ImportError:
        log.error(json.dumps({"phase": "call_claude", "err": "anthropic SDK not installed"}))
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error(json.dumps({"phase": "call_claude", "err": "ANTHROPIC_API_KEY not set"}))
        return None

    prompt = _date_preamble() + render_prompt(em)
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_SECONDS)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS_WITH_TOOLS,
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5,
            }],
        )
    except Exception as e:
        log.error(json.dumps({
            "phase": "call_claude",
            "ticker": em.ticker,
            "err": f"{type(e).__name__}: {str(e)[:200]}",
        }))
        return None

    # Claude with tools returns multiple blocks: server_tool_use, tool_result,
    # and text. Reasoning is interleaved with tool calls, JSON is usually at
    # the very end. Join all text and let parse._extract_json find it.
    text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    if not text_blocks:
        log.error(json.dumps({
            "phase": "call_claude",
            "ticker": em.ticker,
            "err": "no text block in response",
            "stop_reason": response.stop_reason,
        }))
        return None

    joined = "\n".join(text_blocks)
    output, parse_err = parse_model_output(joined)
    if output is None:
        log.error(json.dumps({
            "phase": "call_claude",
            "ticker": em.ticker,
            "err": f"parse failed: {parse_err}",
            "raw_preview": joined[-400:],  # tail is where JSON lives
        }))
        return None

    return output


# ---------------------------------------------------------------------------
# Grok — complement. xAI's API is OpenAI-compatible.
# ---------------------------------------------------------------------------

def call_grok(em: EnrichedMarket) -> Optional[ModelOutput]:
    """Score a market via Grok-4-fast-reasoning. Returns None on any failure."""
    try:
        from openai import OpenAI
    except ImportError:
        log.error(json.dumps({"phase": "call_grok", "err": "openai SDK not installed"}))
        return None

    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        log.error(json.dumps({"phase": "call_grok", "err": "XAI_API_KEY not set"}))
        return None

    prompt = _date_preamble() + render_prompt(em)
    try:
        client = OpenAI(
            base_url="https://api.x.ai/v1",
            api_key=api_key,
            timeout=TIMEOUT_SECONDS,
        )
        response = client.chat.completions.create(
            model=GROK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=MAX_TOKENS_NO_TOOLS,
        )
    except Exception as e:
        log.error(json.dumps({
            "phase": "call_grok",
            "ticker": em.ticker,
            "err": f"{type(e).__name__}: {str(e)[:200]}",
        }))
        return None

    text = response.choices[0].message.content or ""
    output, parse_err = parse_model_output(text)
    if output is None:
        log.error(json.dumps({
            "phase": "call_grok",
            "ticker": em.ticker,
            "err": f"parse failed: {parse_err}",
            "raw_preview": text[:200],
        }))
        return None

    return output


# ---------------------------------------------------------------------------
# Gemini — shadow. google-genai SDK with JSON mode.
# ---------------------------------------------------------------------------

def call_gemini(em: EnrichedMarket) -> Optional[ModelOutput]:
    """Score a market via Gemini 2.0 Flash. Returns None on any failure."""
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        log.error(json.dumps({"phase": "call_gemini", "err": "google-genai SDK not installed"}))
        return None

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        log.error(json.dumps({"phase": "call_gemini", "err": "GOOGLE_API_KEY not set"}))
        return None

    prompt = _date_preamble() + render_prompt(em)
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=MAX_TOKENS_NO_TOOLS,
            ),
        )
    except Exception as e:
        log.error(json.dumps({
            "phase": "call_gemini",
            "ticker": em.ticker,
            "err": f"{type(e).__name__}: {str(e)[:200]}",
        }))
        return None

    text = response.text or ""
    output, parse_err = parse_model_output(text)
    if output is None:
        log.error(json.dumps({
            "phase": "call_gemini",
            "ticker": em.ticker,
            "err": f"parse failed: {parse_err}",
            "raw_preview": text[:200],
        }))
        return None

    return output
