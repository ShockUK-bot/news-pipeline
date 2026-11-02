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
