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


def strip_fences(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text


def parse_model_output(raw: str) -> Tuple[Optional[ModelOutput], Optional[str]]:
    if not raw or not raw.strip():
        return None, "empty response"

    text = strip_fences(raw)
    try:
        obj = json.loads(text)
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
