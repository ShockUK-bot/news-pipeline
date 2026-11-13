"""v0.31.5: the scanner replay gets variants on the LIVE ladder (v27, v27_be1.5,
v27_nobe) and an 'asrun' variant that replays each trade under its own
journaled exit policy; the nightly job backfills variants added later."""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops" / "tools"))
sys.path.insert(0, str(ROOT / "ops"))
import scalp_replay as sr  # noqa: E402

CT = ZoneInfo("America/Chicago")
ENTRY = datetime(2026, 11, 11, 8, 55, tzinfo=CT)

LIVE_POLICY = {"profile": "scalp_v1", "trail": {"k": 3.0, "activate_at_R": 1.0},
               "breakeven_at_R": 0.75, "time_stop": None,
               "realization": {"action": "none", "target_fraction": 0.6},
               "initial_stop": {"k": 2.0}}
OLD_POLICY = {"profile": "scalp_v1", "trail": {"k": 1.5, "activate_at_R": 1.0},
              "breakeven_at_R": 0.75,
              "time_stop": {"window_minutes": 60, "min_progress_R": 0.5},
              "realization": {"action": "scale_out_50", "target_fraction": 0.6},
              "initial_stop": {"k": 2.0}}


def _bars(path):
    """path: [(open, high, low, close)] one per minute from ENTRY."""
    return [{"ts": ENTRY + timedelta(minutes=i), "open": o, "high": h, "low": lo, "close": c}
            for i, (o, h, lo, c) in enumerate(path)]


def test_policy_kwargs_live_and_old_profiles():
    live = sr.policy_kwargs(LIVE_POLICY)
    assert live == {"stop_k": 2.0, "trail_k": 3.0, "trail_at": 1.0, "be_r": 0.75,
                    "scale_out": False, "tf": 0.6, "time_stop": False}
    old = sr.policy_kwargs(OLD_POLICY)
    assert old["trail_k"] == 1.5 and old["scale_out"] is True and old["time_stop"] is True
    assert old["time_win"] == 60 and old["time_min_r"] == 0.5


def test_policy_kwargs_declines_what_the_replay_cannot_model():
    assert sr.policy_kwargs(None) is None
    promoted = dict(OLD_POLICY, profile="short_term_v1",
                    time_stop={"window": "2_sessions", "min_progress_R": 0.5},
                    profit_lock={"activate_pct": 0.05, "trail_pct": 0.04})
    assert sr.policy_kwargs(promoted) is None
    assert sr.policy_kwargs(dict(LIVE_POLICY, trail={})) is None


def test_v27_is_the_live_ladder_and_asrun_matches_it_for_a_live_policy():
    # entry 100, ATR 1 -> stop 98, R = 2. Runs to 101.9 (0.95R: breakeven arms),
    # comes back through entry: the live ladder scratches at breakeven; the old
    # base ladder banked half at the target (100 * (1 + 0.6 * 0.02) = 101.2).
    path = [(100, 100.4, 99.8, 100.3), (100.3, 101.9, 100.2, 101.8),
            (101.8, 101.9, 100.5, 100.6), (100.6, 100.7, 99.9, 100.0)]
    res = sr.replay_variants(_bars(path), "LONG", ENTRY, 100.0, 100, 1.0, 0.02, policy=LIVE_POLICY)
    assert set(res) == set(sr.VARIANTS) | {sr.ASRUN}
    assert res["v27"]["exits"] == [("08:58", "BREAKEVEN", 100, 100.0)] and res["v27"]["pnl"] == 0.0
    assert res[sr.ASRUN]["exits"] == res["v27"]["exits"]
    assert res["base"]["exits"][0][1] == "TARGET" and res["base"]["pnl"] > 0
    # no breakeven ratchet: the same path rides back to the initial stop region
    assert res["v27_nobe"]["exits"][0][1] != "BREAKEVEN"
    assert res["v27_be1.5"]["exits"][0][1] != "BREAKEVEN"


def test_asrun_for_an_old_policy_equals_base_and_is_absent_when_unmodelled():
    path = [(100, 100.4, 99.8, 100.3), (100.3, 101.9, 100.2, 101.8),
            (101.8, 101.9, 100.5, 100.6), (100.6, 100.7, 99.9, 100.0)]
    res = sr.replay_variants(_bars(path), "LONG", ENTRY, 100.0, 100, 1.0, 0.02, policy=OLD_POLICY)
    assert res[sr.ASRUN]["exits"] == res["base"]["exits"] and res[sr.ASRUN]["pnl"] == res["base"]["pnl"]
    none = sr.replay_variants(_bars(path), "LONG", ENTRY, 100.0, 100, 1.0, 0.02)
    assert sr.ASRUN not in none and set(none) == set(sr.VARIANTS)


def test_v27_short_trail_locks_profit_only_past_one_and_a_half_r():
    # short 100, ATR 1, R = 2. Low 96 (2R): trail = 96 + 3 = 99 (0.5R locked).
    path = [(100, 100.1, 99.5, 99.6), (99.6, 99.7, 96.0, 96.2), (96.2, 99.2, 96.1, 99.1)]
    r = sr.replay(_bars(path), "SHORT", ENTRY, 100.0, 100, 1.0, 0.02, 2.0,
                  **{k: v for k, v in sr.VARIANTS["v27"].items()})
    assert r["exits"] == [("08:57", "TRAIL", 100, 99.0)] and r["pnl"] == 100.0


def test_pending_query_backfills_missing_variants():
    src = (ROOT / "ops" / "scanner_counterfactuals.py").read_text()
    assert "c.variant = ANY(%s)) < %s" in src and "(list(VARIANTS), len(VARIANTS))" in src
    assert 'policy=p.get("policy")' in src
