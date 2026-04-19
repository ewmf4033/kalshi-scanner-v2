"""Parse LLM JSON into ModelOutput; structurally prevent v1 label bug."""

import pytest
from scanner import parse
from core.schema import ModelOutput, Confidence, Category


VALID = ('{"model_prob_yes": 0.3, "prob_range_lo": 0.2, "prob_range_hi": 0.4, '
         '"confidence": "medium", "catalyst": "CPI above consensus", '
         '"category": "macro", "reasoning": "stepwise"}')


class TestStripFences:
    def test_no_fence(self):
        assert parse.strip_fences("hello") == "hello"

    def test_json_fence(self):
        assert parse.strip_fences("```json\n{\"a\": 1}\n```") == '{"a": 1}'

    def test_plain_fence(self):
        assert parse.strip_fences("```\n{\"a\": 1}\n```") == '{"a": 1}'

    def test_fence_with_whitespace(self):
        assert parse.strip_fences("  ```json\n  {\"a\": 1}\n  ```  ") == '{"a": 1}'


class TestParseValid:
    def test_valid_json_parses(self):
        out, err = parse.parse_model_output(VALID)
        assert err is None
        assert out.model_prob_yes == 0.3
        assert out.confidence == Confidence.MEDIUM
        assert out.category == Category.MACRO
        assert out.catalyst == "CPI above consensus"
        assert out.reasoning == "stepwise"

    def test_fenced_valid_parses(self):
        out, err = parse.parse_model_output(f"```json\n{VALID}\n```")
        assert err is None

    def test_reasoning_field_optional(self):
        no_reasoning = VALID.replace(', "reasoning": "stepwise"', '')
        out, err = parse.parse_model_output(no_reasoning)
        assert err is None
        assert out.reasoning == ""


class TestParseErrors:
    def test_empty(self):
        out, err = parse.parse_model_output("")
        assert out is None and "empty" in err.lower()

    def test_invalid_json(self):
        out, err = parse.parse_model_output("{not json}")
        assert out is None
        assert "no JSON found" in err or "invalid JSON" in err

    def test_non_object(self):
        out, err = parse.parse_model_output("[1,2,3]")
        assert out is None and "expected JSON object" in err

    def test_missing_field(self):
        bad = '{"model_prob_yes": 0.3, "confidence": "medium", "catalyst": "x", "category": "macro"}'
        out, err = parse.parse_model_output(bad)
        assert out is None and "missing fields" in err

    def test_invalid_confidence(self):
        bad = VALID.replace('"medium"', '"super_high"')
        out, err = parse.parse_model_output(bad)
        assert out is None and "confidence" in err

    def test_invalid_category(self):
        bad = VALID.replace('"macro"', '"sports"')
        out, err = parse.parse_model_output(bad)
        assert out is None and "category" in err

    def test_prob_inverted(self):
        bad = ('{"model_prob_yes": 0.5, "prob_range_lo": 0.7, "prob_range_hi": 0.3, '
               '"confidence": "medium", "catalyst": "x", "category": "macro"}')
        out, err = parse.parse_model_output(bad)
        assert out is None and "prob_range_lo" in err

    def test_point_outside_range(self):
        bad = ('{"model_prob_yes": 0.9, "prob_range_lo": 0.2, "prob_range_hi": 0.4, '
               '"confidence": "medium", "catalyst": "x", "category": "macro"}')
        out, err = parse.parse_model_output(bad)
        assert out is None and "model_prob_yes" in err

    def test_range_too_narrow(self):
        """< 0.05 range violates minimum uncertainty."""
        bad = ('{"model_prob_yes": 0.3, "prob_range_lo": 0.29, "prob_range_hi": 0.31, '
               '"confidence": "high", "catalyst": "x", "category": "macro"}')
        out, err = parse.parse_model_output(bad)
        assert out is None and "range width" in err

    def test_prob_below_floor(self):
        """< 0.01 violates hard floor."""
        bad = ('{"model_prob_yes": 0.005, "prob_range_lo": 0.001, "prob_range_hi": 0.05, '
               '"confidence": "low", "catalyst": "x", "category": "other"}')
        out, err = parse.parse_model_output(bad)
        assert out is None

    def test_prob_above_ceiling(self):
        bad = ('{"model_prob_yes": 0.995, "prob_range_lo": 0.95, "prob_range_hi": 0.999, '
               '"confidence": "high", "catalyst": "x", "category": "other"}')
        out, err = parse.parse_model_output(bad)
        assert out is None

    def test_empty_catalyst(self):
        bad = VALID.replace('"CPI above consensus"', '""')
        out, err = parse.parse_model_output(bad)
        assert out is None and "catalyst" in err

    def test_catalyst_too_long(self):
        long = "x" * 200
        bad = VALID.replace('"CPI above consensus"', f'"{long}"')
        out, err = parse.parse_model_output(bad)
        assert out is None and "catalyst" in err


class TestStructuralGuarantees:
    def test_llm_cannot_set_model_name(self):
        """v1 label bug: LLMs copied 'model' field from prompts. Impossible now."""
        sneaky = (VALID[:-1] + ', "model": "gpt-4", "timestamp": "2026-04-18"}')
        out, err = parse.parse_model_output(sneaky)
        assert err is None and out is not None
        assert not hasattr(out, "model")
        assert not hasattr(out, "timestamp")


class TestEmbeddedJSON:
    """LLMs (especially Claude with tools) emit JSON inside prose."""

    def test_prose_then_fenced_json(self):
        """The format Claude returned in live test — reasoning, then fenced JSON."""
        raw = (
            "Let me analyze this market.\n\n"
            "Current price is X, strike is Y, so...\n\n"
            f"```json\n{VALID}\n```\n\n"
            "Hope this helps."
        )
        out, err = parse.parse_model_output(raw)
        assert err is None, f"should parse embedded fenced JSON, got: {err}"
        assert out.model_prob_yes == 0.3

    def test_multiple_fenced_blocks_takes_last(self):
        """If the LLM emits multiple fenced blocks, the LAST is the final answer."""
        first = '{"some": "draft"}'
        raw = (
            f"First draft:\n```json\n{first}\n```\n\n"
            f"Final answer:\n```json\n{VALID}\n```"
        )
        out, err = parse.parse_model_output(raw)
        assert err is None
        assert out.model_prob_yes == 0.3

    def test_raw_brace_object_in_prose(self):
        """No fences at all — just a {...} embedded in text."""
        raw = f"My answer is {VALID} -- final."
        out, err = parse.parse_model_output(raw)
        assert err is None
        assert out.model_prob_yes == 0.3

    def test_nested_braces_handled(self):
        """Brace counter must handle nested objects properly."""
        with_nested = VALID.replace(
            '"reasoning": "stepwise"',
            '"reasoning": "contains {braces} and more {nested {deep}} text"'
        )
        raw = f"Analysis: {with_nested}"
        out, err = parse.parse_model_output(raw)
        # Nested braces inside STRING values should parse fine
        assert err is None, f"got: {err}"
