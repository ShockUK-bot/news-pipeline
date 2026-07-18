"""A12's output contract — the guard verdict (baseline §4 A12, v0.2).

One model, two enforcement points, same discipline as A1/A2:
  * model-side: guard_json_schema() is sent to llama-server as the grammar
    constraint, so off-contract output can't be generated;
  * code-side: validate_guard() re-checks anyway (spec §13 — models propose,
    code disposes).

Risk-reduction is enforced BY THE SCHEMA: the only expressible actions are
hold, tighten_stop, and exit. A risk-increasing action (widen stop, add,
extend window) is not schema-representable — it cannot be generated, parsed,
or executed (baseline rule 12; rule 16 tighten-only).

Grammar constraints (llama.cpp, probe-verified limits from the A13 deploy —
see a13-deploy-guide v1.1 §3): flat object, no nullable sub-objects, string
maxLength ≤ 600. Keep this schema boring.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# decisions.action / guard_ledger.recommended_action values, keyed by the
# model-side (lowercase) vocabulary.
ACTION_MAP = {"hold": "HOLD", "tighten_stop": "TIGHTEN_STOP", "exit": "EXIT"}


class GuardVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    thesis_intact: bool
    recommended_action: Literal["hold", "tighten_stop", "exit"]
    urgency: Literal["high", "medium", "low"]
    # Ordinal, same convention as A1/A2 (baseline rule 6); journaled on the
    # GUARD decision row.
    confidence: float = Field(ge=0.0, le=1.0)
    # Which news_checkable watch-list entries (verbatim) this item matches;
    # empty when none do. Journaled for A11's saves-vs-shakeouts attribution.
    watch_hits: list[str] = Field(default_factory=list, max_length=3)
    reason: str = Field(min_length=1, max_length=500)


def guard_json_schema() -> dict:
    """Schema for the server-side grammar constraint."""
    return GuardVerdict.model_json_schema()


class GuardValidationError(Exception):
    def __init__(self, detail: str, raw: str):
        self.detail = detail[:500]
        self.raw = raw[:4000]
        super().__init__(detail)


def validate_guard(raw_text: str) -> GuardVerdict:
    """Parse + validate model output. Raises GuardValidationError with a
    message suitable for appending to the retry prompt."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise GuardValidationError(f"output is not valid JSON: {e}", raw_text)
    try:
        return GuardVerdict(**data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                         for x in e.errors()[:4])
        raise GuardValidationError(f"schema violations: {errs}", raw_text)
