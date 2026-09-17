"""v0.17.0: A11 measurement layer pure functions and wiring."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from a11_metrics.metrics import (classify_guard, downsample, excursions, exit_efficiency,
                                 post_exit_outcome, rate, realized_r, target_price)

ROOT = Path(__file__).resolve().parents[2]


def bars(closes, start=None, step_min=1):
    t0 = start or datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
    return [{"ts": t0 + timedelta(minutes=i * step_min), "open": c, "high": c + 0.5, "low": c - 0.5, "close": c}
            for i, c in enumerate(closes)]


def test_excursions_long_and_short():
    b = bars([100, 101, 103, 99, 100])
    L = excursions(b, "LONG", 100.0, 2.0)
    assert L["mfe_r"] == 1.75 and L["mae_r"] == 0.75 and L["fav_px"] == 103.5 and L["bars"] == 5
    S = excursions(b, "SHORT", 100.0, 2.0)
    assert S["mfe_r"] == 0.75 and S["mae_r"] == 1.75
    # window trims
    W = excursions(b, "LONG", 100.0, 2.0, start=b[3]["ts"])
    assert W["bars"] == 2 and W["mfe_r"] == 0.25


def test_realized_r_and_efficiency():
    assert realized_r(250.0, 100, 2.5) == 1.0
    assert exit_efficiency(1.0, 2.0) == 0.5 and exit_efficiency(-0.5, 0.0) is None
    assert target_price(100.0, "LONG", 0.6, 0.05) == 103.0
    assert target_price(100.0, "SHORT", 0.6, 0.05) == 97.0


def test_classify_guard_exit_and_hold():
    # EXIT verdict, position then lost 1R: exiting would have saved 1R
    assert classify_guard("EXIT", -1.0) == ("SAVE", 1.0)
    # EXIT verdict, position then gained 1R: shakeout
    assert classify_guard("EXIT", 1.0) == ("SHAKEOUT", -1.0)
    assert classify_guard("TIGHTEN_STOP", 0.1) == ("NEUTRAL", -0.1)
    # HOLD verdict, then gained: holding was right
    assert classify_guard("HOLD", 0.8) == ("SAVE", 0.8)
    assert classify_guard("HOLD", -0.8) == ("SHAKEOUT", -0.8)
    assert classify_guard("HOLD", 0.0) == ("NEUTRAL", 0.0)
    # v0.17.1: a HOLD on a >= 1R winner is the ladder's give-back, not a shakeout
    assert classify_guard("HOLD", -2.5, unrealized_r=2.26) == ("NEUTRAL", -2.5)
    assert classify_guard("HOLD", -0.8, unrealized_r=0.9) == ("SHAKEOUT", -0.8)
    assert classify_guard("EXIT", 1.0, unrealized_r=2.0) == ("SHAKEOUT", -1.0)


def test_post_exit_outcome_sign():
    assert post_exit_outcome("LONG", 100.0, 102.0, 2.0) == 1.0     # left 1R on the table
    assert post_exit_outcome("SHORT", 100.0, 102.0, 2.0) == -1.0   # avoided 1R of loss


def test_downsample_and_rate():
    pts = [(datetime(2026, 9, 17, tzinfo=timezone.utc) + timedelta(minutes=i), 100 + i) for i in range(100)]
    d = downsample(pts, 10)
    assert len(d) == 10 and d[0][1] == 100 and d[-1][1] == 199 and isinstance(d[0][0], str)
    assert downsample(pts[:3], 10) == [[p[0].isoformat(), float(p[1])] for p in pts[:3]]
    assert rate(1, 4) == 0.25 and rate(0, 0) is None


def test_wiring():
    svc = (ROOT / "ops" / "systemd" / "a11-metrics.service").read_text()
    tim = (ROOT / "ops" / "systemd" / "a11-metrics.timer").read_text()
    assert "a11_metrics.service" in svc and "15:20" in tim
    wd = yaml.safe_load((ROOT / "config" / "watchdog.yaml").read_text())
    assert "a11-metrics" in wd["timers"] and "scanner-counterfactual" not in wd["timers"]
    assert wd["heartbeats"]["metrics"]["unit"] == "a11-metrics"
    src = (ROOT / "src" / "a11_metrics" / "service.py").read_text()
    assert "paper" not in src.lower() or "order" not in src.lower().replace("order by", ""), "A11 must not touch orders"
