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

    SNAPSHOTS_URL = "https://data.alpaca.markets/v2/stocks/snapshots"

    async def most_actives(self, top: int = 20,
                           by: str = "volume") -> list[dict]:
        """Top names by share volume, price/change_pct filled from ONE batch
        snapshot call (v0.12.17) so the service can pre-filter non-movers
        cheaply — most actives are not moving 4%+, and they must cost one
        HTTP request per scan in total, not a full measurement each.
        Snapshot failure degrades gracefully: change_pct stays None and the
        service measures the ticker the expensive way.

        v0.12.28: `by` selects the ranking. 'volume' ranks by SHARE count,
        which structurally favours low-priced names — WDAY traded ~$1.8bn on
        2026-08-13 (+25%, the best single-day move on the tape) and never
        entered a top-50 by shares, because 8.75m shares is unremarkable next
        to sub-$10 tickers. 'trades' ranks by trade COUNT, which is a
        price-neutral proxy for "something is happening here" and is where a
        high-priced large cap in a news explosion actually shows up."""
        data = await self._get("/most-actives", {"by": by, "top": top})
        out = [{"symbol": str(m["symbol"]).upper(), "price": None,
                "change_pct": None} for m in data.get("most_actives") or []]
        if not out:
            return out
        try:
            async with httpx.AsyncClient(timeout=15.0,
                                         headers=self._headers) as client:
                resp = await client.get(
                    self.SNAPSHOTS_URL,
                    params={"symbols": ",".join(r["symbol"] for r in out)})
                resp.raise_for_status()
                body = resp.json() or {}
            snaps = body.get("snapshots", body)
            for r in out:
                s = snaps.get(r["symbol"]) or {}
                price = (s.get("latestTrade") or {}).get("p")
                prev_close = (s.get("prevDailyBar") or {}).get("c")
                if price is not None:
                    r["price"] = float(price)
                if price and prev_close:
                    r["change_pct"] = (float(price) - float(prev_close)) \
                        / float(prev_close)
        except Exception as e:                                # noqa: BLE001
            log.warning("snapshot enrich failed; actives pass through unpriced",
                        extra={"error": repr(e)[:150]})
        return out

    ASSETS_BASE = "https://paper-api.alpaca.markets"   # same creds as broker

    async def asset_name(self, symbol: str) -> str | None:
        """Official asset name from the trading API's assets endpoint
        (v0.12.14 — feeds the name-based ETF exclusion). None on any
        failure: the caller falls back to the ticker sets."""
        try:
            async with httpx.AsyncClient(timeout=10.0,
                                         headers=self._headers) as client:
                resp = await client.get(
                    f"{self.ASSETS_BASE}/v2/assets/{symbol}")
                resp.raise_for_status()
                return (resp.json() or {}).get("name")
        except Exception as e:                                # noqa: BLE001
            log.warning("asset name lookup failed",
                        extra={"symbol": symbol, "error": repr(e)[:150]})
            return None


@dataclass
class FakeScreener:
    """Programmable fixture. set_movers([...]) with normalized mover dicts."""
    _movers: list[dict] = field(default_factory=list)
    _actives: list[dict] = field(default_factory=list)
    _actives_by: dict = field(default_factory=dict)
    _names: dict = field(default_factory=dict)

    def set_movers(self, movers: list[dict]) -> None:
        self._movers = movers

    def set_actives(self, actives: list[dict]) -> None:
        self._actives = actives

    def set_asset_names(self, names: dict) -> None:
        self._names = names

    async def asset_name(self, symbol: str) -> str | None:
        return self._names.get(symbol)

    async def movers(self, top: int = 20) -> list[dict]:
        return self._movers[:top]

    async def most_actives(self, top: int = 20,
                           by: str = "volume") -> list[dict]:
        """v0.12.28: set_actives_by({'trades': [...]}) programs a per-leg
        fixture; plain set_actives() answers every leg (back-compatible)."""
        if by in self._actives_by:
            return self._actives_by[by][:top]
        return self._actives[:top]

    def set_actives_by(self, by_leg: dict) -> None:
        self._actives_by = dict(by_leg)


def get_screener():
    kind = os.environ.get("MARKETDATA", "alpaca").lower()
    return FakeScreener() if kind == "fake" else AlpacaScreener()
