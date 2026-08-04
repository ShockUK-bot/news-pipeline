"""A4 late-pass unit tests (v0.12.13) — DB-free: the run-window guard and the
daily forwarding budget. The late pass exists because signal.overnight was
drained exactly once per day at 07:00 ET, leaving 07:00->09:30 news unread
until the next morning (the 2026-08-04 PLTR miss)."""
from datetime import datetime, timedelta, timezone

from a4_premarket.late import (open_ts_from_entry, plan_forwarding,
                               should_run)
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


# --- forwarding budget -----------------------------------------------------

def test_budget_orders_by_priority_and_caps():
    cands = [_c(1, 48), _c(2, 42), _c(3, 45), _c(4, 50)]
    fwd, over = plan_forwarding(cands, already_forwarded=0, late_daily_max=2)
    assert [c["msg_id"] for c in fwd] == [2, 3]        # best (lowest) first
    assert [c["msg_id"] for c in over] == [1, 4]


def test_budget_accounts_for_earlier_passes():
    cands = [_c(1, 42), _c(2, 43)]
    fwd, over = plan_forwarding(cands, already_forwarded=9, late_daily_max=10)
    assert len(fwd) == 1 and fwd[0]["msg_id"] == 1
    assert len(over) == 1


def test_budget_exhausted_forwards_nothing():
    cands = [_c(1, 42)]
    fwd, over = plan_forwarding(cands, already_forwarded=10, late_daily_max=10)
    assert fwd == [] and len(over) == 1


def test_priority_tie_breaks_on_msg_id_fifo():
    cands = [_c(7, 45), _c(3, 45)]
    fwd, _ = plan_forwarding(cands, already_forwarded=0, late_daily_max=1)
    assert fwd[0]["msg_id"] == 3
