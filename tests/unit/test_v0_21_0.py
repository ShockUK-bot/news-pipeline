"""v0.21.0: A12 gated auto execution and the SIC gap fill."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from a12_guard.auto import arm_block, auto_execute_gate
from a12_guard.schema import GuardVerdict
from c4_exec import engine as eng
from common.sectors import name_sector_heuristic, parse_submissions

ROOT = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")
CFG = {"enabled": True, "actions": ["exit"], "urgencies": ["high"], "require_watch_hit_or_correction": True}


def v(action="exit", urgency="high", hits=None, intact=False):
    return GuardVerdict(thesis_intact=intact, recommended_action=action, urgency=urgency,
                        confidence=0.9, watch_hits=hits or [], reason="x")


def test_gate_admits_only_the_measured_subset():
    assert auto_execute_gate(v(hits=["Analyst upgrades citing strong guidance"]), {}, CFG)[0] is True
    assert auto_execute_gate(v(), {"is_correction": True}, CFG) == (True, "correction of the entry story")
    assert auto_execute_gate(v(), {"is_correction": False}, CFG)[0] is False      # plain high exit: advisory
    assert auto_execute_gate(v(urgency="medium", hits=["x"]), {}, CFG)[0] is False
    assert auto_execute_gate(v(action="tighten_stop", hits=["x"]), {}, CFG)[0] is False
    assert auto_execute_gate(v(action="hold", intact=True), {"is_correction": True}, CFG)[0] is False
    assert auto_execute_gate(v(hits=["x"]), {}, {**CFG, "enabled": False}) == (False, "auto_execute disabled")
    assert auto_execute_gate(v(hits=["x"]), {}, None)[0] is False
    b = arm_block(42, "watch-list hit: x", "2026-09-18T14:00:00+00:00")
    assert b["decision_id"] == 42 and b["armed_by"] == "A12"


def _run_guard_pass(monkeypatch, positions):
    calls = []

    async def _open_positions():
        return positions

    async def _execute_exit(broker, pos, qty, layer, reason, px, now_fn, *a, **kw):
        calls.append((pos["ticker"], qty, layer, reason, round(px, 2)))
        return "FILLED"
    monkeypatch.setattr(eng, "open_positions", _open_positions)
    monkeypatch.setattr(eng, "execute_exit", _execute_exit)
    e = eng.PositionEngine.__new__(eng.PositionEngine)
    e.broker = object(); e.monitors = {}; e.unprotected_max_secs = 1; e.poll_sleep = 0
    e.now_fn = lambda: datetime(2026, 9, 18, 10, 5, tzinfo=ET).astimezone(timezone.utc)
    return asyncio.run(e.guard_exit_pass()), calls


def test_guard_exit_pass_sells_armed_only(monkeypatch):
    armed = {"position_id": 60, "ticker": "NTNX", "side": "SHORT", "qty_open": 107, "avg_entry": 60.0,
             "last_price": 61.0, "exit_policy": {"guard_exit": arm_block(1, "watch-list hit: upgrade", "t")}}
    quiet = {"position_id": 61, "ticker": "FRMI", "side": "LONG", "qty_open": 197, "avg_entry": 4.66,
             "last_price": 4.95, "exit_policy": {"current_stop": 3.42}}
    out, calls = _run_guard_pass(monkeypatch, [armed, quiet])
    assert out == ["NTNX:FILLED"]
    (ticker, qty, layer, reason, px), = calls
    assert ticker == "NTNX" and qty == 107 and layer == "GUARD" and "watch-list hit" in reason
    assert px > 61.0                                   # a short is bought back over the mark


def test_service_wiring_and_yaml():
    svc = (ROOT / "src" / "a12_guard" / "service.py").read_text()
    assert "auto_execute_gate(verdict, item, self.cfg.get(\"auto_execute\"))" in svc
    assert "EXIT_ARMED" in svc and "'GUARD_ACTION'" in svc
    c4 = (ROOT / "src" / "c4_exec" / "service.py").read_text()
    assert "engine.guard_exit_pass()" in c4 and c4.index("open_exit_pass") < c4.index("guard_exit_pass")
    a12 = yaml.safe_load((ROOT / "config" / "a12.yaml").read_text())["auto_execute"]
    assert a12["enabled"] is True and a12["actions"] == ["exit"] and a12["urgencies"] == ["high"]
    assert a12["require_watch_hit_or_correction"] is True


def test_new_sic_ranges():
    from common.sectors import sector_for_sic
    assert sector_for_sic(5160) == "Materials"                  # chemicals wholesale (ASH)
    assert sector_for_sic(700) == "Consumer Staples"            # agricultural services (AVO)
    assert sector_for_sic(2510) == "Consumer Discretionary"     # household furniture (HOFT)
    assert sector_for_sic(5122) == "Consumer Staples"           # drugs wholesale
    assert sector_for_sic(3021) == "Consumer Discretionary"     # rubber and plastics footwear
    assert sector_for_sic(8200) == "Consumer Discretionary"     # education services
    assert sector_for_sic(3674) == "Information Technology" and sector_for_sic(3533) == "Energy"


def test_sic_gap_heuristic():
    assert name_sector_heuristic("ARES CAPITAL CORP") == "Financials"
    assert name_sector_heuristic("abrdn Income Credit Strategies Fund") == "Financials"
    assert name_sector_heuristic("Churchill Capital Acquisition Corp IX") == "Financials"
    assert name_sector_heuristic("NVIDIA CORP") is None and name_sector_heuristic(None) is None
    rec = parse_submissions({"cik": "1287750", "sic": "", "sicDescription": None, "name": "ARES CAPITAL CORP"})
    assert rec["sector"] == "Financials" and rec["source"] == "name_heuristic" and rec["sic"] is None
    rec = parse_submissions({"cik": "1", "sic": "3674", "sicDescription": "Semis", "name": "X Fund"})
    assert rec["sector"] == "Information Technology" and rec["source"] == "edgar_submissions"
