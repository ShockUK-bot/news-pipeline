"""v0.11.12 unit tests: C3 periodic marketdata liveness probe.

Incident (2026-07-22): the 'marketdata' heartbeat was only written when C3
computed volume for a signal (v0.5.9), so any >2-minute lull in gate traffic
aged the row past config/deadman.yaml's block_entries_min (2) and the
dead-man blocked all entries. The switch flapped block/unblock all session
(alerts=('marketdata', ~2.3-2.5) dozens of times) and A3 vetoed the day's
only gate PASS (PSKY 12:43:14 CT) BLOCK_ENTRIES — 18 seconds before the
unblock that the PASS's own volume computation triggered.

These tests pin the probe contract:
  - success (positive price)  -> exactly one OK 'marketdata' health write
  - provider raises           -> NO health write (age must keep growing:
                                 deadman reads updated_ts age, so a fresh
                                 DEGRADED row would blind it)
  - zero/None price           -> NO health write
"""
from datetime import timezone

import pytest

import c3_gate.service as c3s
from common.clock import utcnow
from common.marketdata import FakeData, Quote


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, component, status, detail=""):
        self.calls.append((component, status, detail))


class _RaisingMD:
    async def snapshot(self, symbol):
        raise RuntimeError("connection refused")


class _ZeroPriceMD:
    async def snapshot(self, symbol):
        return Quote(price=0.0, bid=0.0, ask=0.0, ts=utcnow())


async def test_probe_success_writes_ok(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(c3s, "set_health", rec)
    md = FakeData()  # unprogrammed symbols quote at $100
    assert await c3s.probe_marketdata(md, "SPY") is True
    assert rec.calls == [("marketdata", "OK", "probe ok (SPY)")]


async def test_probe_uses_given_symbol(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(c3s, "set_health", rec)
    assert await c3s.probe_marketdata(FakeData(), "QQQ") is True
    assert rec.calls == [("marketdata", "OK", "probe ok (QQQ)")]


async def test_probe_provider_error_writes_nothing(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(c3s, "set_health", rec)
    assert await c3s.probe_marketdata(_RaisingMD(), "SPY") is False
    assert rec.calls == []          # silence is the alarm — age must grow


async def test_probe_zero_price_writes_nothing(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(c3s, "set_health", rec)
    assert await c3s.probe_marketdata(_ZeroPriceMD(), "SPY") is False
    assert rec.calls == []


async def test_probe_never_raises(monkeypatch):
    # the probe runs inside the consume loop; an exploding provider must not
    # take the gate down with it.
    rec = _Recorder()
    monkeypatch.setattr(c3s, "set_health", rec)
    try:
        ok = await c3s.probe_marketdata(_RaisingMD(), "SPY")
    except Exception:  # pragma: no cover
        pytest.fail("probe_marketdata must swallow provider errors")
    assert ok is False


def test_default_probe_symbol_is_liquid_reference():
    assert c3s.PROBE_DEFAULT_SYMBOL == "SPY"
