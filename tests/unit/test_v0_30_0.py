"""v0.30.0 (2026-11-02): deterministic analyst pre-check. Unmoved news-lane
signals in the regular session are deferred, then rejected, without a model
call; scanner signals and off-session news are untouched."""
from pathlib import Path

import yaml

from a2_analyst.precheck import precheck
from a2_analyst.service import DeferAnalysis

ROOT = Path(__file__).resolve().parents[2]
CFG = yaml.safe_load((ROOT / "config" / "a2.yaml").read_text())["precheck"]


def _ctx(pct, minutes):
    return {"price_action": {"pct_move_since_news": pct, "minutes_since_news": minutes}}


def test_config():
    assert CFG["enabled"] is True and CFG["rth_only"] is True
    assert CFG["min_move_pct"] == 0.005 and CFG["wait_max_min"] == 60 and CFG["defer_secs"] == 300


def test_precheck_defer_then_reject():
    v = precheck(_ctx(0.001, 12), "news", True, CFG)
    assert v.action == "DEFER" and v.delay_secs == 300 and "no model call" in v.reason
    v = precheck(_ctx(-0.002, 60), "news", True, CFG)
    assert v.action == "REJECT" and "pre-check" in v.reason and v.minutes == 60
    assert precheck(_ctx(0.0, 240), "news", True, CFG).action == "REJECT"


def test_precheck_passes_through():
    assert precheck(_ctx(0.012, 5), "news", True, CFG) is None          # moved: run the model
    assert precheck(_ctx(-0.006, 5), "news", True, CFG) is None
    assert precheck(_ctx(0.0, 30), "scanner", True, CFG) is None        # scanner never pre-checked
    assert precheck(_ctx(0.0, 30), "news", False, CFG) is None          # pre-market: today's path
    assert precheck(_ctx(None, 30), "news", True, CFG) is None          # unknown is not unmoved
    assert precheck(_ctx(0.0, None), "news", True, CFG) is None
    assert precheck(_ctx(0.0, 30), "news", True, {"enabled": False}) is None
    assert precheck(_ctx(0.0, 30), "news", True, None) is None
    assert precheck({}, "news", True, CFG) is None


def test_defer_exception():
    d = DeferAnalysis(300)
    assert d.delay_secs == 300.0 and isinstance(d, Exception)


def test_wiring():
    svc = (ROOT / "src" / "a2_analyst" / "service.py").read_text()
    assert "pv = precheck(context, origin, is_market_hours(utcnow()), self.precheck_cfg)" in svc
    assert "raise DeferAnalysis(pv.delay_secs)" in svc
    assert "except DeferAnalysis as d:" in svc and "await defer(msg.msg_id, d.delay_secs)" in svc
    assert '"precheck": True' in svc
    assert svc.index("pv = precheck(") < svc.index("reply = await self.backend.complete(messages, schema)")


def test_a9_approve_registers_config_version():
    """v0.30.1: --approve / --reject must register the config version before journaling."""
    a9 = (ROOT / "src" / "a9_review" / "service.py").read_text()
    body = a9[a9.index("async def set_status"):a9.index("def main()")]
    assert "await register_config_version(" in body
    assert body.index("await register_config_version(") < body.index("write_decision(")


def test_replay_breakeven_variants():
    """v0.30.2: be1.5 and nobe variants exist and change the breakeven ratchet."""
    import sys
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    sys.path.insert(0, str(ROOT / "ops" / "tools"))
    import scalp_replay as sr
    CT = ZoneInfo("America/Chicago")
    assert "be1.5" in sr.VARIANTS and "nobe" in sr.VARIANTS
    t0 = datetime(2026, 11, 5, 9, 0, tzinfo=CT)
    def bar(i, o, h, l, c):
        return {"ts": t0 + timedelta(minutes=i), "open": o, "high": h, "low": l, "close": c}
    # short from 100, atr 1, stop 2.0 -> r_unit 2; drops to 98.3 (0.85R: past breakeven 0.75R, short of
    # the 1R trail), bounces through entry, then falls to 94
    bars = [bar(0, 100, 100.2, 98.3, 98.3), bar(1, 98.3, 100.1, 98.2, 99.5), bar(2, 99.5, 99.6, 94.0, 94.2)]
    base = sr.replay(bars, "SHORT", t0, 100.0, 100, 1.0, 0.015, 2.0, time_stop=False, scale_out=False)
    nobe = sr.replay(bars, "SHORT", t0, 100.0, 100, 1.0, 0.015, 2.0, time_stop=False, scale_out=False, be_r=99.0)
    assert base["exits"][0][1] == "BREAKEVEN" and base["pnl"] == 0.0
    assert nobe["pnl"] > 400 and nobe["exits"][-1][1] == "EOD_MARK"
