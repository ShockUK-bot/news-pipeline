"""v0.16.1: exit at next open for dead / review-exit theses, thesis re-entry
cooling off, Gate Lab and BURST tab query shapes."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from c11_thesis.service import arm_exit_at_open, tighten_stop
from c4_exec import engine as eng

ROOT = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")


def test_arm_block_shapes():
    a = arm_exit_at_open("REVIEW", "ACTIVE", "2026-09-17T02:15:00+00:00")
    assert a["label"] == "REVIEW_EXIT" and "broken" in a["reason"] and a["armed_by"] == "C11"
    d = arm_exit_at_open("DEAD", "EXPIRED", "x")
    assert d["label"] == "DEAD_EXPIRED" and d["reason"] == "thesis EXPIRED"


def test_tighten_only_still_holds():
    pol = {"current_stop": 20.25, "initial_stop": {"price": 14.25}}
    assert tighten_stop(pol, 20.30) is None          # 20.30*0.995 = 20.20 < 20.25
    pol2, stop = tighten_stop(pol, 21.88)
    assert stop == 21.77 and pol2["current_stop"] == 21.77 and pol["current_stop"] == 20.25


def _run_open_exit(monkeypatch, positions, hhmm):
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
    h, m = int(hhmm[:2]), int(hhmm[3:])
    e.now_fn = lambda: datetime(2026, 9, 18, h, m, tzinfo=ET).astimezone(timezone.utc)
    out = asyncio.run(e.open_exit_pass("09:35"))
    return out, calls


def test_open_exit_pass_sells_armed_positions_after_0935(monkeypatch):
    riot = {"position_id": 7, "ticker": "RIOT", "side": "LONG", "qty_open": 34,
            "avg_entry": 21.43, "last_price": 21.88,
            "exit_policy": {"exit_at_open": arm_exit_at_open("REVIEW", "ACTIVE", "x"),
                            "current_stop": 20.25}}
    frmi = {"position_id": 33, "ticker": "FRMI", "side": "LONG", "qty_open": 197,
            "avg_entry": 4.66, "last_price": 4.95, "exit_policy": {"current_stop": 3.42}}
    out, calls = _run_open_exit(monkeypatch, [riot, frmi], "09:34")
    assert out == [] and calls == []                       # too early
    out, calls = _run_open_exit(monkeypatch, [riot, frmi], "09:35")
    assert out == ["RIOT:FILLED"]
    (ticker, qty, layer, reason, px), = calls
    assert ticker == "RIOT" and qty == 34 and layer == "REVIEW"
    assert "thesis broken" in reason and px < 21.88        # marketable, under the mark
    # a short is bought back OVER the mark
    short = dict(riot, side="SHORT", ticker="XYZ")
    out, calls = _run_open_exit(monkeypatch, [short], "10:00")
    assert calls[0][4] > 21.88


def test_yaml_pins():
    t = yaml.safe_load((ROOT / "config" / "thesis_entry.yaml").read_text())
    assert t["management"]["dead_exit_at_open"] is True
    assert 3 <= t["entry"]["reentry_cooloff_days"] <= 10


def test_c4_service_calls_open_exit_pass_in_session():
    src = (ROOT / "src" / "c4_exec" / "service.py").read_text()
    assert 'OPEN_EXIT_ET = "09:35"' in src
    assert "engine.open_exit_pass(OPEN_EXIT_ET)" in src
    assert src.index("open_exit_pass") < src.index("force_flat_pass()")


def test_dashboard_shapes():
    app = (ROOT / "dashboard" / "app.py").read_text()
    html = (ROOT / "dashboard" / "index.html").read_text()
    assert "WHERE rule NOT IN ('eh_shadow', 'fade')" in app
    assert "\"fade\": fade" in app and "labFade" in html
    assert "CASE WHEN direction = 'down'" in app        # EH direction-adjusted
    assert "burst" not in app.lower() and "tabBurst" not in html      # v0.25.0: C12 retired
