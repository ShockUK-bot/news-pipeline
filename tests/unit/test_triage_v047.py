"""v0.4.7 unit tests: tightened materiality prompt (catalyst taxonomy +
negative categories + few-shot negatives from the 2026-07-15 evidence),
required confidence field, the min_confidence router lever, and the
suppression corroboration-bypass predicate. No database required."""
import json

import pytest

from a1_triage.prompt import FEW_SHOT, SYSTEM_PROMPT
from a1_triage.schema import TriageOutput, TriageValidationError, validate_triage
from a1_triage.suppression import corroboration_bypass
from router.facts import RoutingFacts
from router.rules import route


def _t(material=True, tickers=("ACME",), confidence=0.9):
    return TriageOutput(material=material, tickers=list(tickers),
                        direction_hint="up", urgency="medium",
                        novelty_score=0.8, confidence=confidence, reason="test")


def _f(market_open=True, position_ids=()):
    return RoutingFacts(market_open=market_open, position_ids=list(position_ids),
                        thesis_matches=[], priority_score=10)


# ---- prompt: negative categories are explicit -------------------------------

def test_prompt_names_negative_categories():
    for phrase in ("rating change", "Price-action commentary", "52-week",
                   "Distant-future scheduled", "Sub-materiality",
                   "Political/macro commentary"):
        assert phrase in SYSTEM_PROMPT, f"negative category missing: {phrase}"


def test_prompt_bans_sentiment_boilerplate():
    assert "sentiment" in SYSTEM_PROMPT       # named, and named as banned
    assert "not a reason" in SYSTEM_PROMPT


def test_prompt_keeps_catalyst_recall():
    # Baseline §4 recall bias must survive the tightening — scoped to catalysts.
    assert "recall applies to" in SYSTEM_PROMPT
    assert "escalate even if details are thin" in SYSTEM_PROMPT


def test_fewshot_negatives_cover_evidence_categories():
    """The four 2026-07-15 failure modes each have a few-shot negative."""
    negatives = [json.dumps(out) + json.dumps(inp)
                 for inp, out in FEW_SHOT if out["material"] is False]
    text = " ".join(negatives)
    assert "Maintains" in text                 # PT maintenance (Mizuho/UBS)
    assert "52-Week High" in text              # price-action commentary (AAPL)
    assert "Retire in 2027" in text            # distant scheduled event (CLDX)
    assert "$6.25M" in text                    # sub-materiality micro deal


def test_fewshot_outputs_validate_against_schema():
    for _, out in FEW_SHOT:
        validate_triage(json.dumps(out))       # raises on any contract drift


def test_fewshot_reasons_name_categories_not_sentiment():
    for _, out in FEW_SHOT:
        assert "sentiment" not in out["reason"].lower()


# ---- schema: confidence ------------------------------------------------------

def test_confidence_required():
    with pytest.raises(TriageValidationError) as e:
        validate_triage(json.dumps({"material": True, "reason": "x"}))
    assert "confidence" in e.value.detail


def test_confidence_bounds():
    with pytest.raises(TriageValidationError):
        validate_triage(json.dumps(
            {"material": True, "confidence": 1.2, "reason": "x"}))


# ---- router: min_confidence lever --------------------------------------------

def test_lever_off_by_default_low_confidence_escalates():
    d = route(_t(confidence=0.05), _f())
    assert d.action == "ESCALATE"


def test_lever_discards_below_floor():
    d = route(_t(confidence=0.4), _f(), min_confidence=0.6)
    assert d.action == "DISCARD" and d.routes == ()


def test_lever_passes_at_floor():
    d = route(_t(confidence=0.6), _f(), min_confidence=0.6)
    assert d.action == "ESCALATE"


def test_lever_discard_keeps_guard_fanout():
    d = route(_t(confidence=0.4), _f(position_ids=[7]), min_confidence=0.6)
    assert d.action == "DISCARD"
    assert [r.queue for r in d.routes] == ["signal.guard"]


# ---- suppression: corroboration bypass ---------------------------------------

def test_corroboration_bypass_on_crossing():
    assert corroboration_bypass(outlets_now=3, outlets_prior=1, threshold=3)


def test_no_bypass_when_already_crossed():
    assert not corroboration_bypass(outlets_now=5, outlets_prior=4, threshold=3)


def test_no_bypass_below_threshold():
    assert not corroboration_bypass(outlets_now=2, outlets_prior=1, threshold=3)
