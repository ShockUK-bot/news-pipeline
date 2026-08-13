"""v0.12.10 unit tests — DB-free.

Part 1: the confirmation re-check window (c3_gate.service.recheck_delay +
RECHECKABLE_VETOES). Incident 2026-08-03: every intraday evaluation ran
once at minutes=3-6 and NO_CONFIRM was terminal, so the 30-minute window
was a one-shot check — all 21 theses of the first post-recovery session
died at the gate (Boeing 09:41: vol_mult 3.17 but pct_move +0.4% at
minute 3, never looked at again). These tests pin the scheduling
arithmetic: re-checks step through the window, the LAST check lands before
expiry (final verdict is the real reason, never a misleading GATE_WINDOW),
and only verdicts whose inputs can change are re-checkable.

Part 2: counterfactual outcome derivation (c3_gate.counterfactual
.derive_outcomes) — the pure half of the §14 measurement table.
"""
from datetime import datetime, timedelta, timezone

import pytest

from c3_gate.counterfactual import derive_outcomes
from c3_gate.service import RECHECKABLE_VETOES, recheck_delay


def T(h, m, s=0):
    return datetime(2026, 8, 3, h, m, s, tzinfo=timezone.utc)


CFG = {"intraday_window_min": 30, "confirm_recheck_secs": 180,
       "confirm_final_margin_secs": 45}


# ---- recheck_delay ----------------------------------------------------------

def test_early_veto_rechecks_at_step():
    # incident shape: evaluated at minute 3 of a 30-minute window ->
    # plenty of window left, defer one full step (180s).
    assert recheck_delay(T(14, 0), T(14, 3), CFG) == 180.0


def test_rechecks_step_through_the_whole_window():
    # walk the schedule: checks at ~3, 6, 9, ... minutes; every defer while
    # >margin remains is the full step, so the window gets ~9 looks instead
    # of the old single one.
    now, checks = T(14, 3), 1
    while True:
        d = recheck_delay(T(14, 0), now, CFG)
        if d is None:
            break
        now += timedelta(seconds=d)
        checks += 1
        assert checks < 30, "re-check schedule failed to terminate"
    assert checks >= 9
    # and the final evaluation still happens INSIDE the window:
    assert now < T(14, 30)


def test_last_recheck_lands_before_window_expiry():
    # 200s of window left, margin 45 -> defer 155s, NOT the full 180 step
    # (which would overshoot into GATE_WINDOW mislabeling territory).
    assert recheck_delay(T(14, 0), T(14, 26, 40), CFG) == 155.0


def test_inside_final_margin_is_final():
    # 40s left (< 45 margin): the verdict stands, no more scheduling.
    assert recheck_delay(T(14, 0), T(14, 29, 20), CFG) is None


def test_window_expired_is_final():
    assert recheck_delay(T(14, 0), T(14, 30), CFG) is None
    assert recheck_delay(T(14, 0), T(15, 0), CFG) is None


def test_delay_floor_prevents_busy_reclaims():
    # 50s remaining, margin 45 -> remaining-margin is 5s, exactly the floor;
    # a tiny sliver of window never turns into sub-second re-claim spin.
    assert recheck_delay(T(14, 0), T(14, 29, 10), CFG) == 5.0


def test_future_skewed_publish_cannot_park_message():
    # publish stamped an hour ahead: remaining is huge, delay is capped at
    # one step — the message keeps cycling harmlessly (defer refunds the
    # claim attempt) instead of parking for an hour.
    assert recheck_delay(T(15, 0), T(14, 0), CFG) == 180.0


def test_recheckable_set_is_exactly_the_changeable_verdicts():
    # inputs that can change inside the window: price/volume build
    # (NO_CONFIRM), outlets corroborate (CREDIBILITY), bar gaps heal
    # (MARKETDATA_MISSING), a cached bar refreshes (STALE_MARKETDATA,
    # v0.12.28). Everything else is terminal by nature.
    assert RECHECKABLE_VETOES == {"GATE_NO_CONFIRM", "MARKETDATA_MISSING",
                                  "CREDIBILITY", "STALE_MARKETDATA"}
    for terminal in ("LONG_ONLY", "GATE_EXTENDED", "GATE_WINDOW",
                     "PRICED_IN", "GATE_OPEN_WINDOW", "SCANNER_STALE"):
        assert terminal not in RECHECKABLE_VETOES


# ---- derive_outcomes --------------------------------------------------------

def bar(h, m, close, high=None, low=None):
    return {"ts": T(h, m), "open": close, "close": close,
            "high": high if high is not None else close,
            "low": low if low is not None else close}


def test_outcomes_from_full_session_tape():
    veto = T(14, 0)
    bars = ([bar(14, i, 100 + i * 0.1) for i in range(0, 60)]          # drift up
            + [bar(15, i, 106.0, high=108.0, low=99.0) for i in range(0, 5)]
            + [bar(16, 0, 107.0)])
    out = derive_outcomes(bars, veto, 100.0)
    assert out["price_30m"] == 103.0          # bar stamped 14:30
    assert out["price_2h"] == 107.0           # checkpoint past close -> last bar
    assert out["price_eod"] == 107.0
    assert out["max_up_pct"] == pytest.approx(0.08)     # high 108 from 100
    assert out["max_down_pct"] == pytest.approx(-0.01)  # low 99 from 100


def test_checkpoint_before_first_bar_falls_back_to_first():
    # tape starts late (halt, thin symbol): the 30m checkpoint has no bar at
    # or before it -> first available bar, never a crash or a None.
    veto = T(14, 0)
    bars = [bar(15, 30, 105.0), bar(15, 55, 106.0)]
    out = derive_outcomes(bars, veto, 100.0)
    assert out["price_30m"] == 105.0
    assert out["price_eod"] == 106.0


def test_empty_tape_yields_nulls_not_zeroes():
    out = derive_outcomes([], T(14, 0), 100.0)
    assert all(v is None for v in out.values())


def test_missing_veto_price_skips_excursions_only():
    out = derive_outcomes([bar(14, 30, 50.0)], T(14, 0), None)
    assert out["price_eod"] == 50.0
    assert out["max_up_pct"] is None and out["max_down_pct"] is None
