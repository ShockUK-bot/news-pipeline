"""v0.14.6 unit tests.

1. Scanner freshness credit: minutes_since_extreme == 0 is the FRESHEST
   value and must score full credit; only None is "unknown".
2. Pre-close provisional invalidation pass (15:55 ET): peek() evaluates a
   provisional session bar without consuming the monitor; the engine pass
   exits when the predicate would fire and leaves the 16:01 pass intact.
3. Stop slippage journaled on the exit event: ExitAction.trigger_price is
   set per layer and record_exit writes trigger_price / slip_px / slip_r.
"""
from datetime import datetime, timezone

import pytest

from c10_scanner.rules import CandidateMetrics, score_candidate
from c4_exec import engine as engine_mod
from c4_exec import state as state_mod
from c4_exec.engine import PositionEngine
from c4_exec.exits import evaluate_on_bar
from common.invalidation_dsl import ArmContext, Bar, compile_predicate

UTC = timezone.utc


# ---------------------------------------------------------------------------
# 1. freshness credit
# ---------------------------------------------------------------------------

def metrics(**over):
    m = CandidateMetrics(
        ticker="MU", price=120.0, prev_close=113.0, move_pct=0.062,
        adv20_dollars=900_000_000.0, rel_volume=4.1, minutes_since_hod=12,
        spread_bps=6.0, luld_headroom_pct=0.09, vwap=118.4,
        day_high=120.6, detected_ts="2026-07-23T15:00:00+00:00")
    for k, v in over.items():
        setattr(m, k, v)
    return m


def test_zero_minutes_since_extreme_scores_full_freshness():
    at_high = score_candidate(metrics(minutes_since_hod=0))
    stale = score_candidate(metrics(minutes_since_hod=60))
    assert at_high - stale == pytest.approx(0.15, abs=1e-3)   # w_fresh default


def test_unknown_freshness_still_scores_zero():
    unknown = score_candidate(metrics(minutes_since_hod=None))
    stale = score_candidate(metrics(minutes_since_hod=60))
    assert unknown == pytest.approx(stale, abs=1e-4)


def test_down_mover_at_low_scores_full_freshness():
    at_low = score_candidate(metrics(move_pct=-0.062, minutes_since_lod=0,
                                     minutes_since_hod=45))
    stale = score_candidate(metrics(move_pct=-0.062, minutes_since_lod=60,
                                    minutes_since_hod=45))
    assert at_low - stale == pytest.approx(0.15, abs=1e-3)


def test_avav_2026_09_10_regression():
    # journaled 0.6236 with the bug; the formula says ~0.774 at 0 min from HOD
    m = metrics(ticker="AVAV", price=157.65, move_pct=0.1197,
                adv20_dollars=230_000_000.0, rel_volume=13.93,
                minutes_since_hod=0, spread_bps=34.92)
    assert score_candidate(m) == pytest.approx(0.774, abs=0.005)


# ---------------------------------------------------------------------------
# 2. provisional pre-close invalidation
# ---------------------------------------------------------------------------

def _ctx(prenews=100.0, mark=101.0):
    return ArmContext(entry_price=101.0, initial_stop=98.0, r_unit=3.0,
                      prenews_price=prenews, atr_14=1.5, mark=mark)


def session_bar(close, ts=1_757_000_000):
    return Bar(ts=ts, tf="session", open=101.0, high=102.0, low=99.0,
               close=close, vwap=100.5, volume_ratio=1.0)


def test_peek_reports_without_consuming_the_monitor():
    p = compile_predicate({"std": "close_below_prenews"}, _ctx())
    assert p.peek(session_bar(99.5)) is True
    assert p.fired is False and p._streak == 0
    assert all(c._prev is None for c in p.conds)
    assert p.peek(session_bar(100.5)) is False
    # the real pass still fires afterwards — confirmation path intact
    fire = p.on_bar(session_bar(99.5))
    assert fire is not None and fire.predicate_id == "close_below_prenews"
    assert p.peek(session_bar(99.5)) is False          # fired -> never again


def test_peek_ignores_other_timeframes():
    p = compile_predicate({"std": "close_below_prenews"}, _ctx())
    minute = Bar(ts=1, tf="1m", open=101.0, high=101.0, low=99.0,
                 close=99.0, vwap=100.0, volume_ratio=1.0)
    assert p.peek(minute) is False


def news_pos(**over):
    policy = {
        "profile": "short_term_v1", "origin": "news", "side": "LONG",
        "initial_stop": {"method": "atr", "k": 2.0, "price": 98.0},
        "catastrophe_stop_broker": {"k": 3.5, "price": 95.75},
        "breakeven_at_R": 1.0,
        "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
        "time_stop": {"window": "2_sessions", "min_progress_R": 0.5},
        "realization": {"target_fraction": 0.7, "action": "scale_out_50"},
        "overnight_hold": "eod_rule_v1",
        "magnitude_est": 0.02, "atr_14": 1.5, "prenews_price": 100.0,
        "machine_invalidations": ["close_below_prenews"],
    }
    policy.update(over.pop("policy", {}))
    pos = {"position_id": 35, "ticker": "BEN", "horizon": "SHORT",
           "side": "LONG", "qty_open": 332, "avg_entry": 101.0,
           "initial_stop": 98.0, "r_unit": 3.0, "exit_policy": policy,
           "opened_ts": datetime(2026, 9, 8, 13, 47, tzinfo=UTC),
           "last_price": 101.0}
    pos.update(over)
    return pos


class _Rec:
    def __init__(self):
        self.exits, self.events = [], []


@pytest.fixture
def rig(monkeypatch):
    rec, positions = _Rec(), []

    async def fake_open_positions():
        return positions

    async def fake_execute_exit(broker, pos, qty, layer, reason, bid, now_fn,
                                *a, **k):
        rec.exits.append((pos["ticker"], qty, layer, bid,
                          k.get("trigger_price")))
        return "FILLED"

    async def fake_position_event(pid, event_type, actor, **k):
        rec.events.append((pid, event_type, k.get("new_value")))

    monkeypatch.setattr(engine_mod, "open_positions", fake_open_positions)
    monkeypatch.setattr(engine_mod, "execute_exit", fake_execute_exit)
    monkeypatch.setattr(engine_mod, "position_event", fake_position_event)
    return rec, positions


def engine_at(hhmm_et):
    h, m = map(int, hhmm_et.split(":"))
    now = datetime(2026, 9, 8, h + 4, m, tzinfo=UTC)        # EDT = UTC-4
    return PositionEngine(broker=None, now_fn=lambda: now)


def bar_fn(close):
    async def _fn(ticker):
        return {"open": 101.0, "high": 102.0, "low": 99.0, "close": close}
    return _fn


async def test_preclose_pass_exits_when_close_below_prenews(rig):
    rec, positions = rig
    positions.append(news_pos())
    eng = engine_at("15:55")
    out = await eng.preclose_invalidation_pass(bar_fn(99.6))
    assert out == ["BEN:FILLED"]
    ticker, qty, layer, bid, trigger = rec.exits[0]
    assert (ticker, qty, layer) == ("BEN", 332, "INVALIDATION")
    assert bid == pytest.approx(99.6 * 0.999, abs=0.01)     # sell under the mark
    assert trigger == pytest.approx(99.6)
    fired = [e for e in rec.events if e[1] == "INVALIDATION_FIRED"]
    assert fired and fired[0][2]["provisional"] is True
    assert fired[0][2]["predicate"] == "close_below_prenews"
    assert 35 not in eng.monitors                          # position closed


async def test_preclose_pass_holds_when_close_above_prenews(rig):
    rec, positions = rig
    positions.append(news_pos())
    eng = engine_at("15:55")
    assert await eng.preclose_invalidation_pass(bar_fn(100.4)) == []
    assert rec.exits == []
    # monitor untouched: the real 16:01 pass can still fire on a real close
    p = eng.monitors[35][0]
    assert p.fired is False
    assert p.on_bar(session_bar(99.6)) is not None


async def test_preclose_pass_skips_positions_without_session_predicates(rig):
    rec, positions = rig
    positions.append(news_pos(policy={"machine_invalidations": []}))
    assert await engine_at("15:55").preclose_invalidation_pass(bar_fn(90.0)) == []
    assert rec.exits == []


async def test_preclose_pass_leaves_monitor_armed_if_exit_unfilled(
        rig, monkeypatch):
    rec, positions = rig
    positions.append(news_pos())

    async def reinstated(*a, **k):
        return "REINSTATED"
    monkeypatch.setattr(engine_mod, "execute_exit", reinstated)
    eng = engine_at("15:55")
    assert await eng.preclose_invalidation_pass(bar_fn(99.6)) == ["BEN:REINSTATED"]
    p = eng.monitors[35][0]
    assert p.fired is False                                # 16:01 still fires
    assert p.on_bar(session_bar(99.6)) is not None


# ---------------------------------------------------------------------------
# 3. trigger_price on ExitAction, slippage on the exit event
# ---------------------------------------------------------------------------

def scalp_pos(**over):
    policy = {
        "profile": "scalp_v1", "origin": "scanner", "side": "LONG",
        "initial_stop": {"method": "atr_5m", "k": 2.0, "price": 118.0},
        "catastrophe_stop_broker": {"k": 3.5, "price": 116.5},
        "breakeven_at_R": 0.75,
        "trail": {"activate_at_R": 1.0, "method": "atr_5m", "k": 1.5},
        "time_stop": {"window_minutes": 60, "min_progress_R": 0.5},
        "realization": {"target_fraction": 0.6, "action": "scale_out_50"},
        "overnight_hold": "force_flat", "force_flat_time_et": "15:50",
        "magnitude_est": 0.02, "atr_14": 4.0, "atr_value": 0.5,
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


def test_stop_action_carries_the_stop_as_trigger():
    a = evaluate_on_bar(scalp_pos(), bar(l=117.9), session_age=0, minutes_open=5)
    assert (a[0].kind, a[0].layer) == ("EXIT", "STOP")
    assert a[0].trigger_price == 118.0


def test_time_stop_carries_bar_close_as_trigger():
    a = evaluate_on_bar(scalp_pos(), bar(), session_age=0, minutes_open=65)
    assert (a[0].kind, a[0].layer) == ("EXIT", "TIME")
    assert a[0].trigger_price == 119.1


def test_target_scale_out_carries_target_as_trigger():
    # target = 119 * (1 + 0.6 * 0.02) = 120.428
    a = evaluate_on_bar(scalp_pos(), bar(h=120.6, c=120.5), session_age=0,
                        minutes_open=5)
    so = [x for x in a if x.kind == "SCALE_OUT"]
    assert so and so[0].trigger_price == pytest.approx(120.428, abs=1e-3)


class _Conn:
    def __init__(self):
        self.sql = []

    async def execute(self, sql, params=None):
        self.sql.append((sql, params))


@pytest.fixture
def exit_rig(monkeypatch):
    events = []

    async def fake_position_event(pid, event_type, actor, **k):
        events.append((pid, event_type, k.get("new_value")))
    monkeypatch.setattr(state_mod, "position_event", fake_position_event)
    return events


async def test_record_exit_journals_long_slippage(exit_rig):
    # long stopped at 118.00, filled 117.20: 0.80 worse = 0.8R at r_unit 1.0
    await state_mod.record_exit(7, 1, datetime.now(UTC), "STOP", 100, 117.20,
                                119.0, 1.0, is_partial=False, side="LONG",
                                conn=_Conn(), trigger_price=118.0)
    pid, etype, nv = exit_rig[0]
    assert etype == "EXIT" and nv["layer"] == "STOP"
    assert nv["trigger_price"] == 118.0
    assert nv["slip_px"] == pytest.approx(0.80)
    assert nv["slip_r"] == pytest.approx(0.80)


async def test_record_exit_journals_short_slippage_sign(exit_rig):
    # short stopped at 121.00, covered 121.50: worse by 0.50 (positive)
    await state_mod.record_exit(7, 1, datetime.now(UTC), "STOP", 100, 121.50,
                                119.0, 2.0, is_partial=False, side="SHORT",
                                conn=_Conn(), trigger_price=121.0)
    nv = exit_rig[0][2]
    assert nv["slip_px"] == pytest.approx(0.50)
    assert nv["slip_r"] == pytest.approx(0.25)


async def test_record_exit_fill_better_than_stop_is_negative(exit_rig):
    # KLAC 2026-09-04 shape: stop 183.69, filled 184.07 -> -0.38
    await state_mod.record_exit(34, 1, datetime.now(UTC), "STOP", 79, 184.07,
                                185.40, 1.71, is_partial=False, side="LONG",
                                conn=_Conn(), trigger_price=183.69)
    nv = exit_rig[0][2]
    assert nv["slip_px"] == pytest.approx(-0.38)
    assert nv["slip_r"] == pytest.approx(-0.222, abs=1e-3)


async def test_record_exit_without_trigger_keeps_old_shape(exit_rig):
    await state_mod.record_exit(7, 1, datetime.now(UTC), "OVERNIGHT", 100,
                                118.5, 119.0, 1.0, is_partial=False,
                                side="LONG", conn=_Conn())
    nv = exit_rig[0][2]
    assert set(nv) == {"layer", "qty", "price", "pnl"}


async def test_engine_apply_threads_trigger_price_to_execute_exit(rig):
    rec, positions = rig
    eng = engine_at("10:00")
    pos = scalp_pos()
    out = await eng._apply(pos, evaluate_on_bar(pos, bar(l=117.9),
                                                session_age=0, minutes_open=5),
                           bar(l=117.9))
    assert out == ["EXIT:STOP:FILLED"]
    assert rec.exits[0][4] == 118.0
