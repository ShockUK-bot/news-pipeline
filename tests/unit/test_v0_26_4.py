"""v0.26.4: broker cancel treats 422 as not cancellable; engine loop isolates positions; reconcile journals a catastrophe fill."""
import asyncio
from pathlib import Path

import httpx

from common.broker import AlpacaBroker

ROOT = Path(__file__).resolve().parents[2]


def _broker_raising(status):
    b = AlpacaBroker.__new__(AlpacaBroker)

    async def _req(method, path, **kw):
        req = httpx.Request(method, "https://paper-api.alpaca.markets" + path)
        raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(status, request=req))
    b._req = _req
    return b


def test_cancel_422_and_404_mean_not_cancellable():
    assert asyncio.run(_broker_raising(422).cancel("abc")) is False
    assert asyncio.run(_broker_raising(404).cancel("abc")) is False
    try:
        asyncio.run(_broker_raising(500).cancel("abc"))
        assert False, "5xx must still raise"
    except httpx.HTTPStatusError:
        pass


def test_wiring():
    svc = (ROOT / "src" / "c4_exec" / "service.py").read_text()
    assert "engine step failed for position; continuing" in svc
    rec = (ROOT / "src" / "c4_exec" / "reconcile.py").read_text()
    assert "_catastrophe_fill(broker, position_id)" in rec and "RECONCILE_CATASTROPHE_FILL" in rec
    assert "record_exit" in rec and "transition_order" in rec
