"""Market data layer (Phase 3 decision: Alpaca Market Data API, free IEX feed,
behind a provider abstraction).

KNOWN CAVEAT (accepted, deferred-list item): the free feed is IEX-only —
roughly 2-3% of consolidated volume. C3's volume-multiple check computes on a
biased-but-consistent sample; directionally meaningful, absolutely wrong.
Must revisit (SIP feed or Polygon) before real capital.

Providers:
  AlpacaData — httpx against https://data.alpaca.markets (feed=iex).
               Code-complete; smoke test on the Spark (host unreachable from
               the build environment).
  FakeData   — deterministic fixture provider for tests/dev. Bars are
               programmable per symbol; unprogrammed symbols get a flat tape.

All timestamps aware UTC in and out. Bars are plain dicts:
  {"ts": datetime, "open": float, "high": float, "low": float,
   "close": float, "volume": int, "vwap": float}
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

import httpx

from .clock import utcnow
from .log import get_logger

log = get_logger("marketdata")


@dataclass
class Quote:
    price: float
    bid: float
    ask: float
    ts: datetime

    @property
    def spread_bps(self) -> float:
        mid = (self.bid + self.ask) / 2 or 1.0
        return round((self.ask - self.bid) / mid * 10_000, 2)


class MarketData(Protocol):
    async def minute_bars(self, symbol: str, start: datetime, end: datetime) -> list[dict]: ...
    async def daily_bars(self, symbol: str, n: int) -> list[dict]: ...
    async def snapshot(self, symbol: str) -> Quote: ...
    async def prev_close(self, symbol: str) -> float: ...


# ---------------------------------------------------------------------------
# Derived indicators — pure functions over bars (provider-independent)
# ---------------------------------------------------------------------------

def atr14(daily: list[dict]) -> Optional[float]:
    """Wilder ATR(14) over daily bars (needs >= 15)."""
    if len(daily) < 15:
        return None
    trs = []
    for prev, cur in zip(daily[-15:-1], daily[-14:]):
        tr = max(cur["high"] - cur["low"],
                 abs(cur["high"] - prev["close"]),
                 abs(cur["low"] - prev["close"]))
        trs.append(tr)
    return round(sum(trs) / len(trs), 4)


def adv20(daily: list[dict]) -> Optional[float]:
    if len(daily) < 20:
        return None
    return sum(b["volume"] for b in daily[-20:]) / 20


def sma(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def realized_vol(daily: list[dict], n: int = 20) -> Optional[float]:
    """Annualized close-to-close realized volatility over the last n sessions."""
    closes = [b["close"] for b in daily]
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return round(math.sqrt(var) * math.sqrt(252), 4)


def avg_minute_volume(minute: list[dict]) -> Optional[float]:
    if not minute:
        return None
    return sum(b["volume"] for b in minute) / len(minute)


# ---------------------------------------------------------------------------
# Alpaca (IEX feed)
# ---------------------------------------------------------------------------

class AlpacaData:
    BASE = "https://data.alpaca.markets"

    def __init__(self):
        key = os.environ.get("ALPACA_KEY_ID")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    @staticmethod
    def _bar(b: dict) -> dict:
        return {"ts": datetime.fromisoformat(b["t"].replace("Z", "+00:00")),
                "open": float(b["o"]), "high": float(b["h"]), "low": float(b["l"]),
                "close": float(b["c"]), "volume": int(b["v"]),
                "vwap": float(b.get("vw") or b["c"])}

    async def _get(self, path: str, params: dict) -> dict:
        params = {**params, "feed": "iex"}
        async with httpx.AsyncClient(timeout=15.0, headers=self._headers) as client:
            resp = await client.get(f"{self.BASE}{path}", params=params)
            resp.raise_for_status()
            return resp.json()

    async def minute_bars(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        data = await self._get(f"/v2/stocks/{symbol}/bars",
                               {"timeframe": "1Min", "start": start.isoformat(),
                                "end": end.isoformat(), "limit": 10_000,
                                "adjustment": "raw"})
        return [self._bar(b) for b in (data.get("bars") or [])]

    async def daily_bars(self, symbol: str, n: int) -> list[dict]:
        start = utcnow() - timedelta(days=int(n * 1.7) + 10)   # calendar padding
        data = await self._get(f"/v2/stocks/{symbol}/bars",
                               {"timeframe": "1Day", "start": start.isoformat(),
                                "limit": n + 30, "adjustment": "split"})
        bars = [self._bar(b) for b in (data.get("bars") or [])]
        return bars[-n:]

    async def snapshot(self, symbol: str) -> Quote:
        data = await self._get(f"/v2/stocks/{symbol}/snapshot", {})
        q = data.get("latestQuote") or {}
        t = data.get("latestTrade") or {}
        price = float(t.get("p") or q.get("ap") or 0.0)
        return Quote(price=price, bid=float(q.get("bp") or price),
                     ask=float(q.get("ap") or price),
                     ts=datetime.fromisoformat(
                         (t.get("t") or q.get("t")).replace("Z", "+00:00")))

    async def prev_close(self, symbol: str) -> float:
        bars = await self.daily_bars(symbol, 2)
        if not bars:
            raise RuntimeError(f"no daily bars for {symbol}")
        # if the last bar is today's (partial), the previous one is prev close
        last = bars[-1]
        if last["ts"].date() == utcnow().date() and len(bars) >= 2:
            return bars[-2]["close"]
        return last["close"]


# ---------------------------------------------------------------------------
# Fake (tests/dev)
# ---------------------------------------------------------------------------

@dataclass
class FakeData:
    """Programmable fixture provider. set_minute()/set_daily()/set_quote() per
    symbol; unprogrammed symbols get a flat $100 tape with 10k-share bars."""
    _minute: dict[str, list[dict]] = field(default_factory=dict)
    _daily: dict[str, list[dict]] = field(default_factory=dict)
    _quotes: dict[str, Quote] = field(default_factory=dict)
    _prev_close: dict[str, float] = field(default_factory=dict)

    # -- programming interface -------------------------------------------------
    def set_minute(self, symbol: str, bars: list[dict]) -> None:
        self._minute[symbol] = bars

    def set_daily(self, symbol: str, bars: list[dict]) -> None:
        self._daily[symbol] = bars

    def set_quote(self, symbol: str, quote: Quote) -> None:
        self._quotes[symbol] = quote

    def set_prev_close(self, symbol: str, price: float) -> None:
        self._prev_close[symbol] = price

    @staticmethod
    def flat_daily(n: int, close: float = 100.0, volume: int = 1_000_000,
                   end: datetime | None = None) -> list[dict]:
        end = end or utcnow()
        return [{"ts": end - timedelta(days=n - i), "open": close, "high": close * 1.005,
                 "low": close * 0.995, "close": close, "volume": volume, "vwap": close}
                for i in range(n)]

    @staticmethod
    def ramp_minute(start: datetime, minutes: int, start_price: float,
                    end_price: float, volume_each: int) -> list[dict]:
        out = []
        for i in range(minutes):
            p0 = start_price + (end_price - start_price) * i / max(minutes - 1, 1)
            p1 = start_price + (end_price - start_price) * (i + 1) / max(minutes, 1)
            out.append({"ts": start + timedelta(minutes=i), "open": p0,
                        "high": max(p0, p1), "low": min(p0, p1), "close": p1,
                        "volume": volume_each, "vwap": (p0 + p1) / 2})
        return out

    # -- MarketData interface ---------------------------------------------------
    async def minute_bars(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        bars = self._minute.get(symbol)
        if bars is None:
            bars = self.ramp_minute(start, max(int((end - start).total_seconds() // 60), 1),
                                    100.0, 100.0, 10_000)
        return [b for b in bars if start <= b["ts"] <= end]

    async def daily_bars(self, symbol: str, n: int) -> list[dict]:
        return (self._daily.get(symbol) or self.flat_daily(n))[-n:]

    async def snapshot(self, symbol: str) -> Quote:
        return self._quotes.get(symbol) or Quote(price=100.0, bid=99.98,
                                                 ask=100.02, ts=utcnow())

    async def prev_close(self, symbol: str) -> float:
        if symbol in self._prev_close:
            return self._prev_close[symbol]
        daily = self._daily.get(symbol)
        return daily[-1]["close"] if daily else 100.0


def get_marketdata() -> MarketData:
    kind = os.environ.get("MARKETDATA", "alpaca").lower()
    if kind == "alpaca":
        return AlpacaData()
    if kind == "fake":
        return FakeData()
    raise RuntimeError(f"unknown MARKETDATA={kind!r} (expected 'alpaca' or 'fake')")

