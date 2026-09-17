"""v0.15.4: C12 scores from a realistic entry (first print after a delay, half
spread crossed), flags repeat bursts and oversize fades. Pure book tests plus
a service-level scoring test with a stubbed pool."""
import asyncio
import json
from pathlib import Path

import yaml

from c12_burst.book import SymbolBook
from c12_burst.detect import evaluate

ROOT = Path(__file__).resolve().parents[2] / "config"


def t0():
    return 1_800_000_000.0 - (1_800_000_000.0 % 5)


def test_entry_after_returns_first_print_after_delay():
    b = SymbolBook("X")
    now = t0()
    b.add_trade(now, 100.0, 10)          # the detection print (the spike)
    b.add_trade(now + 20, 99.7, 10)      # inside the delay: not fillable
    b.add_trade(now + 35, 99.5, 10)      # first print after 30 s
    b.add_trade(now + 40, 99.6, 10)
    px, ts = b.entry_after(now, 30)
    assert px == 99.5 and ts == now + 35 - (now + 35) % 5
    assert b.entry_after(now, 600) == (None, None)


def test_realistic_path_removes_the_first_minute_snap_back():
    """A fade scored from the print 'wins' on the snap back inside the first
    minute; scored from the delayed entry the same path is flat."""
    b = SymbolBook("X")
    now = t0()
    b.add_trade(now, 100.0, 10)
    b.add_trade(now + 35, 99.4, 10)      # snap back: -0.6% inside a minute
    b.add_trade(now + 300, 99.4, 10)
    b.add_trade(now + 1800, 99.4, 10)
    at_print = b.path(now, 100.0, -1, 1800, 0.005, 0.005)
    assert at_print["first_hit"] == "target"
    px, ts = b.entry_after(now, 30)
    late = b.path(ts, px, -1, 1800 - int(ts - now), 0.005, 0.005)
    assert late["first_hit"] == "none" and late["max_fav_pct"] < 0.001


def test_service_scores_realistic_and_flags_repeat_and_oversize(monkeypatch):
    from c12_burst import service as svc
    written = []

    class _Cur:
        def __init__(self, row): self._row = row
        async def fetchone(self): return self._row
    class _Conn:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def transaction(self): return self
        async def execute(self, sql, params=None):
            written.append((sql, params))
            return _Cur((len(written),) if "RETURNING" in sql else None)
    class _Pool:
        def connection(self): return _Conn()
    async def _pool(): return _Pool()
    monkeypatch.setattr(svc, "get_pool", _pool)
    monkeypatch.setenv("ALPACA_KEY_ID", "k"); monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    cfg = yaml.safe_load((ROOT / "burst.yaml").read_text())
    cfg["detect"]["session_start_ct"] = "00:00"; cfg["detect"]["session_end_ct"] = "23:59"
    cfg["detect"]["baseline_rth_only"] = False   # the synthetic clock is not inside RTH
    s = svc.C12Service(cfg, ["X"])
    now = t0()
    async def _ctx(symbol, ts): return (False, None, False)
    async def _spr(symbol): return 20.0
    s._context = _ctx; s._spread_bps = _spr
    b = s.books["X"]
    # 30 min of quiet baseline, then a +2% burst (oversize for a fade)
    for k in range(360):
        b.add_trade(now - 1800 + k * 5, 100.0, 100)
    for k in range(12):
        b.add_trade(now - 59 + k * 5, 100.0 * (1 + 0.02 * (k + 1) / 12), 600)
    s.last_fire["X"] = now - 1000               # a prior burst 1000 s ago -> repeat
    ev = evaluate(b, now, cfg["detect"], s.last_fire)
    assert ev is not None
    # mirror the service loop's flagging
    gap = now - (now - 1000)
    ev.detail["prior_burst_secs"] = round(gap)
    ev.detail["repeat"] = gap <= cfg["detect"]["repeat_window_secs"]
    ev.detail["fade_oversize"] = max(abs(ev.ret_60s or 0), abs(ev.ret_120s or 0)) > cfg["detect"]["fade_max_burst_pct"]
    assert ev.detail["repeat"] is True and ev.detail["fade_oversize"] is True
    asyncio.run(s._journal_event(ev))
    assert len(s.pending) == 2 and s.pending[0]["spread_bps"] == 20.0
    # forward path: first print 40 s after detection, then flat
    b.add_trade(now + 40, 101.5, 10)
    b.add_trade(now + 1800, 101.5, 10)
    asyncio.run(s._fill_pending(now + 1900))
    updates = [w for w in written if "UPDATE journal.burst_events" in w[0]]
    assert len(updates) == 2 and not s.pending
    detail = json.loads(updates[0][1][8])
    r = detail["realistic"]
    assert r["delay_secs"] == 30 and r["print_px"] == 101.5 and r["half_spread_bps"] == 10.0
    assert set(r["brackets"]) == set(detail["brackets"])
    # long side pays up half the spread, short side sells down
    longs = [json.loads(u[1][8])["realistic"]["entry_px"] for u in updates]
    assert max(longs) > 101.5 > min(longs)


def test_burst_yaml_v0_15_4_pins():
    d = yaml.safe_load((ROOT / "burst.yaml").read_text())["detect"]
    assert 10 <= d["entry_delay_secs"] <= 120
    assert d["repeat_window_secs"] >= d["cooldown_secs"]
    assert 0.005 < d["fade_max_burst_pct"] <= 0.03
