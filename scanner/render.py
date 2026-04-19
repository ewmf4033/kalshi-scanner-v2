"""
render.py — Prompt rendering for LLM scoring.

Loads category-specific prompts at import and interpolates per-market
context. Pure functions; fail-loud on missing prompts.

Contract:
    - render_prompt(em: EnrichedMarket) -> str
      Returns a complete prompt ready for any LLM (Claude, Grok, Gemini).
    - Category dispatch: MACRO/WEATHER/POLITICS/CRYPTO/COMMODITIES use
      their specialized prompts; TECH and OTHER fall back to general.
"""

from __future__ import annotations

from pathlib import Path
from core.schema import EnrichedMarket, Category

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.md").read_text()


_BASE = _load("base")
_CATEGORY_PROMPTS = {
    Category.MACRO:       _load("macro"),
    Category.WEATHER:     _load("weather"),
    Category.POLITICS:    _load("politics"),
    Category.CRYPTO:      _load("crypto"),
    Category.COMMODITIES: _load("commodities"),
    Category.TECH:        _load("general"),
    Category.OTHER:       _load("general"),
}


def _market_context_block(em: EnrichedMarket) -> str:
    snap = em.snapshot
    close_days = em.minutes_to_close / 1440.0
    return (
        "MARKET CONTEXT\n"
        f"  ticker:           {snap.ticker}\n"
        f"  title:            {snap.title}\n"
        f"  event_ticker:     {snap.event_ticker}\n"
        f"  close_time:       {snap.close_time_utc} "
        f"({close_days:.1f} days from now)\n"
        f"  current yes_bid:  {snap.yes_bid_cents}¢\n"
        f"  current yes_ask:  {snap.yes_ask_cents}¢\n"
        f"  current no_bid:   {snap.no_bid_cents}¢\n"
        f"  current no_ask:   {snap.no_ask_cents}¢\n"
        f"  spread:           {snap.spread_cents}¢\n"
        f"  volume_24h:       {snap.volume_24h:.0f}\n"
        f"  open_interest:    {snap.open_interest}\n"
        f"  market-implied fair p(YES) after de-vig: {em.fair_prob_yes:.3f}\n"
        f"  category (our tag): {em.category.value}\n"
    )


def render_prompt(em: EnrichedMarket) -> str:
    category_prompt = _CATEGORY_PROMPTS[em.category]
    return f"{_BASE}\n\n{category_prompt}\n\n{_market_context_block(em)}"
