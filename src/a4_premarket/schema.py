"""A4's output contract — the ranked action sheet (baseline §6 A4).

The model receives the TOP-K fresh overnight items (code-selected by queue
priority) and assigns each a lane + rank + one-line rationale. Lanes:

  open_candidate   worth evaluating against the open (A2 at open+blackout,
                   C3's open-handoff rule decides priced-in vs opportunity)
  thesis           long-horizon relevance, route to the A5 lane
  ignore           overnight noise

The model does NOT assign the position-touching lane — code routes anything
touching an open position to A12 before the model ever sees the list
(protecting capital is not a ranking decision).

Grammar-safe per the probe-verified llama.cpp limits: flat item objects, no
anyOf/null, short string bounds.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class SheetItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item_id: str = Field(min_length=1, max_length=120)
    lane: Literal["open_candidate", "thesis", "ignore"]
    rank: int = Field(ge=1, le=50)        # 1 = highest conviction, per lane
    rationale: str = Field(min_length=1, max_length=200)


class ActionSheet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[SheetItem] = Field(max_length=25)
    summary: str = Field(min_length=1, max_length=500)


def sheet_json_schema() -> dict:
    return ActionSheet.model_json_schema()


class SheetValidationError(Exception):
    def __init__(self, detail: str, raw: str):
        self.detail = detail[:500]
        self.raw = raw[:4000]
        super().__init__(detail)


def validate_sheet(raw_text: str) -> ActionSheet:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise SheetValidationError(f"output is not valid JSON: {e}", raw_text)
    try:
        return ActionSheet(**data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                         for x in e.errors()[:4])
        raise SheetValidationError(f"schema violations: {errs}", raw_text)
