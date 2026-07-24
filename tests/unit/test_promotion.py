"""v0.12.2 unit tests — scanner-trade promotion.

A12 verdicts the confirmation (news_confirms_move); C4 code executes the
promotion. Covered here: the schema cross-field rules, the pure
promoted_policy transform (what changes vs what must NEVER change), and the
force-flat interaction (a promoted position stops force-flatting).
"""
import json
from datetime import datetime, timezone

import pytest

from a12_guard.schema import (GuardValidationError, guard_json_schema,
                              validate_guard)
from c4_exec.engine import promoted_policy

UTC = timezone.utc

SHORT_PROFILE = {
    "initial_stop": {"method": "atr", "k": 2.0},
    "catastrophe": {"method": "atr", "k": 3.5},
    "breakeven_at_R": 1.0,
    "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
    "time_stop": {"window": "thesis", "min_progress_R": 0.5},
    "realization": {"target_fraction": 0.7, "action": "scale_out_50"},
    "earnings_blackout_exit": True,
    "overnight_hold": "eod_rule_v1",
}


def _verdict(**over):
    d = {"thesis_intact": True, "recommended_action": "hold",
         "urgency": "low", "confidence": 0.6, "watch_hits": [],
         "news_confirms_move": False, "reason": "noise item, thesis stands"}
    d.update(over)
    return json.dumps(d)


def scalp_policy(**over):
    p = {"profile": "scalp_v1", "origin": "scanner",
         "initial_stop": {"method": "atr_5m", "k": 2.0, "price": 118.0},
         "catastrophe_stop_broker": {"k": 3.5, "price": 116.5},
         "breakeven_at_R": 0.75,
         "trail": {"activate_at_R": 1.0, "method": "atr_5m", "k": 1.5},
         "time_stop": {"window_minutes": 60, "min_progress_R": 0.5},
         "realization": {"target_fraction": 0.6, "action": "scale_out_50"},
         "overnight_hold": "force_flat", "force_flat_time_et": "15:50",
         "earnings_blackout_exit": True, "magnitude_est": 0.02,
         "atr_14": 4.0, "atr_value": 0.5, "atr_method": "atr_5m",
         "current_stop": 119.2, "stop_basis": "breakeven", "hwm": 121.0,
         "scale_out_done": True,
         "machine_invalidations": [], "news_invalidations": ["offering"]}
    p.update(over)
    return p


# ---- schema ------------------------------------------------------------------

def test_confirms_field_defaults_false_and_old_outputs_still_parse():
    raw = json.loads(_verdict())
    raw.pop("news_confirms_move")                     # pre-v0.12.2 output shape
    v = validate_guard(json.dumps(raw))
    assert v.news_confirms_move is False


def test_confirms_with_intact_thesis_parses():
    v = validate_guard(_verdict(news_confirms_move=True))
    assert v.news_confirms_move and v.thesis_intact


def test_confirms_requires_intact_thesis():
    with pytest.raises(GuardValidationError):
        validate_guard(_verdict(news_confirms_move=True, thesis_intact=False,
                                recommended_action="exit"))


def test_confirms_contradicts_exit():
    with pytest.raises(GuardValidationError):
        validate_guard(_verdict(news_confirms_move=True,
                                recommended_action="exit"))


def test_schema_stays_grammar_safe():
    s = guard_json_schema()
    assert s["properties"]["news_confirms_move"]["type"] == "boolean"
    assert s["additionalProperties"] is False


# ---- promoted_policy transform -------------------------------------------------

def test_promotion_changes_the_right_things():
    p = promoted_policy(scalp_policy(), SHORT_PROFILE, decision_id=42,
                        now_iso="2026-07-24T15:00:00+00:00")
    assert p["profile"] == "short_term_v1"
    assert p["promoted_from"] == "scalp_v1"
    assert p["promotion_decision_id"] == 42
    assert p["promoted_ts"] == "2026-07-24T15:00:00+00:00"
    assert p["overnight_hold"] == "eod_rule_v1"
    assert "force_flat_time_et" not in p
    assert p["time_stop"] == {"window": "2_sessions", "min_progress_R": 0.5}
    assert p["trail"] == SHORT_PROFILE["trail"]
    assert p["breakeven_at_R"] == 1.0
    assert p["atr_value"] == 4.0 and p["atr_method"] == "atr"   # daily basis now


def test_promotion_never_touches_risk_state():
    """The tighten-only doctrine survives promotion: live stop state,
    catastrophe order, realization progress and invalidations unchanged."""
    before = scalp_policy()
    p = promoted_policy(before, SHORT_PROFILE, 1, "t")
    assert p["current_stop"] == before["current_stop"]
    assert p["stop_basis"] == before["stop_basis"]
    assert p["hwm"] == before["hwm"]
    assert p["catastrophe_stop_broker"] == before["catastrophe_stop_broker"]
    assert p["initial_stop"] == before["initial_stop"]
    assert p["scale_out_done"] is True
    assert p["realization"] == before["realization"]
    assert p["news_invalidations"] == before["news_invalidations"]
    assert p["origin"] == "scanner"
    assert p["earnings_blackout_exit"] is True
    # and the input dict was not mutated
    assert before["profile"] == "scalp_v1"
    assert "promoted_ts" not in before


def test_promotion_is_marked_once():
    p = promoted_policy(scalp_policy(), SHORT_PROFILE, 1, "t1")
    assert p.get("promoted_ts")                        # the idempotency marker


# ---- force-flat interaction ----------------------------------------------------

async def test_promoted_position_no_longer_force_flats(monkeypatch):
    """After promotion, overnight_hold is eod_rule_v1 — force_flat_pass must
    ignore the position (it now belongs to the 15:45 D1 decision instead)."""
    from c4_exec import engine as engine_mod
    from c4_exec.engine import PositionEngine

    exits = []
    promoted = promoted_policy(scalp_policy(), SHORT_PROFILE, 1, "t")
    pos = {"position_id": 9, "ticker": "MU", "horizon": "SHORT",
           "qty_open": 100, "avg_entry": 119.0, "r_unit": 1.0,
           "exit_policy": promoted,
           "opened_ts": datetime(2026, 7, 24, 15, 0, tzinfo=UTC),
           "last_price": 120.0}

    async def fake_open_positions():
        return [pos]

    async def fake_execute_exit(*a, **k):
        exits.append(a)
        return "FILLED"

    async def fake_position_event(*a, **k):
        pass

    monkeypatch.setattr(engine_mod, "open_positions", fake_open_positions)
    monkeypatch.setattr(engine_mod, "execute_exit", fake_execute_exit)
    monkeypatch.setattr(engine_mod, "position_event", fake_position_event)
    now = datetime(2026, 7, 24, 19, 55, tzinfo=UTC)     # 15:55 ET (EDT)
    eng = PositionEngine(broker=None, now_fn=lambda: now)
    assert await eng.force_flat_pass() == []
    assert exits == []
