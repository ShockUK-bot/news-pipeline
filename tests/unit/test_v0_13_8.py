"""v0.13.8 unit tests — the MRNA/MSTR non-trade fixes (incident 2026-08-19).

Every test is pinned to a real number from that day's journal, so a future
refactor that quietly restores the old behaviour fails here with the
incident in the failure message.

Reference facts (journal.scanner_candidates / journal.decisions, ET):
  09:51:36-09:54:23  ALL 6 daily emissions spent (3 scans, top-2 each);
                     scores of slots 4-6: AVGO 0.5527, AXTI 0.5428,
                     RKLB 0.4820
  09:54:23           LAST scanner_candidates row of the entire session —
                     scan_once early-returned on every later cycle; MSTR
                     (+12.4% that afternoon) has NO row of any status
  24-26 min          queue wait of every scanner signal at A2 (enqueued at
                     default priority 100 behind ~30 news signals) vs a
                     ~5-minute gate staleness budget; 3x SCANNER_STALE
  WYFI / AXTI        2 of 6 slots spent on down-movers whose short entry was
                     already impossible (no borrow / through the -10% SSR
                     trigger) — knowable before spending the slot
  6x in 40 min       A2 'invalid thesis output' retries, all the identical
                     cosmetic violation: "45 minutes" for "45_minutes",
                     each costing a full extra model call
"""
import inspect

import pytest

from a1_triage import service as a1_service
from a2_analyst import service as a2_service
from a2_analyst.schema import (ThesisValidationError, coerce_window,
                               validate_thesis)
from c10_scanner.rules import (CandidateMetrics, emission_disposition,
                               scan_mode)
from c10_scanner.service import C10Service

CFG = {"max_per_scan": 2, "max_per_day": 15, "max_per_hour": 6,
       "max_concurrent_positions": 2, "min_emit_score": 0.60,
       "precheck_short_availability": True, "ssr_trigger_pct": -0.10,
       "observe_after_cap": True}


def metrics(**over):
    m = CandidateMetrics(
        ticker="MSTR", price=104.0, prev_close=92.6, move_pct=0.123,
        adv20_dollars=5_000_000_000.0, rel_volume=3.4, minutes_since_hod=8,
        spread_bps=5.0, luld_headroom_pct=0.08, vwap=101.2,
        day_high=104.4, detected_ts="2026-08-19T17:30:00+00:00")
    for k, v in over.items():
        setattr(m, k, v)
    return m


def disp(score=0.90, m=None, **over):
    kw = dict(emitted_this_scan=0, emitted_today=0, emitted_last_hour=0,
              open_scanner=0, etb_ok=None)
    kw.update(over)
    return emission_disposition(score, m or metrics(), CFG, **kw)


# ---- scan_mode: the cap must never blind the scanner -------------------------

def test_cap_reached_means_observe_not_idle():
    """08-19: cap hit 09:54:23, scanner journaled nothing until close."""
    assert scan_mode(15, CFG) == "OBSERVE"
    assert scan_mode(99, CFG) == "OBSERVE"


def test_budget_remaining_means_emit():
    assert scan_mode(0, CFG) == "EMIT"
    assert scan_mode(14, CFG) == "EMIT"


def test_observe_after_cap_is_disableable():
    assert scan_mode(15, {**CFG, "observe_after_cap": False}) == "IDLE"


def test_scan_once_no_longer_early_returns_at_the_daily_cap():
    src = inspect.getsource(C10Service.scan_once)
    assert "scan_mode(" in src                 # the mode decision is live
    assert "daily cap reached ({emitted_count})\"" not in src  # old message+return


# ---- emission_disposition: quality floor and viability spend no budget ------

def test_score_floor_rejects_the_08_19_slot_wasters():
    """AVGO 0.5527 / AXTI 0.5428 / RKLB 0.4820 consumed slots 4-6 of 6."""
    for s in (0.5527, 0.5428, 0.4820):
        assert disp(score=s) == ("FILTERED", "SCORE_FLOOR")


def test_score_floor_is_filtered_not_capped():
    """A floor reject must not read as budget consumption in the journal."""
    status, _ = disp(score=0.10)
    assert status == "FILTERED"


def test_floor_checked_before_every_cap():
    """Even with every cap simultaneously exhausted, a weak candidate
    journals SCORE_FLOOR — the honest reason, not an incidental cap."""
    assert disp(score=0.10, emitted_this_scan=9, emitted_today=99,
                emitted_last_hour=99, open_scanner=9) \
        == ("FILTERED", "SCORE_FLOOR")


def test_ssr_bound_down_mover_never_spends_a_slot():
    """AXTI-shaped: a loser at/through -10% is Reg-SHO-restricted and C3's
    ssr_veto fails closed on it — the veto was knowable at emission time."""
    m = metrics(move_pct=-0.102)
    assert disp(m=m) == ("FILTERED", "SSR_RESTRICTED")
    assert disp(m=metrics(move_pct=-0.07)) is None       # above trigger: fine


def test_unborrowable_down_mover_never_spends_a_slot():
    """WYFI-shaped: no easy-to-borrow shares -> SHORT_UNAVAILABLE at every
    later stage; refuse at emission instead."""
    m = metrics(move_pct=-0.07)
    assert disp(m=m, etb_ok=False) == ("FILTERED", "SHORT_UNAVAILABLE")
    assert disp(m=m, etb_ok=True) is None


def test_borrow_unknown_is_not_a_verdict():
    """etb_ok None = 'not looked up' (no creds / long candidate / deferred
    lookup) — the disposition must not fabricate a borrow verdict; C3 still
    fails closed downstream."""
    assert disp(m=metrics(move_pct=-0.07), etb_ok=None) is None


def test_up_movers_ignore_the_short_precheck():
    assert disp(m=metrics(move_pct=0.123), etb_ok=False) is None


def test_short_precheck_is_disableable():
    cfg = {**CFG, "precheck_short_availability": False}
    m = metrics(move_pct=-0.15)
    assert emission_disposition(0.9, m, cfg, emitted_this_scan=0,
                                emitted_today=0, emitted_last_hour=0,
                                open_scanner=0, etb_ok=False) is None


# ---- emission_disposition: the cap family ------------------------------------

@pytest.mark.parametrize("over,reason", [
    ({"emitted_this_scan": 2}, "PER_SCAN"),
    ({"emitted_today": 15}, "PER_DAY"),
    ({"emitted_last_hour": 6}, "PER_HOUR"),
    ({"open_scanner": 2}, "CONCURRENT"),
])
def test_cap_matrix(over, reason):
    assert disp(**over) == ("CAPPED", reason)


def test_per_hour_pacing_stops_an_08_19_shaped_burst():
    """08-19: 6/6 spent in 2m47s. With max_per_hour 6 and 6 already emitted
    this rolling hour, the 7th-best candidate journals PER_HOUR while the
    daily budget (15) survives for the afternoon."""
    assert disp(emitted_this_scan=0, emitted_today=6, emitted_last_hour=6) \
        == ("CAPPED", "PER_HOUR")


def test_per_hour_zero_disables_pacing():
    cfg = {**CFG, "max_per_hour": 0}
    assert emission_disposition(0.9, metrics(), cfg, emitted_this_scan=0,
                                emitted_today=0, emitted_last_hour=50,
                                open_scanner=0, etb_ok=None) is None


def test_clean_high_score_candidate_emits():
    assert disp() is None


# ---- A1: scanner signals claim first and enqueue with priority ---------------

def test_scanner_signals_enqueue_with_priority():
    """08-19: enqueued at the default 100, all six waited 24-26 min behind
    the news backlog. queue.claim_next has always ordered by priority."""
    src = inspect.getsource(a1_service.A1Service.handle_scanner)
    assert "scanner_analyst_priority" in src
    assert 'enqueue("signal.analyst", signal_id, out, conn=conn)' not in src


def test_a1_consume_loop_claims_scanner_queue_first():
    src = inspect.getsource(a1_service.consume_loop)
    assert src.index("claim(SCANNER_QUEUE") < src.index("claim(IN_QUEUE")


# ---- A2: the queue wait is journaled ----------------------------------------

def test_a2_journals_queue_wait():
    src = inspect.getsource(a2_service.A2Service.handle)
    assert src.count('"queue_wait_secs"') >= 3   # data-err, invalid, no-trade,
    assert "enqueued_ts" in src                  # thesis payloads


# ---- A2 schema: cosmetic window variants normalize, one model call ----------

@pytest.mark.parametrize("raw,canonical", [
    ("45 minutes", "45_minutes"),        # the exact 08-19 failure, 6x
    ("45-minutes", "45_minutes"),
    ("45_Minutes", "45_minutes"),
    ("45 min", "45_minutes"),
    ("45mins", "45_minutes"),
    ("1 hour", "60_minutes"),            # hours -> minutes (no hours unit)
    ("2 hrs", "120_minutes"),
    ("3 days", "3_sessions"),            # calendar-honest reading
    ("2 weeks", "2_weeks"),
    ("1 wk", "1_weeks"),
    ("2_sessions", "2_sessions"),        # canonical passes through untouched
])
def test_window_coercion(raw, canonical):
    assert coerce_window(raw) == canonical


@pytest.mark.parametrize("raw", [
    "soon", "two_weeks", "45", "minutes", "", "45_fortnights", "9999 minutes",
])
def test_ambiguous_windows_still_fail_strict(raw):
    assert coerce_window(raw) == raw     # untouched -> strict validator reports


def _thesis(window):
    import json
    return json.dumps({
        "ticker": "MRNA", "direction": "up", "magnitude_est": 0.04,
        "expected_move_window": window, "horizon": "SHORT",
        "confidence": 0.5, "priced_in_assessment": "x",
        "source_risk": "low", "invalidation": {}, "reason": "y"})


def test_validate_thesis_accepts_cosmetic_variant_first_try():
    t = validate_thesis(_thesis("45 minutes"))
    assert t.expected_move_window == "45_minutes"


def test_validate_thesis_still_rejects_garbage():
    with pytest.raises(ThesisValidationError):
        validate_thesis(_thesis("sometime_soon"))
