"""Phase 3 unit tests: ThesisOutput incl. DSL-validated invalidations, the C3
rules matrix, credibility matrix, market-data indicators, C8 features. No DB."""
import asyncio
import json

import pytest

from a2_analyst.schema import (ThesisValidationError, thesis_json_schema,
                               validate_thesis)
from c3_gate.rules import GateVerdict, MarketState, credibility_required, evaluate
from common.marketdata import FakeData, adv20, atr14, realized_vol, sma

GATE_CFG = {
    "intraday_move_pct": 0.015, "intraday_vol_mult": 2.5,
    "intraday_window_min": 30, "extended_pct": 0.06,
    "open_blackout_min": 15, "handoff_gap_ratio": 0.5,
    "impact_medium_min": 0.02, "impact_high_min": 0.05,
    "required_outlets": {"low": {2: 1, 3: 1}, "medium": {2: 1, 3: 2},
                         "high": {2: 2, 3: 3}},
}


def thesis_json(**over):
    base = {"ticker": "ACME", "direction": "up", "magnitude_est": 0.055,
            "expected_move_window": "2_sessions", "horizon": "SHORT",
            "confidence": 0.72, "priced_in_assessment": "moved 1.1% of 5.5% est",
            "source_risk": "low",
            "invalidation": {"machine_checkable": ["close_below_prenews"],
                             "news_checkable": ["counterparty denial"]},
            "related_opportunities": [], "reason": "supply repricing"}
    base.update(over)
    return json.dumps(base)


# ---- thesis schema + DSL hook -------------------------------------------------

def test_valid_thesis_parses():
    t = validate_thesis(thesis_json())
    assert t.ticker == "ACME" and t.invalidation.machine_checkable == ["close_below_prenews"]


def test_unknown_stdlib_predicate_rejected():
    with pytest.raises(ThesisValidationError) as e:
        validate_thesis(thesis_json(
            invalidation={"machine_checkable": ["price_goes_down_a_lot"],
                          "news_checkable": []}))
    assert "unknown stdlib predicate" in e.value.detail


def test_full_mip_spec_accepted():
    spec = {"id": "custom_1",
            "when": {"metric": "close", "tf": "session", "op": "<",
                     "value": {"ref": "prenews_price"}},
            "persist": {"bars": 1}, "action": {"type": "exit"}}
    t = validate_thesis(thesis_json(
        invalidation={"machine_checkable": [spec], "news_checkable": []}))
    assert t.invalidation.machine_checkable[0]["id"] == "custom_1"


def test_invalid_mip_spec_rejected():
    bad = {"id": "x", "when": {"metric": "vibes", "tf": "1m", "op": "<", "value": 1},
           "persist": {"bars": 1}, "action": {"type": "exit"}}
    with pytest.raises(ThesisValidationError) as e:
        validate_thesis(thesis_json(
            invalidation={"machine_checkable": [bad], "news_checkable": []}))
    assert "MIP spec invalid" in e.value.detail


def test_move_window_pattern():
    with pytest.raises(ThesisValidationError):
        validate_thesis(thesis_json(expected_move_window="soon"))
    assert validate_thesis(thesis_json(expected_move_window="3_weeks"))


def test_magnitude_bounds():
    with pytest.raises(ThesisValidationError):
        validate_thesis(thesis_json(magnitude_est=0.9))    # > 50% is a hallucination


def test_thesis_schema_generates():
    s = thesis_json_schema()
    assert "invalidation" in s["required"]


# ---- credibility matrix ----------------------------------------------------------

def test_tier1_passes_alone():
    assert credibility_required("high", 1, "high", GATE_CFG) == 1


def test_tier3_high_impact_never_alone():
    assert credibility_required("high", 3, "low", GATE_CFG) == 3


def test_source_risk_bumps_level():
    # medium impact tier-3 normally 2; high source_risk -> treated as high -> 3
    assert credibility_required("medium", 3, "low", GATE_CFG) == 2
    assert credibility_required("medium", 3, "high", GATE_CFG) == 3


# ---- gate rules matrix -------------------------------------------------------------

def state(**over):
    base = dict(prenews_price=100.0, last_price=102.0, vol_mult=3.0,
                minutes_since_publish=10, news_in_session=True,
                minutes_since_open=120, gap_pct=None,
                corroboration_outlets=2, tier_min=2)
    base.update(over)
    return MarketState(**base)


def thesis_d(**over):
    base = {"ticker": "ACME", "direction": "up", "magnitude_est": 0.055,
            "source_risk": "low"}
    base.update(over)
    return base


def test_intraday_pass():
    v = evaluate(thesis_d(), state(), GATE_CFG)
    assert (v.verdict, v.rule, v.veto_reason) == ("PASS", "intraday", None)
    assert v.numbers["pct_move"] == 0.02


def test_long_only_veto():
    v = evaluate(thesis_d(direction="down"), state(), GATE_CFG)
    assert v.veto_reason == "LONG_ONLY"


def test_credibility_veto_tier3_single_source():
    v = evaluate(thesis_d(), state(corroboration_outlets=1, tier_min=3), GATE_CFG)
    assert v.veto_reason == "CREDIBILITY"
    assert v.numbers["credibility"]["required_outlets"] == 3


def test_window_veto():
    v = evaluate(thesis_d(), state(minutes_since_publish=45), GATE_CFG)
    assert v.veto_reason == "GATE_WINDOW"


def test_extended_veto():
    v = evaluate(thesis_d(), state(last_price=107.0), GATE_CFG)
    assert v.veto_reason == "GATE_EXTENDED"


def test_no_confirm_low_volume():
    v = evaluate(thesis_d(), state(vol_mult=1.2), GATE_CFG)
    assert v.veto_reason == "GATE_NO_CONFIRM"


def test_marketdata_missing_distinct_veto():
    # v0.5.9: absent volume data is journaled as MARKETDATA_MISSING, never
    # as GATE_NO_CONFIRM — a starved feed must be visible as such.
    v = evaluate(thesis_d(), state(vol_mult=None), GATE_CFG)
    assert (v.verdict, v.veto_reason) == ("VETO", "MARKETDATA_MISSING")
    assert v.numbers["vol_mult"] is None


def test_marketdata_missing_not_in_handoff_path():
    # open-handoff rule never used vol_mult; missing volume must not veto it
    v = evaluate(thesis_d(), state(news_in_session=False, minutes_since_open=30,
                                   gap_pct=0.01, last_price=101.0,
                                   vol_mult=None), GATE_CFG)
    assert v.verdict == "PASS" and v.rule == "open_handoff"


def test_no_confirm_small_move():
    v = evaluate(thesis_d(), state(last_price=100.5), GATE_CFG)
    assert v.veto_reason == "GATE_NO_CONFIRM"


def test_handoff_open_blackout():
    v = evaluate(thesis_d(), state(news_in_session=False, minutes_since_open=5,
                                   gap_pct=0.01), GATE_CFG)
    assert (v.rule, v.veto_reason) == ("open_handoff", "GATE_OPEN_WINDOW")


def test_handoff_priced_in_large_gap():
    # gap 3% vs est 5.5% -> ratio 0.545 >= 0.5 -> priced in
    v = evaluate(thesis_d(), state(news_in_session=False, minutes_since_open=30,
                                   gap_pct=0.03), GATE_CFG)
    assert v.veto_reason == "PRICED_IN"


def test_handoff_small_gap_passes():
    v = evaluate(thesis_d(), state(news_in_session=False, minutes_since_open=30,
                                   gap_pct=0.01, last_price=101.0), GATE_CFG)
    assert v.verdict == "PASS" and v.rule == "open_handoff"


# ---- indicators + C8 features -----------------------------------------------------------

def test_atr14_needs_15_bars():
    assert atr14(FakeData.flat_daily(10)) is None
    assert atr14(FakeData.flat_daily(20)) == pytest.approx(1.0, rel=0.05)


def test_adv20():
    assert adv20(FakeData.flat_daily(25, volume=2_000_000)) == 2_000_000


def test_realized_vol_flat_is_zero():
    assert realized_vol(FakeData.flat_daily(30)) == pytest.approx(0.0, abs=1e-6)


def test_c8_features_shapes():
    from c8_regime.service import compute_features
    md = FakeData()
    feats = asyncio.run(compute_features(md))
    assert feats["index_trend"] in ("above_50d", "below_50d")
    assert 0.0 <= feats["breadth_proxy"] <= 1.0
    assert "realized_vol_20d" in feats
    assert "vix" not in feats                       # honest naming: proxy, not VIX
    assert feats["source"] == "etf_proxies_iex"
    assert len(feats["sector_rs"]["top"]) == 3

