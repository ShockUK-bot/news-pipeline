"""v0.26.1: exits priced from the live quote; dashboard shows the current stop."""
import asyncio
from pathlib import Path

from c4_exec import engine as eng
from c4_exec.exits import ExitAction

ROOT = Path(__file__).resolve().parents[2]


class _Q:
    def __init__(self, bid, ask): self.bid, self.ask, self.price = bid, ask, (bid + ask) / 2


def _engine(quote):
    e = eng.PositionEngine.__new__(eng.PositionEngine)
    e.broker = object(); e.monitors = {}; e.unprotected_max_secs = 1; e.poll_sleep = 0
    e.now_fn = lambda: None
    if quote is not None:
        async def qf(ticker): return quote
        e.quote_fn = qf
    return e


def test_exit_price_uses_live_bid_or_ask():
    e = _engine(_Q(4.88, 4.89))
    bar = {"close": 4.914}
    assert asyncio.run(e._exit_price("FRMI", "LONG", bar)) == 4.87       # 4.88 * (1 - 0.002)
    assert asyncio.run(e._exit_price("FRMI", "SHORT", bar)) == 4.90      # 4.89 * (1 + 0.002)


def test_exit_price_falls_back_to_bar():
    e = _engine(None)
    assert asyncio.run(e._exit_price("FRMI", "LONG", {"close": 4.914})) == 4.91      # close - 10 bps
    assert asyncio.run(e._exit_price("FRMI", "LONG", {"close": 4.914, "bid": 4.88})) == 4.88

    async def broken(ticker): raise RuntimeError("no quote")
    e2 = _engine(None); e2.quote_fn = broken
    assert asyncio.run(e2._exit_price("FRMI", "SHORT", {"close": 4.914})) == 4.92


def test_wiring():
    assert "engine.quote_fn = marketdata.snapshot" in (ROOT / "src" / "c4_exec" / "service.py").read_text()
    mig = (ROOT / "schema" / "migrations" / "022-dash-positions-current-stop.sql").read_text()
    assert "COALESCE((p.exit_policy ->> 'current_stop')::numeric" in mig and "stop_basis" in mig
    assert "p.stop_basis" in (ROOT / "dashboard" / "index.html").read_text()
