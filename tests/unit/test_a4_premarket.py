"""A4 unit tests — DB-free: sheet contract, deterministic fallback, entry
timing (holiday-aware open handoff), briefing renderer."""
import json
from datetime import datetime, timezone

import pytest

from a4_premarket.render import render_briefing, subject_line
from a4_premarket.schema import (SheetValidationError, sheet_json_schema,
                                 validate_sheet)
from a4_premarket.service import fallback_sheet, next_entry_ts


def _sheet(**over):
    base = {"items": [{"item_id": "n:1", "lane": "open_candidate", "rank": 1,
                       "rationale": "corroborated tier-2 M&A"}],
            "summary": "One actionable overnight story."}
    base.update(over)
    return json.dumps(base)


# --- contract --------------------------------------------------------------

def test_sheet_valid():
    s = validate_sheet(_sheet())
    assert s.items[0].lane == "open_candidate"


def test_sheet_rejects_unknown_lane_and_bad_rank():
    with pytest.raises(SheetValidationError):
        validate_sheet(_sheet(items=[{"item_id": "n:1", "lane": "short",
                                      "rank": 1, "rationale": "x"}]))
    with pytest.raises(SheetValidationError):
        validate_sheet(_sheet(items=[{"item_id": "n:1",
                                      "lane": "open_candidate",
                                      "rank": 0, "rationale": "x"}]))
    with pytest.raises(SheetValidationError):
        validate_sheet("nope")


def test_sheet_schema_grammar_safe():
    s = json.dumps(sheet_json_schema())
    assert "anyOf" not in s


# --- fallback --------------------------------------------------------------

def test_fallback_sheet_orders_by_priority_position():
    cands = [{"item_id": f"n:{i}"} for i in range(8)]
    sheet = fallback_sheet(cands, open_k=3)
    lanes = [i.lane for i in sheet.items]
    assert lanes[:3] == ["open_candidate"] * 3
    assert set(lanes[3:]) == {"ignore"}
    assert [i.rank for i in sheet.items[:3]] == [1, 2, 3]
    assert "model" in sheet.summary.lower()


# --- entry timing ----------------------------------------------------------

def test_entry_ts_weekend_rolls_to_monday_open_plus_blackout():
    sat = datetime(2026, 7, 18, 22, 0, tzinfo=timezone.utc)   # Saturday
    entry = next_entry_ts(sat, blackout_min=15)
    # Monday 2026-07-20, 09:30 EDT open + 15 = 09:45 EDT = 13:45 UTC
    assert entry == datetime(2026, 7, 20, 13, 45, tzinfo=timezone.utc)


def test_entry_ts_premarket_uses_todays_open():
    mon_7am_et = datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc)  # 07:00 ET
    entry = next_entry_ts(mon_7am_et, blackout_min=15)
    assert entry == datetime(2026, 7, 20, 13, 45, tzinfo=timezone.utc)


def test_entry_ts_midsession_is_now():
    mon_11am_et = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    assert next_entry_ts(mon_11am_et, blackout_min=15) == mon_11am_et


# --- renderer --------------------------------------------------------------

def _stats(**over):
    base = {"session_date": "2026-07-20", "expired_bulk": 4120, "fresh": 9,
            "open_candidates": 2, "guard_routed": 1, "thesis_routed": 3,
            "ignored": 3, "slot": "heavy",
            "entry_ts": "2026-07-20T13:45:00+00:00"}
    base.update(over)
    return base


def test_briefing_renders_all_sections():
    body = render_briefing(
        "2026-07-20", "Busy weekend for defense names.",
        [{"rank": 1, "tickers": ["ACME"], "headline": "Acme wins contract",
          "rationale": "tier-1 filing, uncorroborated but structural"}],
        [{"tickers": ["HELD"], "headline": "Held name downgraded"}],
        [{"headline": "Sector regulation proposal"}],
        _stats(), "heavy")
    assert "Busy weekend for defense names." in body
    assert "#1 ACME — Acme wins contract" in body
    assert "08:45 CT" in body                       # 13:45Z rendered central
    assert "HELD — Held name downgraded" in body
    assert "Sector regulation proposal" in body
    assert "4120 stale" in body
    assert "open-handoff confirmation gate is unchanged" in body


def test_briefing_subjects():
    assert "quiet night" in subject_line("2026-07-20",
                                         _stats(open_candidates=0))
    s = subject_line("2026-07-20", _stats(open_candidates=2))
    assert "2 open candidates" in s and "08:45 CT" in s


def test_briefing_marks_fallback():
    body = render_briefing("2026-07-20", "s", [], [], [], _stats(), "fallback")
    assert "model offline" in body
