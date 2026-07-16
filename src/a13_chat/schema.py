"""A13's two output contracts, strict-typed like TriageOutput/ThesisOutput.

Call 1 (planner): the model PROPOSES which whitelisted retrieval packs to run.
Code disposes — only names in QUERY_NAMES execute, only through parameterized
SQL in retrieval.py. The model never writes SQL.

Call 2 (answer): prose answer computed FROM the fact sheet, plus an optional
advisory recommendation and an optional filing proposal. A filing proposal is
inert text until the operator confirms it on the dashboard (token-gated) and
filing.py's code-side gates pass.
"""
from __future__ import annotations

import json
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

QUERY_NAMES = (
    "open_positions",     # open positions (optional ticker filter)
    "closed_trades",      # closed positions with realized P&L + exit layer
    "position_detail",    # one position: events, exits, thesis reason
    "vetoes",             # VETO decisions with veto_reason (optional ticker)
    "decision_trace",     # decision timeline by signal_id or ticker
    "ticker_news",        # recent news items tagged with the ticker
    "ticker_snapshot",    # live quote, ATR/ADV, regime, open-position check
    "performance",        # daily aggregates over closed trades
    "control_state",      # journal.control flags + component health
)


def _sym(v: str) -> str:
    v = v.strip().upper()
    if not v or len(v) > 7 or not v.replace(".", "").isalpha():
        raise ValueError(f"implausible ticker {v!r}")
    return v


class PlannedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: Literal[QUERY_NAMES]  # type: ignore[valid-type]
    ticker: Optional[str] = None
    days: Optional[int] = Field(default=None, ge=1, le=365)
    position_id: Optional[int] = Field(default=None, ge=1)
    signal_id: Optional[str] = Field(default=None, max_length=80)
    limit: Optional[int] = Field(default=None, ge=1, le=100)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: Optional[str]) -> Optional[str]:
        return _sym(v) if v is not None else None


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    queries: list[PlannedQuery] = Field(min_length=1, max_length=5)
    reason: str = Field(min_length=1, max_length=300)


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    stance: Literal["consider_long", "watch", "avoid", "no_view"]
    rationale: str = Field(min_length=1, max_length=600)


class FilingProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ticker: str = Field(min_length=1, max_length=7)
    anchor_item_id: str = Field(min_length=1, max_length=120)
    rationale: str = Field(min_length=1, max_length=300)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str) -> str:
        return _sym(v)


class AnswerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer: str = Field(min_length=1, max_length=4000)
    recommendation: Optional[Recommendation] = None
    filing_proposal: Optional[FilingProposal] = None
    caveats: list[str] = Field(default_factory=list, max_length=4)


def planner_json_schema() -> dict:
    return PlannerOutput.model_json_schema()


def answer_json_schema() -> dict:
    return AnswerOutput.model_json_schema()


class ChatValidationError(Exception):
    def __init__(self, detail: str, raw: str):
        self.detail = detail[:600]
        self.raw = raw[:4000]
        super().__init__(detail)


def _validate(model_cls, raw_text: str):
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ChatValidationError(f"output is not valid JSON: {e}", raw_text)
    try:
        return model_cls(**data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                         for x in e.errors()[:4])
        raise ChatValidationError(f"schema violations: {errs}", raw_text)


def validate_plan(raw_text: str) -> PlannerOutput:
    return _validate(PlannerOutput, raw_text)


def validate_answer(raw_text: str) -> AnswerOutput:
    return _validate(AnswerOutput, raw_text)
