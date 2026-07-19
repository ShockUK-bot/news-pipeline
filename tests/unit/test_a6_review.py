"""A6 unit tests — DB-free: verdict contracts + grammar-safety, action
mapping, R-progress, code-side staleness classification, alert renderer."""
import json
from datetime import datetime, timezone

import pytest

from a6_position_review.context import classify_staleness, r_progress
from a6_position_review.prompt import (build_eod_messages,
                                       build_review_messages)
from a6_position_review.render import render_review_alert, subject_line
from a6_position_review.schema import (EOD_ACTION, REVIEW_ACTION,
                                       ReviewValidationError,
                                       eod_json_schema, review_json_schema,
                                       validate_eod, validate_review)


def _eod(**over):
    base = {"verdicts": [{"position_id": 1, "verdict": "hold_overnight",
                          "confidence": 0.7,
                          "rationale": "half the move left, window open"}]}
    base.update(over)
    return json.dumps(base)


def _review(**over):
    base = {"verdict": "hold", "thesis_intact": True, "staleness": "fresh",
            "guard_review": "none", "confidence": 0.7,
            "rationale": "thesis confirmed by follow-on coverage"}
    base.update(over)
    return json.dumps(base)


# --- contracts -------------------------------------------------------------

def test_eod_valid_and_rejects():
    s = validate_eod(_eod())
    assert s.verdicts[0].verdict == "hold_overnight"
    with pytest.raises(ReviewValidationError):
        validate_eod(_eod(verdicts=[{"position_id": 1, "verdict": "short",
                                     "confidence": 0.5, "rationale": "x"}]))
    with pytest.raises(ReviewValidationError):
        validate_eod("nope")


def test_review_valid_and_rejects():
    v = validate_review(_review(verdict="exit", staleness="stale"))
    assert v.verdict == "exit" and v.staleness == "stale"
    with pytest.raises(ReviewValidationError):
        validate_review(_review(verdict="add_size"))
    with pytest.raises(ReviewValidationError):
        validate_review(_review(confidence=1.5))


def test_schemas_grammar_safe_and_action_maps():
    assert "anyOf" not in json.dumps(eod_json_schema())
    assert "anyOf" not in json.dumps(review_json_schema())
    assert REVIEW_ACTION == {"hold": "HOLD", "trim": "TRIM_RECO",
                             "exit": "EXIT_RECO"}
    assert EOD_ACTION["exit_before_close"] == "EXIT_EOD_RECO"


# --- code facts ------------------------------------------------------------

def test_r_progress():
    assert r_progress(100.0, 4.0, 106.0) == 1.5
    assert r_progress(100.0, 4.0, 98.0) == -0.5
    assert r_progress(100.0, 4.0, None) is None


def test_staleness_young_position_never_stale():
    # opened 3 days ago, zero news since entry -> fresh, not stale
    assert classify_staleness(None, "LONG", 4, opened_days_ago=3) == "fresh"


def test_staleness_old_quiet_long_is_stale():
    assert classify_staleness(None, "LONG", 4, opened_days_ago=40) == "stale"
    assert classify_staleness(35.0, "LONG", 4, opened_days_ago=60) == "stale"


def test_staleness_aging_midway():
    assert classify_staleness(15.0, "LONG", 4, opened_days_ago=60) == "aging"
    assert classify_staleness(2.0, "LONG", 4, opened_days_ago=60) == "fresh"


# --- prompts + renderer ----------------------------------------------------

def test_prompts_carry_retry_note():
    m = build_eod_messages([{"position_id": 1}], retry_error="bad")
    assert "previous response was invalid" in m[1]["content"]
    m2 = build_review_messages({"position_id": 1})
    assert "position" in m2[1]["content"]


def test_alert_render_and_subject():
    recos = [{"position_id": 1, "ticker": "ACME", "horizon": "LONG",
              "sessions_held": 30, "r_progress": 0.4, "action": "EXIT_RECO",
              "rationale": "stale thesis, no confirming evidence in 5 weeks"}]
    holds = [{"ticker": "NVDA", "horizon": "SHORT", "r_progress": 1.2,
              "rationale": "window open"}]
    body = render_review_alert("2026-07-20", recos, holds,
                               {"reviewed": 2, "stale_flagged": 1,
                                "slot": "heavy"})
    assert "EXIT_RECO" in body and "ACME" in body and "NVDA" in body
    assert "auto-apply is OFF" in body
    assert "ACME" in subject_line("2026-07-20", recos)
