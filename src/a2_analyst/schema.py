"""A2's output contract (queue-contracts-spec §7 `thesis` object), strict-typed
like TriageOutput.

The Phase 3 hook: every machine_checkable invalidation is compiled against the
MIP DSL AT AUTHORING TIME — an entry must be either a stdlib predicate name or
a full spec dict that passes invalidation_dsl.validate(). An unmonitorable
invalidation is a validation error back to the model on retry; it cannot
enter the journal.
"""
from __future__ import annotations

import json
import re
from typing import Literal, Union

from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator)

from common.invalidation_dsl import MIPError, STDLIB, validate as mip_validate

# v0.12.1: minutes windows added for the scanner/scalp lane ("45_minutes");
# news-origin theses keep using sessions/weeks.
_WINDOW = re.compile(r"^\d{1,3}_(minutes?|sessions?|weeks?)$")

# v0.13.8 — cosmetic-variant coercion. On 2026-08-19 the same schema
# violation ("expected_move_window must look like '45_minutes' ...") fired
# six times in one 40-minute window, and each one cost a FULL extra model
# call (~30-50s) on the exact queue that was starving the scanner lane —
# roughly 10% of the analyst's morning throughput spent re-generating
# answers whose only defect was "45 minutes" for "45_minutes". A cosmetic
# deviation is now normalized before validation; anything semantically
# ambiguous still fails to the strict validator and retries as before.
_WINDOW_LOOSE = re.compile(r"^(\d{1,3})[ _\-]*([a-z]+)$")
_UNIT_SYNONYMS = {
    "m": "minutes", "min": "minutes", "mins": "minutes",
    "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours",
    "hour": "hours", "hours": "hours",           # converted to minutes below
    "d": "sessions", "day": "sessions", "days": "sessions",
    "sess": "sessions", "session": "sessions", "sessions": "sessions",
    "w": "weeks", "wk": "weeks", "wks": "weeks",
    "week": "weeks", "weeks": "weeks",
}


def coerce_window(v):
    """Normalize cosmetic variants of expected_move_window to the canonical
    '<n>_<unit>' form; return the input untouched when it is not confidently
    normalizable (the strict validator then reports it, model retries).
    Hours become minutes (the schema has no hours unit and the scalp lane
    thinks in minutes); days become sessions (the trading-calendar reading —
    journaled verbatim either way). Pure; no I/O."""
    if not isinstance(v, str):
        return v
    s = v.strip().lower()
    m = _WINDOW_LOOSE.match(s)
    if not m:
        return v
    n, unit = int(m.group(1)), _UNIT_SYNONYMS.get(m.group(2))
    if unit is None:
        return v
    if unit == "hours":
        n, unit = n * 60, "minutes"
    if n > 999:
        return v                       # out of the schema's 1-3 digit range
    return f"{n}_{unit}"


class RelatedOpportunity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticker: str = Field(min_length=1, max_length=7)
    relation: str = Field(min_length=1, max_length=40)     # supplier|customer|competitor|...
    rationale: str = Field(min_length=1, max_length=300)

    @field_validator("ticker")
    @classmethod
    def _sym(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.replace(".", "").isalpha():
            raise ValueError(f"implausible ticker {v!r}")
        return v


class Invalidation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine_checkable: list[Union[str, dict]] = Field(default_factory=list, max_length=4)
    news_checkable: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("machine_checkable")
    @classmethod
    def _mip_valid(cls, v: list) -> list:
        for entry in v:
            if isinstance(entry, str):
                if entry not in STDLIB:
                    raise ValueError(
                        f"unknown stdlib predicate {entry!r}; known: {sorted(STDLIB)}")
            elif isinstance(entry, dict):
                try:
                    mip_validate(entry)
                except MIPError as e:
                    raise ValueError(f"MIP spec invalid ({e.code}): {e}") from e
            else:
                raise ValueError("entries must be stdlib names or MIP spec objects")
        return v


class ThesisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ticker: str = Field(min_length=1, max_length=7)
    direction: Literal["up", "down"]
    # fraction, e.g. 0.055 = 5.5%. EXACTLY 0.0 is the analyst's explicit
    # no-trade verdict (v0.12.9): "nothing left here" is a valid, successful
    # answer — the service journals it as REJECT and nothing reaches the gate.
    # Before v0.12.9 the gt=0 bound made an honest 0 a schema violation, so
    # every exhausted-move judgment burned two model calls and was journaled
    # as a model FAILURE (2026-08-02 stale-scanner-backlog incident).
    magnitude_est: float = Field(ge=0.0, le=0.5)
    expected_move_window: str
    horizon: Literal["SHORT", "LONG"]
    confidence: float = Field(ge=0.0, le=1.0)              # ordinal (baseline rule 6)
    priced_in_assessment: str = Field(min_length=1, max_length=300)
    source_risk: Literal["low", "medium", "high"]
    invalidation: Invalidation
    related_opportunities: list[RelatedOpportunity] = Field(default_factory=list, max_length=3)
    reason: str = Field(min_length=1, max_length=600)

    @field_validator("ticker")
    @classmethod
    def _sym(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.replace(".", "").isalpha():
            raise ValueError(f"implausible ticker {v!r}")
        return v

    @field_validator("expected_move_window", mode="before")
    @classmethod
    def _window_coerce(cls, v):
        return coerce_window(v)          # v0.13.8: cosmetic variants normalize

    @field_validator("expected_move_window")
    @classmethod
    def _window(cls, v: str) -> str:
        if not _WINDOW.match(v):
            raise ValueError("expected_move_window must look like '45_minutes', "
                             "'2_sessions' or '3_weeks'")
        return v


def thesis_json_schema() -> dict:
    return ThesisOutput.model_json_schema()


class ThesisValidationError(Exception):
    def __init__(self, detail: str, raw: str):
        self.detail = detail[:600]
        self.raw = raw[:4000]
        super().__init__(detail)


def validate_thesis(raw_text: str) -> ThesisOutput:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ThesisValidationError(f"output is not valid JSON: {e}", raw_text)
    try:
        return ThesisOutput(**data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                         for x in e.errors()[:4])
        raise ThesisValidationError(f"schema violations: {errs}", raw_text)

