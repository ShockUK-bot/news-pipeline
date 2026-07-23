"""v0.12.1 unit tests — scalp_v1 exit machinery in C4 and A3.

Covers: minutes-based time stop, atr_value stop basis, A3's scalp policy
materialization (force-flat fields, minutes window, atr_method), scalp
profile selection by origin, and the engine's force_flat_pass (DB and broker
monkeypatched — the decision logic is what's under test).
"""
from datetime import datetime, timedelta, timezone

import pytest

from a3_risk.service import A3Service, RiskAdjustments
from c4_exec import engine as engine_mod
from c4_exec.engine import PositionEngine
from c4_exec.exits import evaluate_on_bar

UTC = timezone.utc


def scalp_pos(**over):
    policy = {
        "profile": "scalp_v1",
        "origin": "scanner",
        "initial_stop": {"method": "atr_5m", "k": 2.0, "price": 118.0},
        "catastrophe_stop_broker": {"k": 3.5, "price": 116.5},
        "breakeven_at_R": 0.75,
        "trail": {"activate_at_R": 1.0, "method": "atr_5m", "k": 1.5},
        "time_stop": {"window_minutes": 60, "min_progress_R": 0.5},
        "realization": {"target_fraction": 0.6, "action": "scale_out_50"},
        "overnight_hold": "force_flat",
        "force_flat_time_et": "15:50",
        "magnitude_est": 0.02,
        "atr_14": 4.0,            # daily — context only
        "atr_value": 0.5,         # 5-min — the stop basis
        "atr_method": "atr_5m",
    }
    policy.update(over.pop("policy", {}))
    pos = {"position_id": 7, "ticker": "MU", "horizon": "SHORT",
           "qty_open": 100, "avg_entry": 119.0, "r_unit": 1.0,
           "exit_policy": policy,
           "opened_ts": datetime(2026, 7, 23, 15, 0, tzinfo=UTC),
           "last_price": None}
    pos.update(over)
    return pos


def bar(o=119.0, h=119.3, l=118.8, c=119.1):
    return {"ts": 1753280000, "open": o, "high": h, "low": l, "close": c}


# ---- minutes time stop -------------------------------------------------------

def test_minutes_time_stop_fires_when_stalled():
    a = evaluate_on_bar(scalp_pos(), bar(), session_age=0, minutes_open=65)
    assert len(a) == 1 and (a[0].kind, a[0].layer) == ("EXIT", "TIME")
    assert "65min >= 60min" in a[0].reason


def test_minutes_time_stop_holds_before_window():
    a = evaluate_on_bar(scalp_pos(), bar(), session_age=0, minutes_open=45)
    assert all(x.kind != "EXIT" for x in a)


def test_minutes_time_stop_holds_when_progressing():
    # +0.6R at the mark — the mover is still moving; time stop stays quiet
    a = evaluate_on_bar(scalp_pos(), bar(c=119.6, h=119.7), session_age=0,
                        minutes_open=90)
    assert all(x.layer != "TIME" for x in a)


def test_minutes_time_stop_needs_minutes_open():
    # engine could not compute minutes (no opened_ts) -> no false TIME exit
    a = evaluate_on_bar(scalp_pos(), bar(), session_age=0, minutes_open=None)
    assert all(x.layer != "TIME" for x in a)


def test_session_time_stop_unaffected():
    pos = scalp_pos(policy={"time_stop": {"window": "2_sessions",
                                          "min_progress_R": 0.5}})
    a = evaluate_on_bar(pos, bar(), session_age=2, minutes_open=10)
    assert len(a) == 1 and a[0].layer == "TIME"      # sessions branch still works


# ---- atr_value stop basis ----------------------------------------------------

def test_trail_uses_atr_value_not_daily_atr():
    # +1.2R -> trail activates. hwm 120.2; trail = hwm - 1.5 * atr_value(0.5)
    # = 119.45. With daily atr_14 (4.0) the proposal would be 114.2 (discarded
    # by tighten-only) — the 5-min basis is what makes the scalp trail tight.
    a = evaluate_on_bar(scalp_pos(), bar(c=120.2, h=120.2), session_age=0,
                        minutes_open=10)
    stops = [x for x in a if x.kind == "SET_STOP"]
    assert stops and stops[0].new_stop == pytest.approx(119.45, abs=0.01)
    assert stops[0].new_basis == "trail"


def test_legacy_policy_without_atr_value_still_works():
    pos = scalp_pos(policy={"atr_value": None})
    pos["exit_policy"].pop("atr_value")
    a = evaluate_on_bar(pos, bar(), session_age=0, minutes_open=5)
    assert isinstance(a, list)                        # falls back to atr_14


# ---- A3 scalp materialization ------------------------------------------------

SCALP_PROFILE = {
    "initial_stop": {"method": "atr_5m", "k": 2.0},
    "catastrophe": {"method": "atr_5m", "k": 3.5},
    "breakeven_at_R": 0.75,
    "trail": {"activate_at_R": 1.0, "method": "atr_5m", "k": 1.5},
    "time_stop": {"window_minutes": 60, "min_progress_R": 0.5},
    "realization": {"target_fraction": 0.6, "action": "scale_out_50"},
    "earnings_blackout_exit": True,
    "overnight_hold": "force_flat",
    "force_flat_time_et": "15:50",
}
THESIS = {"magnitude_est": 0.02,
          "invalidation": {"machine_checkable": [], "news_checkable": []}}


def bare_a3() -> A3Service:
    svc = A3Service.__new__(A3Service)                # no backend/db needed
    svc.profiles = {"scalp_v1": SCALP_PROFILE,
                    "short_term_v1": {"x": 1}, "long_term_v1": {"x": 2}}
    return svc


def test_profile_for_origin():
    svc = bare_a3()
    assert svc.profile_for("SHORT", "scanner")[0] == "scalp_v1"
    assert svc.profile_for("LONG", "scanner")[0] == "scalp_v1"
    assert svc.profile_for("SHORT", "news")[0] == "short_term_v1"
    assert svc.profile_for("LONG")[0] == "long_term_v1"


def test_materialize_scalp_policy():
    svc = bare_a3()
    adj = RiskAdjustments(k=2.0, realization_fraction=0.6,
                          time_window_sessions=1, reason="defaults")
    p = svc.materialize_exit_policy("scalp_v1", SCALP_PROFILE, adj,
                                    limit_price=120.0, atr=0.5,
                                    thesis=THESIS, atr_14=4.0,
                                    atr_method="atr_5m", origin="scanner")
    assert p["initial_stop"]["price"] == pytest.approx(119.0)   # 2.0 * 0.5
    assert p["catastrophe_stop_broker"]["price"] == pytest.approx(118.25)
    assert p["time_stop"] == {"window_minutes": 60, "min_progress_R": 0.5}
    assert p["overnight_hold"] == "force_flat"
    assert p["force_flat_time_et"] == "15:50"
    assert p["atr_value"] == 0.5 and p["atr_14"] == 4.0
    assert p["atr_method"] == "atr_5m" and p["origin"] == "scanner"


def test_materialize_news_policy_unchanged_shape():
    svc = bare_a3()
    profile = {**SCALP_PROFILE,
               "time_stop": {"window": "thesis", "min_progress_R": 0.5},
               "overnight_hold": "eod_rule_v1"}
    profile.pop("force_flat_time_et")
    adj = RiskAdjustments(k=2.0, realization_fraction=0.7,
                          time_window_sessions=2, reason="d")
    p = svc.materialize_exit_policy("short_term_v1", profile, adj,
                                    limit_price=100.0, atr=2.0, thesis=THESIS)
    assert p["time_stop"] == {"window": "2_sessions", "min_progress_R": 0.5}
    assert "force_flat_time_et" not in p
    assert p["atr_value"] == 2.0 and p["atr_14"] == 2.0


# ---- force_flat_pass ---------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.exits = []
        self.events = []


@pytest.fixture
def rig(monkeypatch):
    rec = _Recorder()
    positions = []

    async def fake_open_positions():
        return positions

    async def fake_execute_exit(broker, pos, qty, layer, reason, bid, now_fn,
                                *a, **k):
        rec.exits.append((pos["ticker"], qty, layer, bid))
        return "FILLED"

    async def fake_position_event(pid, event_type, actor, **k):
        rec.events.append((pid, event_type))

    monkeypatch.setattr(engine_mod, "open_positions", fake_open_positions)
    monkeypatch.setattr(engine_mod, "execute_exit", fake_execute_exit)
    monkeypatch.setattr(engine_mod, "position_event", fake_position_event)
    return rec, positions


def engine_at(hhmm_et: str) -> PositionEngine:
    # 2026-07-23 EDT: ET = UTC-4
    h, m = map(int, hhmm_et.split(":"))
    now = datetime(2026, 7, 23, h + 4, m, tzinfo=UTC)
    return PositionEngine(broker=None, now_fn=lambda: now)


async def test_force_flat_fires_at_1550(rig):
    rec, positions = rig
    positions.append(scalp_pos(last_price=119.4))
    eng = engine_at("15:51")
    out = await eng.force_flat_pass()
    assert out == ["MU:FILLED"]
    assert rec.exits[0][1:3] == (100, "FORCE_FLAT")
    assert ("MU", 100, "FORCE_FLAT", pytest.approx(119.04, abs=0.01))[3] \
        == pytest.approx(rec.exits[0][3], abs=0.01)
    assert rec.events and rec.events[0][1] == "FORCE_FLAT"


async def test_force_flat_waits_for_its_time(rig):
    rec, positions = rig
    positions.append(scalp_pos())
    assert await engine_at("15:47").force_flat_pass() == []
    assert rec.exits == []


async def test_force_flat_ignores_news_positions(rig):
    rec, positions = rig
    news = scalp_pos()
    news["exit_policy"]["overnight_hold"] = "eod_rule_v1"
    positions.append(news)
    assert await engine_at("15:55").force_flat_pass() == []
    assert rec.exits == []


async def test_overnight_pass_skips_force_flat_positions(rig, monkeypatch):
    rec, positions = rig
    positions.append(scalp_pos())                      # would be EXIT stale_flat
    eng = engine_at("15:45")
    results = await eng.overnight_pass(
        {"hold_min_unrealized_R": 0.3, "young_max_age_sessions": 1,
         "young_max_realized_fraction": 0.5})
    assert results == []                               # force-flat lane skipped
    assert rec.exits == []
