"""A5's output contract — the thematic update (baseline §6.3 A5).

One grammar-constrained call per nightly run. The model receives the fresh
`signal.thesis` items (code-selected, capped) plus a compact view of every
ACTIVE thesis in the store, and returns three lists:

  items        one op per input item: attach it as evidence to an EXISTING
               thesis ("evidence", with thesis_id + polarity) or drop it
               ("ignore"). Items that seed a brand-new thesis go in
               new_theses instead and may be omitted here.
  new_theses   fully-specified new standing theses, each anchored to one of
               tonight's item_ids (anchor_item_id). Code mints the thesis_id
               — the model never invents identifiers.
  reviews      per-thesis judgment updates: confidence moves and status
               proposals (keep / invalidate / realized). Expiry-by-staleness
               is a CODE rule, not a model op.

Code validates every referenced thesis_id against the store and downgrades
unknown references to "ignore" — the model cannot write to a thesis that
does not exist.

Grammar-safe per the probe-verified llama.cpp limits: flat objects, no
anyOf/null (no Optional fields — sentinels instead), short string bounds.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator)


class ItemOp(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item_id: str = Field(min_length=1, max_length=120)
    op: Literal["evidence", "ignore"]
    thesis_id: str = Field(default="", max_length=40)   # "" when op="ignore"
    polarity: Literal["supports", "contradicts", "neutral"] = "neutral"
    note: str = Field(default="", max_length=200)


class Beneficiary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ticker: str = Field(min_length=1, max_length=7)
    relation: str = Field(min_length=1, max_length=60)
    rationale: str = Field(min_length=1, max_length=150)

    @field_validator("ticker")
    @classmethod
    def _sym(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.replace(".", "").isalpha():
            raise ValueError(f"implausible ticker {v!r}")
        return v


class NewThesis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    anchor_item_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=120)
    driver: str = Field(min_length=1, max_length=400)
    direction: Literal["up", "down", "unclear"]
    horizon: Literal["LONG", "SHORT"] = "LONG"
    confidence: float = Field(ge=0.0, le=1.0)
    beneficiaries: list[Beneficiary] = Field(max_length=5)
    invalidation: list[str] = Field(max_length=4)


class ThesisReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    thesis_id: str = Field(min_length=1, max_length=40)
    op: Literal["keep", "invalidate", "realized"]
    confidence: float = Field(ge=0.0, le=1.0)   # new confidence when op="keep"
    note: str = Field(default="", max_length=200)


class ThematicUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[ItemOp] = Field(max_length=80)
    new_theses: list[NewThesis] = Field(max_length=6)
    reviews: list[ThesisReview] = Field(max_length=25)
    summary: str = Field(min_length=1, max_length=500)


def thematic_json_schema() -> dict:
    return ThematicUpdate.model_json_schema()


class ThematicValidationError(Exception):
    def __init__(self, detail: str, raw: str):
        self.detail = detail[:500]
        self.raw = raw[:4000]
        super().__init__(detail)


def validate_thematic(raw_text: str) -> ThematicUpdate:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ThematicValidationError(f"output is not valid JSON: {e}", raw_text)
    try:
        return ThematicUpdate(**data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                         for x in e.errors()[:4])
        raise ThematicValidationError(f"schema violations: {errs}", raw_text)
