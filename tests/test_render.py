"""Render category-specific prompts from EnrichedMarket."""

from scanner import render
from core.schema import EnrichedMarket, MarketSnapshot, MarketStatus, Category


def _em(category=Category.MACRO, ticker="KXCPI-26MAY-T0.4"):
    snap = MarketSnapshot(
        ticker=ticker, event_ticker="KXCPI-26MAY",
        title="Will CPI rise more than 0.4% in May?", status=MarketStatus.ACTIVE,
        yes_bid_cents=18, yes_ask_cents=21, no_bid_cents=79, no_ask_cents=82,
        last_price_cents=20, volume=500.0, volume_24h=246.0, open_interest=1581,
        open_time_utc="2026-02-20T15:00:00Z",
        close_time_utc="2026-06-10T12:25:00Z",
        captured_at_utc="2026-04-18T15:00:00Z",
        category_raw="Economics", raw_json={},
    )
    return EnrichedMarket(
        snapshot=snap, fair_prob_yes=0.2039,
        minutes_to_close=76165, hours_since_open=1417.0, category=category,
    )


class TestRenderPrompt:
    def test_macro(self):
        out = render.render_prompt(_em(Category.MACRO))
        assert "Macro economic releases" in out
        assert "quantitative trading analyst" in out
        assert "KXCPI-26MAY-T0.4" in out
        assert "18¢" in out
        assert "0.204" in out  # fair_prob_yes

    def test_weather(self):
        out = render.render_prompt(_em(Category.WEATHER))
        assert "climatology" in out.lower() or "weather" in out.lower()

    def test_politics(self):
        out = render.render_prompt(_em(Category.POLITICS))
        assert "politics" in out.lower() or "elections" in out.lower()

    def test_crypto(self):
        out = render.render_prompt(_em(Category.CRYPTO))
        assert "crypto" in out.lower() or "GBM" in out or "volatility" in out.lower()

    def test_commodities(self):
        out = render.render_prompt(_em(Category.COMMODITIES))
        assert "WTI" in out or "commodities" in out.lower()

    def test_tech_falls_back(self):
        out = render.render_prompt(_em(Category.TECH))
        assert "general" in out.lower() or "reference class" in out.lower()

    def test_other_falls_back(self):
        out = render.render_prompt(_em(Category.OTHER))
        assert "general" in out.lower() or "reference class" in out.lower()

    def test_market_context_always_present(self):
        out = render.render_prompt(_em())
        assert "MARKET CONTEXT" in out
        assert "fair p(YES)" in out
