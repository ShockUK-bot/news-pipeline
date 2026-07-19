"""A6's output contracts (baseline §6.4) — recommendation-only in Phase 8
(auto-apply off by default, exactly like A12 v1): every verdict is journaled
and mirrored into position_events; no order is placed and no stop is moved.

Two contracts:

  EodSheet       the 15:45 ET overnight-hold check (baseline L6): ONE call
                 covering every open SHORT-lane position — remaining
                 expected move vs. gap exposure. Runs on the RESIDENT
                 analyst slot (the heavy model never runs during market
                 hours — memory rule).
  ReviewVerdict  the nightly deep review: one call PER position on the
                 heavy slot — thesis intact? evidence stale? were today's
                 A12 guard actions sensible?

Grammar-safe per the probe-verified llama.cpp limits: flat objects, no
anyOf/null, short string bounds.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class HoldVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    position_id: int = Field(ge=1)
    verdict: Literal["hold_overnight", "exit_before_close"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=200)


class EodSheet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdicts: list[HoldVerdict] = Field(max_length=25)


class ReviewVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["hold", "trim", "exit"]
    thesis_intact: bool
    staleness: Literal["fresh", "aging", "stale"]
    guard_review: Literal["none", "appropriate", "too_cautious",
                          "too_permissive"] = "none"
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=300)


def eod_json_schema() -> dict:
    return EodSheet.model_json_schema()


def review_json_schema() -> dict:
    return ReviewVerdict.model_json_schema()


class ReviewValidationError(Exception):
    def __init__(self, detail: str, raw: str):
        self.detail = detail[:500]
        self.raw = raw[:4000]
        super().__init__(detail)


def validate_eod(raw_text: str) -> EodSheet:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ReviewValidationError(f"output is not valid JSON: {e}", raw_text)
    try:
        return EodSheet(**data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                         for x in e.errors()[:4])
        raise ReviewValidationError(f"schema violations: {errs}", raw_text)


def validate_review(raw_text: str) -> ReviewVerdict:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ReviewValidationError(f"output is not valid JSON: {e}", raw_text)
    try:
        return ReviewVerdict(**data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                         for x in e.errors()[:4])
        raise ReviewValidationError(f"schema violations: {errs}", raw_text)


# Verdict -> journal action (promoted so A7/A9/C6 can filter without JSON).
REVIEW_ACTION = {"hold": "HOLD", "trim": "TRIM_RECO", "exit": "EXIT_RECO"}
EOD_ACTION = {"hold_overnight": "HOLD_OVERNIGHT",
              "exit_before_close": "EXIT_EOD_RECO"}
