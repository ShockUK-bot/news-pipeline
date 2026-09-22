"""v0.25.1: side-aware breaker P&L, guarded auto reset, heavy slot morning guard."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from c4_exec.breaker import auto_reset_decision

ROOT = Path(__file__).resolve().parents[2]
CFG = {"enabled": True, "max_trips_per_30d": 3}


def ct(y, m, d, h):
    from zoneinfo import ZoneInfo
    return datetime(y, m, d, h, 0, tzinfo=ZoneInfo("America/Chicago"))


def test_auto_reset_decision():
    trip = ct(2026, 10, 6, 10)
    assert auto_reset_decision(trip, ct(2026, 10, 6, 14), 1, CFG) is None          # same session: stays tripped
    assert auto_reset_decision(trip, ct(2026, 10, 7, 8), 1, CFG) == "RESET"       # next session, 1 trip
    assert auto_reset_decision(trip, ct(2026, 10, 7, 8), 2, CFG) == "RESET"       # 2 trips: still under the limit
    assert auto_reset_decision(trip, ct(2026, 10, 7, 8), 3, CFG) == "REFUSE"      # 3rd trip in 30 days: operator
    assert auto_reset_decision(trip, ct(2026, 10, 7, 8), 1, {"enabled": False}) is None
    assert auto_reset_decision(None, ct(2026, 10, 7, 8), 0, CFG) is None
    # a trip late in the evening (after the UTC date rolls) is still "today" in Chicago
    late = datetime(2026, 10, 7, 1, 30, tzinfo=timezone.utc)                     # 20:30 CT on the 6th
    assert auto_reset_decision(late, ct(2026, 10, 6, 21), 1, CFG) is None
    assert auto_reset_decision(late, ct(2026, 10, 7, 8), 1, CFG) == "RESET"


def test_breaker_unrealized_is_side_aware():
    src = (ROOT / "src" / "c4_exec" / "breaker.py").read_text()
    assert "CASE WHEN side='SHORT' THEN -1 ELSE 1 END" in src
    svc = (ROOT / "src" / "c4_exec" / "service.py").read_text()
    assert "maybe_auto_reset(svc.cfg.get(\"breaker_auto_reset\"), now)" in svc


def test_config_and_units():
    c4 = yaml.safe_load((ROOT / "config" / "deadman.yaml").read_text())["c4"]
    assert c4["breaker_auto_reset"]["enabled"] is True and c4["breaker_auto_reset"]["max_trips_per_30d"] == 3
    assert c4["drawdown_breaker_pct"] == 0.02
    svc = (ROOT / "ops" / "systemd" / "llama-heavy-guard.service").read_text()
    assert "systemctl stop llama-heavy.service" in svc and "is-active --quiet llama-heavy.service" in svc
    assert "08:15" in (ROOT / "ops" / "systemd" / "llama-heavy-guard.timer").read_text()
    wd = yaml.safe_load((ROOT / "config" / "watchdog.yaml").read_text())
    assert "llama-heavy-guard" in wd["timers"]
