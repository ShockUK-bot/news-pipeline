"""A7 narrative — the ONLY model-generated part of the EOD report.

Contract (grammar-constrained, flat, llama.cpp-safe): a short summary plus
up to five notable observations. The prompt hard-requires that every number
quoted appears verbatim in the fact sheet and that missing/degraded data is
said out loud, never estimated (same doctrine as A13's answer call).
The report ships even when no model is reachable — the narrative is
decoration on code-computed numbers, never a dependency.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SYSTEM_PROMPT = """\
You narrate the end-of-day report for a news-driven, LONG-ONLY US equities
paper-trading pipeline. You receive a fact sheet of code-computed numbers.

Rules:
- Use ONLY numbers that appear in the fact sheet, verbatim. NEVER add,
  subtract, average, or otherwise derive numbers — the code already did all
  arithmetic. If a number is null/missing, say the data is missing.
- summary: 2-5 plain sentences: did the system trade, what was the realized
  P&L number from the sheet, what dominated the veto mix, anything degraded.
- notables: up to 5 short observations an operator should look at tomorrow
  (e.g. a dominant veto reason, a guard verdict on a held name, a health
  component not OK, an ingestion gap). Empty list is fine on a quiet day.
- tone: factual operator's log, no hype, no advice.

Respond with ONLY a JSON object matching the required schema."""


class NarrativeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1, max_length=600)
    notables: list[str] = Field(default_factory=list, max_length=5)
    data_quality: Literal["ok", "degraded", "missing"] = "ok"


def narrative_json_schema() -> dict:
    return NarrativeOutput.model_json_schema()


class NarrativeValidationError(Exception):
    def __init__(self, detail: str, raw: str):
        self.detail = detail[:500]
        self.raw = raw[:4000]
        super().__init__(detail)


def validate_narrative(raw_text: str) -> NarrativeOutput:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise NarrativeValidationError(f"output is not valid JSON: {e}", raw_text)
    try:
        return NarrativeOutput(**data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                         for x in e.errors()[:4])
        raise NarrativeValidationError(f"schema violations: {errs}", raw_text)


def build_messages(facts: dict, retry_error: str | None = None) -> list[dict]:
    user = json.dumps(facts, ensure_ascii=False, default=str)
    if retry_error:
        user += ("\n\nYour previous response was invalid: " + retry_error +
                 "\nRespond again with ONLY a valid JSON object.")
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]
