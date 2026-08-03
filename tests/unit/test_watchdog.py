"""v0.12.7 unit tests — DB-free: systemctl parsing, the finding matrix
(down / disabled / failed-last-run / stale heartbeat / rth_only gating /
not-found), fingerprint stability, alert-decision transitions
(NEW -> quiet -> REPEAT -> RECOVERED), and email rendering."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from c7_watchdog.service import (evaluate, fingerprint, gather_units, render,
                                 should_alert, systemctl_show)

NOW = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)   # Monday, in RTH


def _cfg(**over):
    base = {
        "realert_hours": 6,
        "services": {
            "c4-exec": {"health": "exec", "desc": "execution engine"},
            "c1-ingestion": {"health": "ingestion"},
        },
        "timers": ["pipeline-nav-snapshot"],
        "heartbeats": {
            "exec": {"max_age_min": 10},
            "marketdata": {"max_age_min": 15, "rth_only": True},
        },
    }
    base.update(over)
    return base


def _healthy_units():
    ok = {"LoadState": "loaded", "ActiveState": "active",
          "UnitFileState": "enabled", "Result": "success",
          "InactiveEnterTimestamp": ""}
    return {
        "c4-exec.service": dict(ok),
        "c1-ingestion.service": dict(ok),
        "pipeline-nav-snapshot.timer": dict(ok),
        "pipeline-nav-snapshot.service": {"LoadState": "loaded",
                                          "ActiveState": "inactive",
                                          "UnitFileState": "static",
                                          "Result": "success",
                                          "InactiveEnterTimestamp": "x"},
    }


# ---------------------------------------------------------------- systemctl

def test_systemctl_show_parses_and_survives_errors():
    def fake_run(cmd, **kw):
        return SimpleNamespace(stdout="ActiveState=active\nUnitFileState=enabled\n")
    assert systemctl_show("c4-exec.service", fake_run)["ActiveState"] == "active"

    def boom(cmd, **kw):
        raise OSError("no systemctl here")
    assert systemctl_show("c4-exec.service", boom) == {}


def test_gather_units_probes_service_and_timer_pairs():
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd[2])
        return SimpleNamespace(stdout="ActiveState=active\n")
    gather_units(_cfg(), fake_run)
    assert "c4-exec.service" in seen
    assert "pipeline-nav-snapshot.timer" in seen
    assert "pipeline-nav-snapshot.service" in seen


# ------------------------------------------------------------ finding matrix

def test_all_healthy_no_findings():
    assert evaluate(_cfg(), _healthy_units(), {"exec": 1.0}, True) == []


def test_dead_service_is_critical_with_since():
    units = _healthy_units()
    units["c4-exec.service"] = {"LoadState": "loaded", "ActiveState": "inactive",
                                "UnitFileState": "disabled", "Result": "success",
                                "InactiveEnterTimestamp": "Tue 2026-07-28 03:04:36 CDT"}
    f = evaluate(_cfg(), units, {}, True)
    codes = {(x["code"], x["severity"]) for x in f}
    assert ("SERVICE_DOWN", "CRITICAL") in codes
    assert ("NOT_ENABLED", "WARNING") in codes       # the 07-28 failure mode
    down = next(x for x in f if x["code"] == "SERVICE_DOWN")
    assert "2026-07-28 03:04:36" in down["detail"]
    assert "execution engine" in down["detail"]


def test_transient_states_produce_no_findings():
    # v0.12.10 — the v0.12.9 deploy restart raced a scheduled pass:
    # 'deactivating' was reported as CRITICAL SERVICE_DOWN and emailed a
    # false alert + RECOVERED pair. Mid-transition states are 'recheck next
    # pass', not findings; a genuinely dead unit shows inactive/failed 5
    # minutes later and still alerts.
    for state in ("activating", "deactivating", "reloading", "refreshing"):
        units = _healthy_units()
        units["c4-exec.service"]["ActiveState"] = state
        assert evaluate(_cfg(), units, {"exec": 1.0}, True) == [], state


def test_transient_timer_produces_no_timer_down():
    units = _healthy_units()
    units["pipeline-nav-snapshot.timer"]["ActiveState"] = "activating"
    assert "TIMER_DOWN" not in [x["code"] for x in
                                evaluate(_cfg(), units, {"exec": 1.0}, True)]


def test_stuck_service_still_caught_once_it_lands():
    # the pass AFTER a transition: failed is not transient — still CRITICAL.
    units = _healthy_units()
    units["c4-exec.service"]["ActiveState"] = "failed"
    codes = {(x["code"], x["severity"])
             for x in evaluate(_cfg(), units, {"exec": 1.0}, True)}
    assert ("SERVICE_DOWN", "CRITICAL") in codes


def test_active_but_disabled_is_warning_only():
    units = _healthy_units()
    units["c1-ingestion.service"]["UnitFileState"] = "disabled"
    f = evaluate(_cfg(), units, {}, True)
    assert [x["code"] for x in f] == ["NOT_ENABLED"]
    assert f[0]["severity"] == "WARNING"
    assert "systemctl enable c1-ingestion" in f[0]["detail"]


def test_failed_oneshot_last_run_is_critical():
    units = _healthy_units()
    units["pipeline-nav-snapshot.service"]["Result"] = "exit-code"
    f = evaluate(_cfg(), units, {}, True)
    assert [x["code"] for x in f] == ["LAST_RUN_FAILED"]
    assert f[0]["severity"] == "CRITICAL"


def test_inactive_timer_is_critical():
    units = _healthy_units()
    units["pipeline-nav-snapshot.timer"]["ActiveState"] = "inactive"
    f = evaluate(_cfg(), units, {}, True)
    assert "TIMER_DOWN" in [x["code"] for x in f]


def test_unit_not_found_is_warning_not_crash():
    units = _healthy_units()
    units["c1-ingestion.service"] = {"LoadState": "not-found"}
    f = evaluate(_cfg(), units, {}, True)
    assert [x["code"] for x in f] == ["UNIT_NOT_FOUND"]


def test_stale_heartbeat_fires_and_fresh_does_not():
    f = evaluate(_cfg(), _healthy_units(), {"exec": 45.0}, True)
    assert [x["code"] for x in f] == ["HEARTBEAT_STALE"]
    assert evaluate(_cfg(), _healthy_units(), {"exec": 9.9}, True) == []


def test_rth_only_heartbeat_skipped_off_hours():
    ages = {"exec": 1.0, "marketdata": 600.0}
    assert evaluate(_cfg(), _healthy_units(), ages, False) == []
    assert [x["unit"] for x in
            evaluate(_cfg(), _healthy_units(), ages, True)] == ["marketdata"]


def test_missing_heartbeat_row_is_not_a_finding():
    assert evaluate(_cfg(), _healthy_units(), {}, True) == []


# --------------------------------------------------- fingerprint + decisions

def _f(code="SERVICE_DOWN", unit="c4-exec", detail="x"):
    return {"severity": "CRITICAL", "code": code, "unit": unit,
            "detail": detail}


def test_fingerprint_ignores_detail_churn_but_not_membership():
    a = fingerprint([_f(detail="stale 45min")])
    b = fingerprint([_f(detail="stale 50min")])
    assert a == b
    assert fingerprint([_f(), _f(unit="c1-ingestion")]) != a
    assert fingerprint([]) == ""


def test_alert_lifecycle_new_quiet_repeat_recovered():
    findings = [_f()]
    fp = fingerprint(findings)
    assert should_alert(findings, "", None, NOW, 6) == "NEW"
    just_sent = NOW - timedelta(minutes=10)
    assert should_alert(findings, fp, just_sent, NOW, 6) is None
    long_ago = NOW - timedelta(hours=7)
    assert should_alert(findings, fp, long_ago, NOW, 6) == "REPEAT"
    assert should_alert([], fp, long_ago, NOW, 6) == "RECOVERED"
    assert should_alert([], "", None, NOW, 6) is None       # steady healthy


def test_changed_finding_set_realerts_immediately():
    old_fp = fingerprint([_f()])
    now_findings = [_f(), _f(unit="c1-ingestion")]
    assert should_alert(now_findings, old_fp, NOW, NOW, 6) == "NEW"


# ------------------------------------------------------------------ render

def test_render_orders_critical_first_and_names_worst():
    findings = [{"severity": "WARNING", "code": "NOT_ENABLED",
                 "unit": "c6-dashboard", "detail": "d"},
                _f(unit="c4-exec", detail="inactive since 03:04")]
    subject, body = render(findings, "NEW", 6)
    assert "1 critical / 1 warning" in subject
    assert "worst: c4-exec" in subject
    assert body.index("CRITICAL") < body.index("WARNING")
    assert "inactive since 03:04" in body


def test_render_repeat_and_recovery():
    subject, _ = render([_f()], "REPEAT", 6)
    assert subject.startswith("[watchdog] REPEAT:")
    subject, body = render([], "RECOVERED", 6)
    assert "RECOVERED" in subject
    assert "No action needed" in body
