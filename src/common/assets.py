"""Alpaca assets endpoint client with TTL cache (v0.13, short selling).

One question, answered fresh enough to act on: *can this ticker be shorted
right now, easy-to-borrow?* Consumed by C3 (gate-time short check) and C4
(daily borrow re-check on open shorts). The scanner keeps its own daily
name cache (v0.12.14) — names change ~never; borrow flags move intraday,
so this client uses a short TTL (default 30 min) and answers conservatively
(not shortable) on any fetch failure.

LLMs never touch this module. Same creds as the broker; the assets endpoint
lives on the trading API host.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

from .log import get_logger

log = get_logger("assets")

ASSETS_BASE = "https://paper-api.alpaca.markets"
DEFAULT_TTL_SECS = 30 * 60


@dataclass
class AssetInfo:
    symbol: str
    name: str | None
    tradable: bool
    shortable: bool
    easy_to_borrow: bool
    marginable: bool

    @property
    def etb_shortable(self) -> bool:
        """The v0.13 policy bar: shortable AND easy-to-borrow (no locates)."""
        return self.tradable and self.shortable and self.easy_to_borrow


# conservative default used on any lookup failure: NOT shortable
def _unknown(symbol: str) -> AssetInfo:
    return AssetInfo(symbol=symbol, name=None, tradable=False,
                     shortable=False, easy_to_borrow=False, marginable=False)


class AssetsClient:
    def __init__(self, ttl_secs: int = DEFAULT_TTL_SECS,
                 now_fn=time.monotonic):
        key = os.environ.get("ALPACA_KEY_ID")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._ttl = ttl_secs
        self._now = now_fn
        self._cache: dict[str, tuple[float, AssetInfo]] = {}

    async def get(self, symbol: str) -> AssetInfo:
        symbol = symbol.upper()
        hit = self._cache.get(symbol)
        if hit is not None and self._now() - hit[0] < self._ttl:
            return hit[1]
        try:
            async with httpx.AsyncClient(timeout=10.0,
                                         headers=self._headers) as client:
                resp = await client.get(f"{ASSETS_BASE}/v2/assets/{symbol}")
                resp.raise_for_status()
                a = resp.json() or {}
            info = AssetInfo(
                symbol=symbol, name=a.get("name"),
                tradable=bool(a.get("tradable", False)),
                shortable=bool(a.get("shortable", False)),
                easy_to_borrow=bool(a.get("easy_to_borrow", False)),
                marginable=bool(a.get("marginable", False)))
        except Exception as e:                                # noqa: BLE001
            log.warning("asset lookup failed (treating as not shortable)",
                        extra={"symbol": symbol, "error": repr(e)[:150]})
            # cache the failure too — a flapping endpoint must not turn
            # into a request storm at gate time
            info = _unknown(symbol)
        self._cache[symbol] = (self._now(), info)
        return info


class FakeAssetsClient:
    """Test fixture. set_asset(symbol, shortable=..., easy_to_borrow=...)."""

    def __init__(self):
        self._assets: dict[str, AssetInfo] = {}
        self.lookups: list[str] = []

    def set_asset(self, symbol: str, name: str | None = None,
                  tradable: bool = True, shortable: bool = True,
                  easy_to_borrow: bool = True,
                  marginable: bool = True) -> None:
        self._assets[symbol.upper()] = AssetInfo(
            symbol=symbol.upper(), name=name, tradable=tradable,
            shortable=shortable, easy_to_borrow=easy_to_borrow,
            marginable=marginable)

    async def get(self, symbol: str) -> AssetInfo:
        self.lookups.append(symbol.upper())
        return self._assets.get(symbol.upper(), _unknown(symbol.upper()))
