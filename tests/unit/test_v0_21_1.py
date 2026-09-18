"""v0.21.1: the after-close session pass tests the close only and survives a
per-position error; an evening restart does not re-run it."""
import asyncio
from pathlib import Path

from c4_exec import engine as eng

ROOT = Path(__file__).resolve().parents[2]


def _engine(monkeypatch, positions, step_impl):
    async def _open_positions():
        return positions
    monkeypatch.setattr(eng, "open_positions", _open_positions)
    e = eng.PositionEngine.__new__(eng.PositionEngine)
    e.step = step_impl
    return e


def test_session_bar_is_close_only_and_errors_are_isolated(monkeypatch):
    seen = []

    async def step(pos, bar):
        seen.append((pos["ticker"], bar["high"], bar["low"], bar["close"], bar["tf"]))
        if pos["ticker"] == "BAD":
            raise RuntimeError("broker reject")
        return [f"{pos['ticker']}:ok"]

    async def daily(ticker):
        return {"open": 21.31, "high": 22.065, "low": 20.65, "close": 21.88}
    e = _engine(monkeypatch, [{"position_id": 1, "ticker": "BAD"}, {"position_id": 7, "ticker": "RIOT"}], step)
    out = asyncio.run(e.session_close_pass(daily))
    assert out == ["RIOT:ok"]                       # BAD's error did not stop RIOT
    assert seen[1] == ("RIOT", 21.88, 21.88, 21.88, "session")   # range collapsed to the close


def test_service_window():
    src = (ROOT / "src" / "c4_exec" / "service.py").read_text()
    assert 'SESSION_CLOSE_PASS_UNTIL_ET = "17:30"' in src
    assert 'overnight_done[today] = "session_close"' in src.split("hhmm_after >= SESSION_CLOSE_PASS_UNTIL_ET")[1][:400]
