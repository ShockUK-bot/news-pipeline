"""v0.14.4 — the checks that would have caught the 2026-08-27 incident.

On 2026-08-27 a1-triage hung inside startup after a reboot and stayed
`active (running)` for 85 hours. Every layer that could have noticed asked
systemd, which cannot see a hung process. These tests pin the layer that
asks the service itself.

Pure functions only: no database, no systemd, no network.
"""
from __future__ import annotations

import pytest

from common.health import (claimed_components, freshness_limits, is_dynamic,
                           orphan_rows, stale_components)
from c7_watchdog.service import evaluate, fingerprint, should_alert
from a8_briefing.render import subject_line, render


CFG = {
    "realert_hours": 6,
    "services": {
        "a1-triage": {"health": "triage", "max_age_min": 10,
                      "desc": "triage router"},
        "a3-risk": {"health": "risk", "max_age_min": 10, "desc": "sizing"},
        "c10-scanner": {"health": "scanner", "desc": "windowed"},
        "c6-dashboard": {"desc": "no health row"},
    },
    "heartbeats": {
        "deadman": {"max_age_min": 10, "unit": "c4-exec"},
        "marketdata": {"max_age_min": 15, "rth_only": True},
    },
    "orphans": {"warn_after_days": 7, "delete_after_days": 30},
    "never_orphan": ["watchdog"],
}

ACTIVE = {"LoadState": "loaded", "ActiveState": "active",
          "UnitFileState": "enabled"}


def units_all_active():
    return {f"{n}.service": dict(ACTIVE) for n in CFG["services"]}


# --------------------------------------------------------------------------
# common.health
# --------------------------------------------------------------------------

def test_limits_come_from_services_and_heartbeats():
    limits = freshness_limits(CFG)
    assert limits["triage"]["max_age_min"] == 10
    assert limits["triage"]["unit"] == "a1-triage"
    assert limits["deadman"]["unit"] == "c4-exec"
    assert limits["marketdata"]["rth_only"] is True


def test_service_without_max_age_is_deliberately_unchecked():
    """c10-scanner writes only inside its scan window. Declaring no limit is
    the documented way to opt out; it must not silently default to one."""
    assert "scanner" not in freshness_limits(CFG)
    assert "dashboard" not in freshness_limits(CFG)


def test_the_incident_is_caught():
    """triage stale for 5102.3 minutes. This is the exact number from the
    2026-08-31 morning email."""
    stale = stale_components(freshness_limits(CFG), {"triage": 5102.3}, True)
    assert [s["component"] for s in stale] == ["triage"]
    assert stale[0]["unit"] == "a1-triage"


def test_fresh_and_absent_are_both_quiet():
    limits = freshness_limits(CFG)
    assert stale_components(limits, {"triage": 1.0}, True) == []
    # never written at all: absent is not stale — the unit checks own that.
    assert stale_components(limits, {}, True) == []


def test_worst_first():
    stale = stale_components(freshness_limits(CFG),
                             {"triage": 30.0, "risk": 5000.0,
                              "deadman": 99.0}, True)
    assert [s["component"] for s in stale] == ["risk", "deadman", "triage"]


def test_rth_only_is_quiet_off_hours():
    limits = freshness_limits(CFG)
    assert stale_components(limits, {"marketdata": 900.0}, False) == []
    assert len(stale_components(limits, {"marketdata": 900.0}, True)) == 1


def test_dynamic_rows():
    assert is_dynamic("ingestion:alpaca")
    assert not is_dynamic("triage")


def test_claimed_components():
    claimed = claimed_components(CFG)
    assert {"triage", "risk", "scanner", "deadman", "watchdog"} <= claimed
    assert "ingestion:testsource" not in claimed


# --------------------------------------------------------------------------
# C7 evaluate()
# --------------------------------------------------------------------------

def test_evaluate_flags_a_running_but_wedged_service():
    """The whole point: the unit is active and enabled, and it is still a
    CRITICAL finding, because its heartbeat stopped."""
    findings = evaluate(CFG, units_all_active(), {"triage": 5102.3}, True)
    stale = [f for f in findings if f["code"] == "HEARTBEAT_STALE"]
    assert len(stale) == 1
    assert stale[0]["severity"] == "CRITICAL"
    assert stale[0]["unit"] == "triage"
    assert "85.0 hours" in stale[0]["detail"]
    assert "systemctl restart a1-triage" in stale[0]["detail"]


def test_evaluate_silent_when_everything_is_fresh():
    assert evaluate(CFG, units_all_active(),
                    {"triage": 1.0, "risk": 2.0, "deadman": 0.5}, True) == []


def test_orphan_row_warned_not_criticalled():
    """A dead component's row lying at OK is a WARNING, never a CRITICAL —
    it is untidiness, not an outage."""
    findings = evaluate(CFG, units_all_active(), {"legacy_thing": 14400.0},
                        True)
    assert [f["code"] for f in findings] == ["ORPHAN_HEALTH_ROW"]
    assert findings[0]["severity"] == "WARNING"
    assert "10.0 days" in findings[0]["detail"]


def test_the_47_day_test_row_is_deleted_not_nagged_about():
    """`ingestion:testsource` — the row that sat at OK for 47.8 days. It is
    dynamic, so it is never warned about; it is ancient, so it is deleted.
    A row that lies is worse than no row."""
    ages = {"ingestion:testsource": 68786.0}
    to_warn, to_delete = orphan_rows(CFG, ages, 7, 30)
    assert to_warn == []
    assert to_delete == ["ingestion:testsource"]
    assert evaluate(CFG, units_all_active(), ages, True) == []


def test_a_live_websocket_row_is_left_completely_alone():
    """Connected for two weeks means a two-week-old row and perfect health.
    GapMonitor writes those on gap open/close only."""
    ages = {"ingestion:alpaca": 20000.0}          # 13.9 days
    assert orphan_rows(CFG, ages, 7, 30) == ([], [])
    assert evaluate(CFG, units_all_active(), ages, True) == []


def test_warning_precedes_deletion_and_they_never_overlap():
    ages = {"legacy_thing": 14400.0, "ancient_thing": 50000.0}
    to_warn, to_delete = orphan_rows(CFG, ages, 7, 30)
    assert to_warn == ["legacy_thing"]            # 10 days: warn
    assert to_delete == ["ancient_thing"]         # 34.7 days: delete
    assert not set(to_warn) & set(to_delete)


def test_watchdogs_own_row_is_never_an_orphan():
    assert evaluate(CFG, units_all_active(), {"watchdog": 999999.0}, True) == []


def test_service_down_still_reported():
    units = units_all_active()
    units["a1-triage.service"] = {"LoadState": "loaded",
                                  "ActiveState": "failed",
                                  "UnitFileState": "enabled"}
    codes = [f["code"] for f in evaluate(CFG, units, {}, True)]
    assert "SERVICE_DOWN" in codes


def test_alert_fires_once_then_repeats_on_schedule():
    from datetime import datetime, timedelta, timezone
    findings = evaluate(CFG, units_all_active(), {"triage": 5102.3}, True)
    fp = fingerprint(findings)
    t0 = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    assert should_alert(findings, "", None, t0, 6) == "NEW"
    assert should_alert(findings, fp, t0, t0 + timedelta(hours=1), 6) is None
    assert should_alert(findings, fp, t0, t0 + timedelta(hours=7), 6) == "REPEAT"
    assert should_alert([], fp, t0, t0 + timedelta(hours=7), 6) == "RECOVERED"


# --------------------------------------------------------------------------
# A8 briefing — the email a human actually reads
# --------------------------------------------------------------------------

def _facts(outages):
    return {"session_date": "2026-08-31", "a4": {"open_candidates": 3},
            "positions": [], "ops": {"queues": {}, "health_not_ok": [],
                                     "outages": outages,
                                     "newest_item_age_hours": 0.2}}


OUTAGE = [{"component": "triage", "age_min": 5102.3, "max_age_min": 10,
           "unit": "a1-triage", "desc": "triage router"}]


def test_outage_reaches_the_subject_line():
    subject = subject_line(_facts(OUTAGE))
    assert subject.startswith("[OUTAGE] triage stale 3.5 days")


def test_quiet_morning_keeps_the_normal_subject():
    assert subject_line(_facts([])).startswith("Morning briefing 2026-08-31")


def test_outage_banner_is_above_the_narrative():
    body = render(_facts(OUTAGE), None)
    assert "PIPELINE OUTAGE" in body
    assert body.index("PIPELINE OUTAGE") < body.index("SYSTEM:")
    assert "systemctl restart a1-triage" in body


def test_healthy_morning_has_no_banner():
    assert "PIPELINE OUTAGE" not in render(_facts([]), None)


@pytest.mark.parametrize("minutes,expected", [
    (5.0, "5 min"), (179.0, "179 min"), (181.0, "3.0 hours"),
    (5102.3, "3.5 days"),
])
def test_span_units(minutes, expected):
    from a8_briefing.render import _span
    assert _span(minutes) == expected
