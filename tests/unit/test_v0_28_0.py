"""v0.28.0 (2026-11-02): catalyst shadow lanes. C10 journals EARNINGS_CONTINUATION
and GAP_FADE candidates at 09:45 to 10:05 ET as scanner_candidates SHADOW rows;
A11 replays them with a fixed percent stop held to the close; A9 holds the
go-live bar. No order path."""
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from a11_metrics.funnel import classify
from a9_review.candidates import generate, rule_earnings_lane, rule_gap_fade_lane
from c10_scanner.rules import CandidateMetrics, catalyst_shadow_lane, in_catalyst_window

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops" / "tools"))
import scalp_replay  # noqa: E402

CFG = yaml.safe_load((ROOT / "config" / "scanner.yaml").read_text())["scanner"]
CT = ZoneInfo("America/Chicago")


def _m(move, price=50.0, adv=50e6, spread=20.0, ticker="ACME"):
    return CandidateMetrics(ticker=ticker, price=price, prev_close=price / (1 + move), move_pct=move,
                            adv20_dollars=adv, rel_volume=4.0, minutes_since_hod=5, spread_bps=spread,
                            luld_headroom_pct=0.05, vwap=price, day_high=price)


def test_config_and_window():
    c = CFG["catalyst_shadow"]
    assert c["enabled"] is True and c["mode"] == "shadow"
    assert c["min_move_pct"] == 0.08 and c["gap_fade_min_move_pct"] == 0.15
    assert in_catalyst_window("09:45", CFG) and in_catalyst_window("10:04", CFG)
    assert not in_catalyst_window("09:44", CFG) and not in_catalyst_window("10:05", CFG)
    assert not in_catalyst_window("09:50", {"catalyst_shadow": {"enabled": False}})


def test_lane_classification():
    assert catalyst_shadow_lane(_m(0.09), True, CFG) == "EARNINGS_CONTINUATION"
    assert catalyst_shadow_lane(_m(-0.12), True, CFG) == "EARNINGS_CONTINUATION"
    assert catalyst_shadow_lane(_m(0.07), True, CFG) is None
    assert catalyst_shadow_lane(_m(0.16), False, CFG) == "GAP_FADE"
    assert catalyst_shadow_lane(_m(-0.16), False, CFG) is None          # down non-earnings: no lane
    assert catalyst_shadow_lane(_m(0.12), False, CFG) is None           # 8-15 non-earnings: coin flip
    assert catalyst_shadow_lane(_m(0.20, adv=5e6), False, CFG) is None
    assert catalyst_shadow_lane(_m(0.20, price=3.0), False, CFG) is None
    assert catalyst_shadow_lane(_m(0.20, spread=120.0), False, CFG) is None
    assert catalyst_shadow_lane(_m(0.20, ticker="SPY"), False, CFG) is None
    assert catalyst_shadow_lane(_m(0.20), False, CFG, asset_name="ProShares Ultra Thing") is None
    assert catalyst_shadow_lane(_m(0.20, ticker="ABCDW"), False, CFG) is None


def _bars(path, start="08:45"):
    h, mnt = map(int, start.split(":"))
    t0 = datetime(2026, 10, 15, h, mnt, tzinfo=CT)
    out = []
    for i, (o, hi, lo, c) in enumerate(path):
        ts = t0.replace(minute=0) + (t0 - t0.replace(minute=0))  # keep tz
        ts = t0 + (datetime(2026, 1, 1, 0, i % 60) - datetime(2026, 1, 1)) + (datetime(2026, 1, 1, i // 60) - datetime(2026, 1, 1))
        out.append({"ts": ts, "open": o, "high": hi, "low": lo, "close": c})
    return out


def test_replay_hold_ladder():
    entry_ts = datetime(2026, 10, 15, 8, 45, tzinfo=CT)
    # long from 100, 4% stop: runs to 112 (3R at 4 per R), trail 3.0 x atr(1.0) = 109 when hwm 112, then falls
    path = [(100, 101, 99.5, 100.8), (101, 104, 100.5, 104), (104, 108, 103.5, 108), (108, 112, 107.5, 111),
            (111, 111.5, 108.5, 108.6), (108.6, 109, 105, 105)]
    bars = _bars(path)
    r = scalp_replay.replay_hold(bars, "LONG", entry_ts, 100.0, 100, 0.04, 1.0, 3.0, 2.0)
    assert r["r_unit"] == 4.0
    assert r["exits"] and r["exits"][0][1] == "TRAIL" and r["exits"][0][3] == 109.0
    assert r["pnl"] == 900.0 and r["r"] == 2.25
    # stopped out: no time stop ever fires, the fixed stop does
    bars2 = _bars([(100, 100.5, 99, 99.2), (99.2, 99.5, 98, 98.1), (98, 98.5, 95.5, 96)])
    r2 = scalp_replay.replay_hold(bars2, "LONG", entry_ts, 100.0, 100, 0.04, 1.0)
    assert r2["exits"][0][1] == "STOP" and r2["r"] == -1.0
    # short mirror
    r3 = scalp_replay.replay_hold(_bars([(100, 100.2, 95, 95.5), (95.5, 96, 94, 94.2)]), "SHORT", entry_ts, 100.0, 100, 0.03, 1.0)
    assert r3["pnl"] > 0 and r3["exits"][-1][1] in ("EOD_MARK", "TRAIL", "BREAKEVEN")


def test_classify_shadow():
    assert classify("SHADOW", "EARNINGS_CONTINUATION", []) == ("SHADOW_EARNINGS_CONTINUATION", None)
    assert classify("SHADOW", "GAP_FADE", []) == ("SHADOW_GAP_FADE", None)


def test_a9_rules():
    quiet = {"funnel": {"by_outcome": {"SHADOW_EARNINGS_CONTINUATION": {"n": 6, "with_move_r": 2.0, "with_move_winners": 4},
                                       "SHADOW_GAP_FADE": {"n": 3, "short_r": 1.0, "short_winners": 2}}}}
    p, w = rule_earnings_lane(quiet); assert p is None and "6 of 20" in w
    p, w = rule_gap_fade_lane(quiet); assert p is None and "3 of 20" in w
    under = {"funnel": {"by_outcome": {"SHADOW_EARNINGS_CONTINUATION": {"n": 25, "with_move_r": 2.0, "with_move_winners": 16}}}}
    p, w = rule_earnings_lane(under); assert p is None and "under the bar" in w
    hot = {"funnel": {"by_outcome": {"SHADOW_EARNINGS_CONTINUATION": {"n": 25, "with_move_r": 7.5, "with_move_winners": 16},
                                     "SHADOW_GAP_FADE": {"n": 22, "short_r": 6.0, "short_winners": 14}}}}
    p, _ = rule_earnings_lane(hot); assert p and "earnings reaction" in p["title"] and p["evidence"]["n_instances"] == 25
    p, _ = rule_gap_fade_lane(hot); assert p and "gap fade" in p["title"]
    props, _ = generate(hot)
    assert {x["rule"] for x in props} >= {"rule_earnings_lane", "rule_gap_fade_lane"}


def test_wiring():
    svc = (ROOT / "src" / "c10_scanner" / "service.py").read_text()
    assert "await svc.catalyst_shadow_pass()" in svc and '"SHADOW", lane' in svc
    assert "reported_recently" in svc
    fun = (ROOT / "src" / "a11_metrics" / "funnel.py").read_text()
    assert "'EMITTED','CAPPED','SHADOW'" in fun and "replay_hold" in fun
    earn = (ROOT / "src" / "c1_ingestion" / "earnings.py").read_text()
    assert "async def reported_recently" in earn
