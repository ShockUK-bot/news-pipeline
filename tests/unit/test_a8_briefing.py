"""A8 unit tests — DB-free: narrative contract + grammar-safety, subject
construction, renderer determinism across present/missing sections."""
import json

import pytest

from a8_briefing.narrative import (BriefingNarrative,
                                   NarrativeValidationError,
                                   narrative_json_schema, validate_narrative)
from a8_briefing.render import render, subject_line


def _facts(**over):
    base = {
        "session_date": "2026-07-20",
        "a4": {"session_date": "2026-07-20", "fresh": 12,
               "open_candidates": 2, "guard_routed": 1, "thesis_routed": 3,
               "ignored": 6, "expired_bulk": 40,
               "entry_ts": "2026-07-20T13:45:00+00:00", "slot": "heavy",
               "summary": "Busy overnight tape.",
               "open_forwarded": [
                   {"item_id": "n:1", "tickers": ["ACME"], "rank": 1,
                    "headline": "Acme wins supply deal"}]},
        "thesis": {"active": [
            {"thesis_id": "th-2026-001", "direction": "up",
             "confidence": 0.55, "title": "Grid capex supercycle",
             "beneficiaries": [{"ticker": "VRT"}]}],
            "digest": {"run_date": "2026-07-19", "new_theses": 1,
                       "evidence_attached": 2, "status_changes": 0}},
        "positions": [
            {"position_id": 1, "ticker": "NVDA", "horizon": "SHORT",
             "qty_open": 50, "avg_entry": 100.0, "last_price": 104.0,
             "r_progress": 1.0, "current_stop": 100.0,
             "earnings_next_sessions": 1, "blackout_soon": True}],
        "a6": {"review": {"run_date": "2026-07-17", "reviewed": 1,
                          "recommendations": 1, "stale_flagged": 0,
                          "recos": [{"position_id": 1, "ticker": "NVDA",
                                     "action": "TRIM_RECO",
                                     "rationale": "move mostly realized"}]},
               "eod": None},
        "earnings": {"reporting_today": 84,
                     "held_reporting_soon": [{"ticker": "NVDA",
                                              "report_date": "2026-07-21"}]},
        "ops": {"queues": {"signal.analyst": 0, "signal.thesis": 2},
                "health_not_ok": [], "newest_item_age_hours": 0.4},
    }
    base.update(over)
    return base


def test_narrative_valid_and_rejects():
    n = validate_narrative(json.dumps(
        {"summary": "Two candidates; NVDA reports tomorrow.",
         "watch_items": ["NVDA earnings in 1 session"]}))
    assert n.watch_items
    with pytest.raises(NarrativeValidationError):
        validate_narrative("nope")
    with pytest.raises(NarrativeValidationError):
        validate_narrative(json.dumps({"summary": ""}))
    assert "anyOf" not in json.dumps(narrative_json_schema())


def test_subject_counts_candidates_recos_and_blackouts():
    s = subject_line(_facts())
    assert "2 candidates" in s and "1 position reco" in s
    assert "1 earnings-window position" in s
    quiet = subject_line(_facts(a4=None, a6={"review": None, "eod": None},
                                positions=[]))
    assert quiet.startswith("Morning briefing 2026-07-20 — 0 candidates")


def test_render_full_facts():
    body = render(_facts(), BriefingNarrative(
        summary="Trim NVDA before its report.",
        watch_items=["NVDA reports in 1 session"]))
    assert "Acme wins supply deal" in body
    assert "EARNINGS in 1 session" in body
    assert "A6 recommends TRIM_RECO" in body
    assert "th-2026-001" in body and "VRT" in body
    assert "84 US names report today" in body
    assert "All health components OK" in body


def test_render_degrades_visibly_not_silently():
    body = render(_facts(a4=None,
                         thesis={"active": [], "digest": None},
                         a6={"review": None, "eod": None},
                         positions=[],
                         earnings={"reporting_today": None,
                                   "held_reporting_soon": []},
                         ops={"queues": {},
                              "health_not_ok": [
                                  {"component": "earnings",
                                   "status": "DEGRADED",
                                   "detail": "no key"}],
                              "newest_item_age_hours": 9.0}),
                  None)
    assert "not available yet this morning" in body      # A4 missing
    assert "(narrative unavailable" in body
    assert "No A6 review on record" in body
    assert "calendar unavailable" in body
    assert "HEALTH DEGRADED: earnings" in body
