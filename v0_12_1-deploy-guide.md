"""Alpaca Screener API client (v0.12.1) — top market movers + most actives.

GET https://data.alpaca.markets/v1beta1/screener/stocks/movers
GET https://data.alpaca.markets/v1beta1/screener/stocks/most-actives

Same credential env vars as common.marketdata.AlpacaData. The screener
endpoints are market-wide (no per-symbol subscription), so C10 never has to
stream the whole tape itself. FakeScreener mirrors the shape for tests/dev.

Returned mover dicts are normalized to:
  {"symbol": str, "price": float, "change_pct": float}   (change_pct 0.062 = +6.2%)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

from common.log import get_logger

log = get_logger("c10.screener")


class AlpacaScreener:
    BASE = "https://data.alpaca.markets/v1beta1/screener/stocks"

    def __init__(self):
        key = os.environ.get("ALPACA_KEY_ID")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    async def _get(self, path: str, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=15.0, headers=self._headers) as client:
            resp = await client.get(f"{self.BASE}{path}", params=params)
            resp.raise_for_status()
            return resp.json()

    async def movers(self, top: int = 20) -> list[dict]:
        """Top gainers (long-only book: losers are journal-only downstream,
        so we don't fetch them at all)."""
        data = await self._get("/movers", {"top": top})
        out = []
        for m in data.get("gainers") or []:
            out.append({"symbol": str(m["symbol"]).upper(),
                        "price": float(m["price"]),
                        "change_pct": float(m["percent_change"]) / 100.0})
        return out

    async def most_actives(self, top: int = 20) -> list[dict]:
        data = await self._get("/most-actives", {"by": "volume", "top": top})
        return [{"symbol": str(m["symbol"]).upper(), "price": None,
                 "change_pct": None} for m in data.get("most_actives") or []]


@dataclass
class FakeScreener:
    """Programmable fixture. set_movers([...]) with normalized mover dicts."""
    _movers: list[dict] = field(default_factory=list)
    _actives: list[dict] = field(default_factory=list)

    def set_movers(self, movers: list[dict]) -> None:
        self._movers = movers

    async def movers(self, top: int = 20) -> list[dict]:
        return self._movers[:top]

    async def most_actives(self, top: int = 20) -> list[dict]:
        return self._actives[:top]


def get_screener():
    kind = os.environ.get("MARKETDATA", "alpaca").lower()
    return FakeScreener() if kind == "fake" else AlpacaScreener()
