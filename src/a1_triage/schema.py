"""A1's output contract (queue-contracts-spec §6 `triage` object).

One model, two enforcement points:
  * model-side: model_json_schema() is sent to llama-server as the grammar
    constraint, so off-contract output can't be generated;
  * code-side: validate_triage() re-checks anyway (spec §13 — models propose,
    code disposes; the stub backend and any future backend get no free pass).
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class TriageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    material: bool
    tickers: list[str] = Field(default_factory=list, max_length=8)
    direction_hint: Literal["up", "down", "unclear"] = "unclear"
    urgency: Literal["high", "medium", "low"] = "low"
    novelty_score: float = Field(ge=0.0, le=1.0, default=0.0)
    reason: str = Field(min_length=1, max_length=400)

    @field_validator("tickers")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        out = []
        for t in v:
            t = t.strip().upper()
            # plausible US equity symbol: 1-5 letters, optional .X class suffix
            if t and len(t) <= 7 and t.replace(".", "").isalpha():
                out.append(t)
        return list(dict.fromkeys(out))          # dedupe, keep order


def triage_json_schema() -> dict:
    """Schema for the server-side grammar constraint."""
    return TriageOutput.model_json_schema()


class TriageValidationError(Exception):
    def __init__(self, detail: str, raw: str):
        self.detail = detail[:500]
        self.raw = raw[:4000]
        super().__init__(detail)


def validate_triage(raw_text: str) -> TriageOutput:
    """Parse + validate model output. Raises TriageValidationError with a
    message suitable for appending to the retry prompt."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise TriageValidationError(f"output is not valid JSON: {e}", raw_text)
    try:
        return TriageOutput(**data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                         for x in e.errors()[:4])
        raise TriageValidationError(f"schema violations: {errs}", raw_text)
