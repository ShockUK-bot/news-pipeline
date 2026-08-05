"""A4 late-pass unit tests (v0.12.13; pacing v0.12.15) — DB-free: the
run-window guard and the paced forwarding budget. The late pass exists
because signal.overnight was drained exactly once per day at 07:00 ET,
leaving 07:00->09:30 news unread until the next morning (the 2026-08-04
PLTR miss). Pacing exists because the original first-come daily budget of
10 was consumed entirely by the first pass on the first busy earnings
morning (2026-08-05), dropping ~140 later signals unmeasured."""
import math
from datetime import datetime, timedelta, timezone

from a4_premarket.late import (open_ts_from_entry, passes_remaining,
                               plan_forwarding, should_run)
from a4_premarket.service import next_entry_ts


def _c(msg_id, priority):
    return {"item_id": f"n:{msg_id}", "revision": 1, "msg_id": msg_id,
            "priority": priority, "headline": "h", "tickers": ["TST"]}


# --- run-window guard ------------------------------------------------------

def test_no_run_before_sheet_exists():
    now = datetime(2026, 7, 20, 11, 20, tzinfo=timezone.utc)   # 07:20 ET Mon
    open_ts = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    assert should_run(now, sheet_done=False, open_ts=open_ts) is False


def test_runs_between_sheet_and_open():
    now = datetime(2026, 7, 20, 11, 46, tzinfo=timezone.utc)   # 07:46 ET —
    open_ts = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)  # the PLTR
    assert should_run(now, sheet_done=True, open_ts=open_ts) is True  # window


def test_no_run_after_open():
    now = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)   # 09:30 ET
    open_ts = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    assert should_run(now, sheet_done=True, open_ts=open_ts) is False


def test_no_run_in_the_evening_toward_tomorrows_open():
    # Mon 20:00 ET: today's sheet exists, next open is TUESDAY — a manual
    # start must no-op rather than drain tonight's queue past the ranking.
    now = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)     # Mon 20:00 ET
    tue_open = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    assert should_run(now, sheet_done=True, open_ts=tue_open) is False


def test_open_ts_recovered_from_entry_ts():
    now = datetime(2026, 7, 20, 11, 46, tzinfo=timezone.utc)   # 07:46 ET Mon
    entry = next_entry_ts(now, blackout_min=15)                # 09:45 ET
    open_ts = open_ts_from_entry(entry, blackout_min=15)       # 09:30 ET
    assert open_ts == datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    assert entry - open_ts == timedelta(minutes=15)


# --- passes_remaining ------------------------------------------------------

def test_passes_remaining_first_pass_of_the_morning():
    # 07:10 ET with a 09:30 open: 140 minutes -> this pass + 14 more
    now = datetime(2026, 8, 5, 11, 10, tzinfo=timezone.utc)
    open_ts = datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc)
    assert passes_remaining(now, open_ts) == 15


def test_passes_remaining_last_pass_before_open():
    now = datetime(2026, 8, 5, 13, 20, tzinfo=timezone.utc)    # 09:20 ET
    open_ts = datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc)
    assert passes_remaining(now, open_ts) == 2
    now = datetime(2026, 8, 5, 13, 29, tzinfo=timezone.utc)    # 09:29 ET
    assert passes_remaining(now, open_ts) == 1


def test_passes_remaining_never_below_one():
    now = datetime(2026, 8, 5, 13, 31, tzinfo=timezone.utc)
    open_ts = datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc)
    assert passes_remaining(now, open_ts) == 1


# --- paced forwarding budget ----------------------------------------------

def test_flood_first_pass_is_paced_not_drained():
    # The 2026-08-05 failure shape: a big first-pass cohort must NOT be able
    # to consume the whole daily budget.
    cands = [_c(i, 30 + i) for i in range(1, 41)]              # 40 candidates
    fwd, rest, allowance = plan_forwarding(
        cands, already_forwarded=0, late_daily_max=24,
        late_pass_max=4, remaining_passes=15)
    assert allowance == 2                                      # ceil(24/15)
    assert [c["msg_id"] for c in fwd] == [1, 2]                # best priority
    assert len(rest) == 38


def test_allowance_grows_as_open_approaches():
    cands = [_c(i, 40 + i) for i in range(1, 9)]
    fwd, _, allowance = plan_forwarding(
        cands, already_forwarded=18, late_daily_max=24,
        late_pass_max=4, remaining_passes=2)                   # ceil(6/2)=3
    assert allowance == 3 and len(fwd) == 3


def test_per_pass_hard_cap_applies_on_the_final_pass():
    # Quiet morning, whole budget intact, one pass left: still at most
    # late_pass_max in a single pass.
    cands = [_c(i, 40 + i) for i in range(1, 31)]
    fwd, rest, allowance = plan_forwarding(
        cands, already_forwarded=0, late_daily_max=24,
        late_pass_max=4, remaining_passes=1)
    assert allowance == 4 and len(fwd) == 4 and len(rest) == 26


def test_budget_accounts_for_earlier_passes():
    cands = [_c(1, 42), _c(2, 43)]
    fwd, rest, allowance = plan_forwarding(
        cands, already_forwarded=23, late_daily_max=24,
        late_pass_max=4, remaining_passes=1)
    assert allowance == 1
    assert len(fwd) == 1 and fwd[0]["msg_id"] == 1
    assert len(rest) == 1


def test_budget_exhausted_forwards_nothing():
    cands = [_c(1, 42)]
    fwd, rest, allowance = plan_forwarding(
        cands, already_forwarded=24, late_daily_max=24,
        late_pass_max=4, remaining_passes=3)
    assert allowance == 0 and fwd == [] and len(rest) == 1


def test_orders_by_priority_lowest_first():
    cands = [_c(1, 48), _c(2, 42), _c(3, 45), _c(4, 50)]
    fwd, rest, _ = plan_forwarding(
        cands, already_forwarded=0, late_daily_max=24,
        late_pass_max=2, remaining_passes=1)
    assert [c["msg_id"] for c in fwd] == [2, 3]                # best (lowest)
    assert [c["msg_id"] for c in rest] == [1, 4]


def test_priority_tie_breaks_on_msg_id_fifo():
    cands = [_c(7, 45), _c(3, 45)]
    fwd, _, _ = plan_forwarding(
        cands, already_forwarded=0, late_daily_max=24,
        late_pass_max=1, remaining_passes=1)
    assert fwd[0]["msg_id"] == 3


def test_pacing_reaches_the_open_with_budget_on_a_flood_morning():
    # Simulate a maximal flood: every one of 15 passes has 40 candidates.
    # The invariant that fixes 2026-08-05: every pass down to the last one
    # gets allowance >= 1, and total forwarded == the daily budget exactly.
    daily, pass_max = 24, 4
    forwarded = 0
    for passes_left in range(15, 0, -1):
        cands = [_c(i, 30 + i) for i in range(1, 41)]
        fwd, _, allowance = plan_forwarding(
            cands, already_forwarded=forwarded, late_daily_max=daily,
            late_pass_max=pass_max, remaining_passes=passes_left)
        assert allowance >= 1, f"pass with {passes_left} left got starved"
        assert allowance == min(pass_max,
                                math.ceil((daily - forwarded) / passes_left))
        forwarded += len(fwd)
    assert forwarded == daily
