"""TA context pack (v0.12.0) — code-computed technical features injected into
model context. Pure arithmetic over MarketData bars; no DB, no LLM.

Design rules (same doctrine as the rest of the pipeline):
- NULL-SAFE EVERYWHERE. Thin history, off-session, immature windows, provider
  errors — every field degrades to None independently and the pack ALWAYS has
  the same key shape, so prompts stay stable (the v0.11.10 lesson: an immature
  window is "unavailable", never a veto and never an exception).
- Honest naming: `dist_from_high_pct` is measured over `high_window_sessions`
  actual sessions (a young listing doesn't get a fake "52-week" high).
- Consumers today: A2 analyst context (full pack), A12 guard context
  (intraday_only). The C10 scanner (v0.12.1) will reuse these functions.

Indicator conventions match `common.marketdata` (simple 14-mean ATR, plain
SMA); RSI uses Wilder smoothing (the number chart platforms show).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .clock import session_open, utcnow
from .log import get_logger
from .marketdata import MarketData, adv20, atr14, sma

log = get_logger("ta")

# Stable key shape — every build_ta_pack() result has exactly these keys.
NULL_INTRADAY = {"vwap": None, "vwap_dist_pct": None, "atr_5m": None,
                 "rel_volume_day": None, "day_range_pos": None,
                 "gap_pct": None}
NULL_DAILY = {"rsi_14": None, "sma20_dist_pct": None, "sma50_dist_pct": None,
              "trend_20_50": None, "dist_from_high_pct": None,
              "high_window_sessions": None, "ret_5d_pct": None,
              "atr_14": None}

SESSION_MINUTES = 390          # 9:30–16:00 ET


# ---------------------------------------------------------------------------
# Daily-bar indicators (pure)
# ---------------------------------------------------------------------------

def rsi14(daily: list[dict]) -> Optional[float]:
    """Wilder RSI(14) over daily closes (needs >= 15 bars)."""
    closes = [b["close"] for b in daily]
    if len(closes) < 15:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes[:-1], closes[1:]):
        chg = cur - prev
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_g = sum(gains[:14]) / 14
    avg_l = sum(losses[:14]) / 14
    for g, l in zip(gains[14:], losses[14:]):
        avg_g = (avg_g * 13 + g) / 14
        avg_l = (avg_l * 13 + l) / 14
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 1)


def sma_dist_pct(daily: list[dict], last: Optional[float], n: int
                 ) -> Optional[float]:
    """% distance of last price from the n-session SMA of closes."""
    if last is None:
        return None
    s = sma([b["close"] for b in daily], n)
    if not s:
        return None
    return round((last / s - 1) * 100, 2)


def trend_20_50(daily: list[dict]) -> Optional[str]:
    closes = [b["close"] for b in daily]
    s20, s50 = sma(closes, 20), sma(closes, 50)
    if s20 is None or s50 is None:
        return None
    if s20 > s50 * 1.001:
        return "up"
    if s20 < s50 * 0.999:
        return "down"
    return "flat"


def dist_from_high(daily: list[dict], last: Optional[float]
                   ) -> tuple[Optional[float], Optional[int]]:
    """(% below the highest high of up to the last 252 sessions, sessions in
    the window). Honest window: young listings report what they have."""
    if last is None or len(daily) < 20:
        return None, None
    window = daily[-252:]
    hi = max(b["high"] for b in window)
    if hi <= 0:
        return None, None
    return round((last / hi - 1) * 100, 2), len(window)


def ret_nd_pct(daily: list[dict], n: int) -> Optional[float]:
    closes = [b["close"] for b in daily]
    if len(closes) < n + 1:
        return None
    return round((closes[-1] / closes[-1 - n] - 1) * 100, 2)


# ---------------------------------------------------------------------------
# Intraday (minute-bar) indicators (pure)
# ---------------------------------------------------------------------------

def resample_5m(minute: list[dict], now: Optional[datetime] = None
                ) -> list[dict]:
    """Aggregate 1-min bars into completed 5-min buckets (the bucket that
    contains `now` is in progress and excluded)."""
    now = now or utcnow()
    cur_bucket = now.replace(minute=(now.minute // 5) * 5, second=0,
                             microsecond=0)
    buckets: dict[datetime, dict] = {}
    for b in minute:
        key = b["ts"].replace(minute=(b["ts"].minute // 5) * 5, second=0,
                              microsecond=0)
        if key >= cur_bucket:
            continue
        cur = buckets.get(key)
        if cur is None:
            buckets[key] = {"ts": key, "open": b["open"], "high": b["high"],
                            "low": b["low"], "close": b["close"],
                            "volume": b["volume"]}
        else:
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] += b["volume"]
    return [buckets[k] for k in sorted(buckets)]


def atr_5m(minute: list[dict], now: Optional[datetime] = None
           ) -> Optional[float]:
    """14-period mean-TR ATR on completed 5-min bars (same simple-mean
    convention as marketdata.atr14). Needs >= 15 completed 5-min bars
    (~75 min of tape) — earlier in the session it is None, by design."""
    bars5 = resample_5m(minute, now)
    return atr14(bars5)


def day_vwap(minute: list[dict]) -> Optional[float]:
    """Volume-weighted average price over the supplied session bars."""
    vol = sum(b["volume"] for b in minute)
    if not vol:
        return None
    return round(sum(b["vwap"] * b["volume"] for b in minute) / vol, 4)


def day_range_pos(minute: list[dict], last: Optional[float]
                  ) -> Optional[float]:
    """Where last sits in the day's range: 1.0 = at high, 0.0 = at low."""
    if last is None or not minute:
        return None
    hi = max(b["high"] for b in minute)
    lo = min(b["low"] for b in minute)
    if hi <= lo:
        return None
    return round(max(0.0, min(1.0, (last - lo) / (hi - lo))), 2)


def rel_volume_day(minute: list[dict], daily: list[dict],
                   elapsed_min: Optional[float]) -> Optional[float]:
    """Today's cumulative volume vs the pace ADV(20) implies for the elapsed
    fraction of the session. < 15 min elapsed is too noisy -> None."""
    if elapsed_min is None or elapsed_min < 15:
        return None
    base = adv20(daily)
    if not base:
        return None
    expected = base * min(elapsed_min, SESSION_MINUTES) / SESSION_MINUTES
    if expected <= 0:
        return None
    return round(sum(b["volume"] for b in minute) / expected, 2)


# ---------------------------------------------------------------------------
# Pack builder
# ---------------------------------------------------------------------------

async def build_ta_pack(md: MarketData, ticker: str, *,
                        intraday_only: bool = False) -> dict:
    """Assemble the TA pack. Never raises: any provider failure logs a
    warning and leaves that section at its null shape."""
    now = utcnow()
    pack = {"intraday": dict(NULL_INTRADAY), "daily": dict(NULL_DAILY)}

    last = None
    prev = None
    try:
        quote = await md.snapshot(ticker)
        last = quote.price or None
    except Exception as e:
        log.warning("ta snapshot unavailable for %s: %r", ticker, e)
    try:
        prev = await md.prev_close(ticker)
    except Exception as e:
        log.warning("ta prev_close unavailable for %s: %r", ticker, e)

    daily: list[dict] = []
    if not intraday_only:
        try:
            daily = await md.daily_bars(ticker, 260)
        except Exception as e:
            log.warning("ta daily bars unavailable for %s: %r", ticker, e)

    minute: list[dict] = []
    open_utc = session_open(now)
    elapsed_min = None
    if open_utc and now > open_utc:
        elapsed_min = (now - open_utc).total_seconds() / 60
        try:
            minute = await md.minute_bars(ticker, open_utc, now)
        except Exception as e:
            log.warning("ta minute bars unavailable for %s: %r", ticker, e)

    # -- intraday ------------------------------------------------------------
    if minute:
        vwap = day_vwap(minute)
        pack["intraday"]["vwap"] = vwap
        if vwap and last:
            pack["intraday"]["vwap_dist_pct"] = round((last / vwap - 1) * 100, 2)
        pack["intraday"]["atr_5m"] = atr_5m(minute, now)
        pack["intraday"]["day_range_pos"] = day_range_pos(minute, last)
        if prev:
            pack["intraday"]["gap_pct"] = round(
                (minute[0]["open"] / prev - 1) * 100, 2)
        if not intraday_only:
            pack["intraday"]["rel_volume_day"] = rel_volume_day(
                minute, daily, elapsed_min)

    # -- daily ---------------------------------------------------------------
    if daily:
        pack["daily"]["rsi_14"] = rsi14(daily)
        pack["daily"]["sma20_dist_pct"] = sma_dist_pct(daily, last, 20)
        pack["daily"]["sma50_dist_pct"] = sma_dist_pct(daily, last, 50)
        pack["daily"]["trend_20_50"] = trend_20_50(daily)
        dist, win = dist_from_high(daily, last)
        pack["daily"]["dist_from_high_pct"] = dist
        pack["daily"]["high_window_sessions"] = win
        pack["daily"]["ret_5d_pct"] = ret_nd_pct(daily, 5)
        pack["daily"]["atr_14"] = atr14(daily)

    return pack
