"""Phase 2 unit tests: schema validation, the four routing rules as a matrix,
priority_score, NYSE calendar, prompt assembly. No database required."""
import json
from datetime import datetime, timezone

import pytest

from a1_triage.prompt import build_messages
from a1_triage.schema import (TriageOutput, TriageValidationError,
                              triage_json_schema, validate_triage)
from router.facts import RoutingFacts, market_open_now, priority_score
from router.rules import route

ROUTER_CFG = {
    "tier_weight": {1: 6, 2: 4, 3: 1},
    "urgency_weight": {"high": 6, "medium": 3, "low": 0},
    "corroboration_bonus_per_outlet": 1,
    "corroboration_bonus_cap": 3,
    "overnight_base": 50,
}


# ---- schema ---------------------------------------------------------------------

def test_valid_triage_parses():
    t = validate_triage(json.dumps({
        "material": True, "tickers": ["acme", "ACME", "TOOLONGSYM"],
        "direction_hint": "up", "urgency": "high",
        "novelty_score": 0.9, "confidence": 0.8, "reason": "M&A approach"}))
    assert t.tickers == ["ACME"]           # uppercased, deduped, implausible dropped
    assert t.confidence == 0.8


def test_not_json_raises():
    with pytest.raises(TriageValidationError) as e:
        validate_triage("I think this is material because...")
    assert "not valid JSON" in e.value.detail


def test_schema_violation_raises():
    with pytest.raises(TriageValidationError) as e:
        validate_triage(json.dumps({"material": "yes", "reason": "x"}))
    assert "material" in e.value.detail


def test_extra_field_rejected():
    with pytest.raises(TriageValidationError):
        validate_triage(json.dumps({
            "material": True, "reason": "x", "magnitude_est": 0.05}))  # A2's field, not A1's


def test_novelty_bounds():
    with pytest.raises(TriageValidationError):
        validate_triage(json.dumps({"material": False, "reason": "x", "novelty_score": 1.5}))


def test_json_schema_generates():
    s = triage_json_schema()
    assert s["properties"]["material"]["type"] == "boolean"
    assert "reason" in s["required"]
    # v0.4.7: confidence is REQUIRED so the model-side grammar forces emission
    assert "confidence" in s["required"]


# ---- routing rules matrix -----------------------------------------------------------

def _t(material=True, tickers=("ACME",), urgency="medium", novelty=0.8,
       confidence=0.9):
    return TriageOutput(material=material, tickers=list(tickers),
                        direction_hint="up", urgency=urgency,
                        novelty_score=novelty, confidence=confidence,
                        reason="test")


def _f(market_open=True, position_ids=(), score=10):
    return RoutingFacts(market_open=market_open, position_ids=list(position_ids),
                        thesis_matches=[], priority_score=score)


def test_rule2_discard_no_routes():
    d = route(_t(material=False), _f())
    assert d.action == "DISCARD" and d.routes == ()


def test_rule2_discard_with_held_position_still_guards():
    """Immaterial-but-held: guard fan-out survives the discard (correction on
    a held name must reach A12)."""
    d = route(_t(material=False), _f(position_ids=[41]))
    assert d.action == "DISCARD"
    assert [r.queue for r in d.routes] == ["signal.guard"]
    assert d.routes[0].priority == 0


def test_rule3_no_ticker_goes_thesis():
    d = route(_t(tickers=()), _f(market_open=True))
    assert [r.queue for r in d.routes] == ["signal.thesis"]   # never intraday


def test_rule4_market_open_analyst():
    d = route(_t(), _f(market_open=True))
    assert [r.queue for r in d.routes] == ["signal.analyst"]


def test_rule4_market_closed_overnight_priority():
    d = route(_t(), _f(market_open=False, score=12), overnight_base=50)
    assert [r.queue for r in d.routes] == ["signal.overnight"]
    assert d.routes[0].priority == 38             # 50 - 12; higher score = claimed earlier


def test_rule1_guard_in_addition_to_normal():
    d = route(_t(), _f(market_open=True, position_ids=[41, 42]))
    assert [r.queue for r in d.routes] == ["signal.guard", "signal.analyst"]


def test_priority_floor_zero():
    d = route(_t(), _f(market_open=False, score=999))
    assert d.routes[0].priority == 0


# ---- priority score ------------------------------------------------------------------

def test_priority_score_composition():
    # tier1(6) + high(6) + round(0.9*4)=4 + outlets 3 -> bonus min(2,3)=2 => 18
    assert priority_score(1, "high", 0.9, 3, ROUTER_CFG) == 18


def test_priority_score_floor():
    assert priority_score(3, "low", 0.0, 1, ROUTER_CFG) == 1


def test_corroboration_bonus_capped():
    assert priority_score(3, "low", 0.0, 99, ROUTER_CFG) == 1 + 3


# ---- market calendar ------------------------------------------------------------------

def test_nyse_holiday_closed():
    # 2026-07-03 (Friday) is the Independence Day observed holiday
    assert not market_open_now(datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc))


def test_nyse_regular_tuesday_open():
    # Tuesday 2026-07-07 15:00 UTC = 11:00 ET
    assert market_open_now(datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc))


def test_nyse_after_close():
    # Tuesday 2026-07-07 21:00 UTC = 17:00 ET
    assert not market_open_now(datetime(2026, 7, 7, 21, 0, tzinfo=timezone.utc))


# ---- prompt ---------------------------------------------------------------------------

def test_prompt_includes_item_and_fewshot():
    msgs = build_messages({"headline": "H", "summary": "S", "source": "edgar",
                           "source_tier": 1, "symbols": [], "channels": ["8-K"]},
                          {"is_new_story": True, "independent_outlets": 1})
    assert msgs[0]["role"] == "system"
    from a1_triage.prompt import FEW_SHOT
    assert len(msgs) == 1 + len(FEW_SHOT) * 2 + 1  # system + shots + user
    assert '"headline": "H"' in msgs[-1]["content"]


def test_prompt_retry_appends_error():
    msgs = build_messages({"headline": "H"}, {}, retry_error="schema violations: material: field required")
    assert "previous response was invalid" in msgs[-1]["content"]

