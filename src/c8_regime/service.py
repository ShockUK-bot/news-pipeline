"""C8 Regime Context Builder (code, no model).

Phase 3 decision: features from ETF proxies on the same Alpaca data — no VIX
feed exists on the free tier, so the volatility field is honest-named
`realized_vol_20d` (SPY close-to-close, annualized), NOT a fake `vix`.
Swap in real VIX when a provider arrives; consumers key on field names.

Features written to journal.regime_snapshots.features:
  index_trend        "above_50d" | "below_50d"
  index_trend_slope  20d SMA-of-50d-SMA slope sign: "rising" | "falling" | "flat"
  realized_vol_20d   annualized SPY realized vol (VIX proxy)
  breadth_proxy      fraction of the 11 SPDR sector ETFs above their own 50d
  sector_rs          top/bottom 3 sectors by 20d relative strength vs SPY

Service: writes a snapshot every interval (config/c8.yaml); every A2 decision
references the latest regime_id.
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal

from common.clock import utcnow
from common.config import config_path, load_yaml
from common.db import close_pool, get_pool, jb
from common.log import get_logger, kv
from common.marketdata import MarketData, get_marketdata, realized_vol, sma

log = get_logger("c8.regime")

SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE",
               "XLI", "XLB", "XLU", "XLRE", "XLC"]
INDEX = "SPY"


async def compute_features(md: MarketData) -> dict:
    spy = await md.daily_bars(INDEX, 80)
    closes = [b["close"] for b in spy]
    sma50 = sma(closes, 50)
    last = closes[-1] if closes else None

    features: dict = {}
    if last is not None and sma50 is not None:
        features["index_trend"] = "above_50d" if last >= sma50 else "below_50d"
        sma50_prev = sma(closes[:-20], 50)
        if sma50_prev:
            delta = (sma50 - sma50_prev) / sma50_prev
            features["index_trend_slope"] = ("rising" if delta > 0.002 else
                                             "falling" if delta < -0.002 else "flat")
    rv = realized_vol(spy, 20)
    if rv is not None:
        features["realized_vol_20d"] = rv

    above = 0, 0
    counted, above_n = 0, 0
    rs: dict[str, float] = {}
    spy_ret20 = (closes[-1] / closes[-21] - 1) if len(closes) >= 21 else None
    for etf in SECTOR_ETFS:
        bars = await md.daily_bars(etf, 80)
        c = [b["close"] for b in bars]
        s50 = sma(c, 50)
        if s50 is not None and c:
            counted += 1
            if c[-1] >= s50:
                above_n += 1
        if spy_ret20 is not None and len(c) >= 21:
            rs[etf] = round((c[-1] / c[-21] - 1) - spy_ret20, 4)
    if counted:
        features["breadth_proxy"] = round(above_n / counted, 3)
    if rs:
        ranked = sorted(rs.items(), key=lambda kv_: kv_[1], reverse=True)
        features["sector_rs"] = {"top": dict(ranked[:3]), "bottom": dict(ranked[-3:])}

    features["computed_ts"] = utcnow().isoformat()
    features["source"] = "etf_proxies_iex"        # provenance: not a real VIX/breadth feed
    return features


async def write_snapshot(md: MarketData) -> int:
    features = await compute_features(md)
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO journal.regime_snapshots (ts, features)
               VALUES (now(), %s) RETURNING regime_id""",
            (jb(features),))
        regime_id = (await cur.fetchone())[0]
    log.info("regime snapshot", extra=kv(regime_id=regime_id,
                                         trend=features.get("index_trend"),
                                         rv=features.get("realized_vol_20d")))
    return regime_id


async def latest_regime_id() -> int | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT regime_id FROM journal.regime_snapshots ORDER BY ts DESC LIMIT 1")
        row = await cur.fetchone()
        return row[0] if row else None


async def main() -> None:
    cfg = load_yaml(config_path("c8.yaml"))
    md = get_marketdata()
    interval_open = float(cfg.get("interval_market_secs", 1800))
    interval_closed = float(cfg.get("interval_offhours_secs", 3600))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    from router.facts import market_open_now
    from c1_ingestion.heartbeat import set_health
    await set_health("regime", "OK", "snapshot loop running")
    while not stop.is_set():
        try:
            await write_snapshot(md)
        except Exception as e:
            log.error("snapshot failed", extra=kv(error=repr(e)[:200]))
            await set_health("regime", "DEGRADED", repr(e)[:200])
        wait = interval_open if market_open_now() else interval_closed
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    await set_health("regime", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

