"""A9 narrative: the model explains a code-found proposal in plain words. It
may not invent numbers, change the proposal, or add a second change."""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SYSTEM_PROMPT = """\
You are the weekend reviewer of a news-driven US long/short equities paper
trading pipeline. Code has already found ONE proposed change and the evidence
for it. Your job is to explain it to the operator, who is not a programmer.
Rules:
- Use ONLY numbers that appear in the input, verbatim. Never derive new ones.
- rationale: 2-4 plain sentences: what the evidence shows and why this one
  change follows from it.
- risk: 1-2 sentences: what could go wrong if the change is made, and what
  the success metric will catch.
- Do not propose anything else. Do not soften or strengthen the proposal.
Respond with ONLY a JSON object matching the required schema."""


class ProposalNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rationale: str = Field(min_length=1, max_length=700)
    risk: str = Field(min_length=1, max_length=400)


def schema() -> dict:
    return ProposalNarrative.model_json_schema()


class NarrativeError(Exception):
    def __init__(self, detail: str, raw: str):
        self.detail, self.raw = detail[:500], raw[:4000]
        super().__init__(detail)


def validate(raw: str) -> ProposalNarrative:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise NarrativeError(f"not JSON: {e}", raw)
    try:
        return ProposalNarrative(**data)
    except ValidationError as e:
        raise NarrativeError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:4]), raw)


def build_messages(proposal: dict, retry_error: str | None = None) -> list[dict]:
    user = json.dumps({k: proposal[k] for k in ("title", "current_state", "proposed_diff",
                                                 "evidence", "expected_effect", "success_metric")},
                      ensure_ascii=False, default=str)
    if retry_error:
        user += "\n\nYour previous response was invalid: " + retry_error + "\nRespond again with ONLY a valid JSON object."
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
