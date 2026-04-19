"""
Unit tests for scanner/models.py.

These mock the LLM SDKs. Live end-to-end tests happen via a separate
script so they don't burn API budget on every pytest run.
"""

from unittest.mock import patch, MagicMock

import pytest
from scanner import models
from core.schema import (
    EnrichedMarket, MarketSnapshot, MarketStatus, Category, ModelOutput, Confidence,
)


def _em():
    snap = MarketSnapshot(
        ticker="KXCPI-26MAY-T0.4", event_ticker="KXCPI-26MAY",
        title="CPI above 0.4% MoM in May", status=MarketStatus.ACTIVE,
        yes_bid_cents=18, yes_ask_cents=21, no_bid_cents=79, no_ask_cents=82,
        last_price_cents=20, volume=500.0, volume_24h=246.0, open_interest=1581,
        open_time_utc="2026-02-20T15:00:00Z",
        close_time_utc="2026-06-10T12:25:00Z",
        captured_at_utc="2026-04-18T15:00:00Z",
        category_raw="Economics", raw_json={},
    )
    return EnrichedMarket(
        snapshot=snap, fair_prob_yes=0.2039,
        minutes_to_close=76165, hours_since_open=1417.0, category=Category.MACRO,
    )


VALID_JSON = (
    '{"model_prob_yes": 0.25, "prob_range_lo": 0.18, "prob_range_hi": 0.35, '
    '"confidence": "medium", "catalyst": "consensus 0.3 vs 0.4 strike, 0.67 stdev gap", '
    '"category": "macro", "reasoning": "normal CDF"}'
)


class TestCallClaude:
    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("anthropic.Anthropic")
    def test_successful_response(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Claude's response has a list of blocks with type="text"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = VALID_JSON
        mock_client.messages.create.return_value = MagicMock(content=[text_block])

        out = models.call_claude(_em())
        assert out is not None
        assert out.model_prob_yes == 0.25
        assert out.confidence == Confidence.MEDIUM
        assert out.category == Category.MACRO

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("anthropic.Anthropic")
    def test_api_exception_returns_none(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = RuntimeError("connection timeout")
        assert models.call_claude(_em()) is None

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("anthropic.Anthropic")
    def test_bad_json_returns_none(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "not json at all"
        mock_client.messages.create.return_value = MagicMock(content=[text_block])
        assert models.call_claude(_em()) is None

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_api_key_returns_none(self):
        assert models.call_claude(_em()) is None


class TestCallGrok:
    @patch.dict("os.environ", {"XAI_API_KEY": "test-key"})
    @patch("openai.OpenAI")
    def test_successful_response(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        choice = MagicMock()
        choice.message.content = VALID_JSON
        mock_client.chat.completions.create.return_value = MagicMock(choices=[choice])

        out = models.call_grok(_em())
        assert out is not None
        assert out.model_prob_yes == 0.25

        # Confirm we used Grok's base_url and model
        call_args = mock_openai_cls.call_args
        assert "x.ai" in call_args.kwargs.get("base_url", "")
        assert mock_client.chat.completions.create.call_args.kwargs["model"] == "grok-4-fast-reasoning"

    @patch.dict("os.environ", {"XAI_API_KEY": "test-key"})
    @patch("openai.OpenAI")
    def test_api_exception_returns_none(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = RuntimeError("rate limit")
        assert models.call_grok(_em()) is None

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_api_key_returns_none(self):
        assert models.call_grok(_em()) is None


class TestCallGemini:
    @patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"})
    @patch("google.genai.Client")
    def test_successful_response(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.models.generate_content.return_value = MagicMock(text=VALID_JSON)

        out = models.call_gemini(_em())
        assert out is not None
        assert out.model_prob_yes == 0.25

    @patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"})
    @patch("google.genai.Client")
    def test_api_exception_returns_none(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.models.generate_content.side_effect = RuntimeError("quota exceeded")
        assert models.call_gemini(_em()) is None

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_api_key_returns_none(self):
        assert models.call_gemini(_em()) is None


class TestStructuralIntegrity:
    """Verify that every client enforces the same output contract."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("anthropic.Anthropic")
    def test_llm_metadata_fields_ignored(self, mock_anthropic_cls):
        """If the LLM tries to include a 'model' field in its response, it's silently dropped.
        This is the v1 label bug made impossible."""
        sneaky = (
            '{"model_prob_yes": 0.25, "prob_range_lo": 0.18, "prob_range_hi": 0.35, '
            '"confidence": "medium", "catalyst": "x", "category": "macro", '
            '"model": "gpt-4", "timestamp": "hack"}'
        )
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = sneaky
        mock_client.messages.create.return_value = MagicMock(content=[text_block])

        out = models.call_claude(_em())
        assert out is not None
        assert not hasattr(out, "model")
        assert not hasattr(out, "timestamp")
