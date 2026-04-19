"""
parse.py — Parse LLM JSON responses into validated ModelOutput.

Structural guarantee: LLMs CANNOT set metadata. ModelOutput has only
the allowed fields; any other keys in the JSON are silently ignored.
This prevents the v1 label bug where LLMs copied "model": "claude"
from prompt examples.

Contract:
    parse_model_output(raw: str) -> (ModelOutput | None, error_str | None)
    - Never raises. Always returns a tuple.
    - On any parse/validation failure, returns (None, error_msg).
"""

from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from core.schema import ModelOutput, Confidence, Category


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
# Match fenced JSON anywhere in the text (for LLMs that emit prose + fenced JSON)
_EMBEDDED_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def strip_fences(text: str) -> str:
    """Strip fences if the ENTIRE text is a fenced block. Idempotent otherwise."""
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text


def _extract_json(text: str) -> Optional[str]:
    """Find the JSON object in text. Tries in order:
       1. Whole text is valid JSON
       2. Whole text is a fenced block
       3. Fenced JSON embedded in prose (takes the LAST one — it's the final answer)
       4. Raw {...} object embedded in prose (brace-counting, LAST occurrence)
    """
    text = text.strip()
    if not text:
        return None

    # Fast path: already valid JSON
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # Try whole-text fence strip
    stripped = strip_fences(text)
    if stripped != text:
        try:
            json.loads(stripped)
            return stripped
        except json.JSONDecodeError:
            pass

    # Embedded fenced blocks — last match (Claude's final answer after reasoning)
    matches = list(_EMBEDDED_FENCE_RE.finditer(text))
    if matches:
        candidate = matches[-1].group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # Last-resort: brace-counted scan for { ... } — find the last balanced object
    best = None
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 0
            start = i
            for j in range(i, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:j+1]
                        try:
                            json.loads(candidate)
                            best = candidate  # keep LAST successful parse
                        except json.JSONDecodeError:
                            pass
                        i = j
                        break
            else:
                break
        i += 1
    return best


def parse_model_output(raw: str) -> Tuple[Optional[ModelOutput], Optional[str]]:
    if not raw or not raw.strip():
        return None, "empty response"

    json_text = _extract_json(raw)
    if json_text is None:
        return None, f"no JSON found in response (preview: {raw[:150]!r})"

    try:
        obj = json.loads(json_text)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"

    if not isinstance(obj, dict):
        return None, f"expected JSON object, got {type(obj).__name__}"

    required = ("model_prob_yes", "prob_range_lo", "prob_range_hi",
                "confidence", "catalyst", "category")
    missing = [f for f in required if f not in obj]
    if missing:
        return None, f"missing fields: {missing}"

    try:
        conf = Confidence(str(obj["confidence"]).lower().strip())
    except ValueError:
        return None, f"invalid confidence: {obj['confidence']!r}"

    try:
        cat = Category(str(obj["category"]).lower().strip())
    except ValueError:
        return None, f"invalid category: {obj['category']!r}"

    try:
        p_yes = float(obj["model_prob_yes"])
        p_lo = float(obj["prob_range_lo"])
        p_hi = float(obj["prob_range_hi"])
    except (TypeError, ValueError) as e:
        return None, f"non-numeric probability: {e}"

    try:
        return ModelOutput(
            model_prob_yes=p_yes,
            prob_range_lo=p_lo,
            prob_range_hi=p_hi,
            confidence=conf,
            catalyst=str(obj["catalyst"]),
            category=cat,
            reasoning=str(obj.get("reasoning", "")),   # optional; preserved if present
        ), None
    except ValueError as e:
        return None, str(e)
