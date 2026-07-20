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
    # v0.11.3: tickers/direction_hint/urgency/novelty_score used to carry
    # Python-side defaults, which meant pydantic's model_json_schema() left
    # them OUT of the grammar's "required" list sent to llama-server. That
    # was harmless while the model happened to fill every field anyway, but
    # confirmed live on the Spark (2026-07-20) that the current model +
    # llama.cpp build will skip these four fields ENTIRELY on harder items
    # (no feed-tagged symbol to lean on) rather than reasoning through them —
    # and because they had defaults, the omission was never treated as
    # invalid output; Python just silently filled in tickers=[] /
    # direction_hint="unclear" / etc. as if the model had decided that.
    # Removing the defaults forces the grammar to require the KEY's
    # presence for every field, the same technique already used for
    # `confidence` in v0.4.7. An empty ticker list / "unclear" / low
    # novelty are still perfectly legal values — this does not force the
    # model to guess a ticker — it only forces it to actually engage with
    # (and commit to) an answer for that field instead of dropping it, and
    # it makes the existing one-retry-then-REJECT path in triage.py
    # actually fire when a field is missing, instead of being bypassed.
    tickers: list[str] = Field(max_length=8)
    direction_hint: Literal["up", "down", "unclear"]
    urgency: Literal["high", "medium", "low"]
    novelty_score: float = Field(ge=0.0, le=1.0)
    # v0.4.7: A1's confidence in the material verdict itself. REQUIRED (no
    # default) so the model-side grammar forces emission and the journal's
    # confidence column is populated on every TRIAGE row (baseline rule 6).
    confidence: float = Field(ge=0.0, le=1.0)
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
