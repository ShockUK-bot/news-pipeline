"""A8 narrative contract — the model DECORATES the code-computed fact
sheet (baseline rule 5: numbers by code, narrative by model; the citation
rule holds because every fact the model may reference carries its item_id /
ticker in the fact sheet itself). Invalid output after retry -> the
briefing ships WITHOUT narrative; the email is never blocked by an LLM
(A7 pattern). Grammar-safe: flat object, no anyOf/null, short bounds.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SYSTEM_PROMPT = """\
You are the morning-briefing narrator for a news-driven, LONG-ONLY US
equities pipeline, writing minutes before the operator's trading day
starts. You receive the code-computed fact sheet: today's pre-market
action sheet, the standing thesis store, open positions (with R-progress
and earnings clocks), last night's position-review recommendations,
today's earnings landscape, and system health.

Write:
- summary: 2-4 sentences on what actually matters this morning — lead
  with anything requiring a decision (exit/trim recommendations, blackout
  windows, system degradation), then the day's character (candidate count,
  dominant themes).
- watch_items: up to 5 one-line bullets, most important first. Each must
  reference something IN the fact sheet (a ticker, thesis id, item, or
  health component) — never outside knowledge or memory.

Rules: no numbers you were not given; no predictions of prices; no
invented tickers or events. Respond with ONLY a JSON object matching the
required schema."""


class BriefingNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1, max_length=500)
    watch_items: list[str] = Field(max_length=5)


def narrative_json_schema() -> dict:
    return BriefingNarrative.model_json_schema()


class NarrativeValidationError(Exception):
    def __init__(self, detail: str, raw: str):
        self.detail = detail[:500]
        self.raw = raw[:4000]
        super().__init__(detail)


def validate_narrative(raw_text: str) -> BriefingNarrative:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise NarrativeValidationError(f"output is not valid JSON: {e}",
                                       raw_text)
    try:
        return BriefingNarrative(**data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                         for x in e.errors()[:4])
        raise NarrativeValidationError(f"schema violations: {errs}", raw_text)


def build_messages(facts: dict, retry_error: str | None = None) -> list[dict]:
    user = json.dumps({"facts": facts}, ensure_ascii=False, default=str)
    if retry_error:
        user += ("\n\nYour previous response was invalid: " + retry_error +
                 "\nRespond again with ONLY a valid JSON object.")
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]
