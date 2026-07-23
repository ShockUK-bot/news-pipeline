"""C10 Momentum Scanner service (v0.12.1).

Every scan_interval_secs during the scan window (09:50–15:15 ET):
  1. read controls — scanner_enabled off or kill_switch on -> idle
  2. circuit breaker — >= N losing scanner trades today -> idle until tomorrow
  3. fetch top gainers from the Alpaca Screener API
  4. per ticker: deterministic metrics (quote/daily/minute via MarketData +
     common.ta), filters (rules.filter_candidate), news cross-check
  5. rank survivors, apply emission caps (top-N/scan, M/day, 1/ticker/day,
     concurrent-position cap)
  6. EMIT: one transaction — synthetic news item (source='scanner', Tier 1:
     the tape does not lie about prices) + signal.scanner enqueue +
     scanner_candidates EMITTED row

Everything C10 *sees* is journaled to journal.scanner_candidates (first
occurrence per ticker/day/status — a 60s rescan of the same reject is not
new evidence). The scanner proposes; A1/A2/C3/A3/C4 dispose, exactly like
every other input. Zero emissions is a correct day.
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal
from datetime import timedelta
from zoneinfo import ZoneInfo

from common.clock import session_open, utcnow
from common.config import config_path, load_yaml
from common.contracts import content_hash, envelope
from common.db import get_pool, jb, close_pool
from common.journal import register_config_version
from common.log import get_logger, kv
from common.marketdata import adv20, get_marketdata
from common.queue import enqueue
from common.ta import day_vwap, rel_volume_day, resample_5m
from c1_ingestion.heartbeat import set_health

from .rules import (CandidateMetrics, filter_candidate, in_scan_window,
                    luld_headroom, scanner_headline, score_candidate)
from .screener import get_screener

log = get_logger("c10.service")

OUT_QUEUE = "signal.scanner"
CONTRACT_SCANNER = "signal.scanner/1"
ET = ZoneInfo("America/New_York")


class C10Service:
    def __init__(self, cfg: dict, md=None, screener=None, now_fn=None):
        self.cfg = cfg["scanner"]
        self.md = md or get_marketdata()
        self.screener = screener or get_screener()
        self.now_fn = now_fn or utcnow
        # per-day memory (rebuilt from the journal on restart where it matters)
        self._journaled: set[tuple] = set()      # (date, ticker, status, reason)
        self._static_reject: dict[str, str] = {} # ticker -> static reject code (today)
        self._day = None

    # ------------------------------------------------------------------ state
    def _roll_day(self, today: str) -> None:
        if self._day != today:
            self._day = today
            self._journaled.clear()
            self._static_reject.clear()

    async def _controls(self) -> dict:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT key, value FROM journal.control WHERE key IN "
                "('scanner_enabled','kill_switch')")
            return {k: v for k, v in await cur.fetchall()}

    async def _emitted_today(self) -> tuple[int, set[str]]:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT ticker FROM journal.scanner_candidates
                   WHERE status='EMITTED'
                     AND scan_date=(now() AT TIME ZONE 'America/New_York')::date""")
            tickers = [r[0] for r in await cur.fetchall()]
        return len(tickers), set(tickers)

    async def _scanner_losses_today(self) -> int:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT count(*) FROM journal.positions
                   WHERE origin='scanner' AND status='CLOSED'
                     AND realized_pnl < 0
                     AND (closed_ts AT TIME ZONE 'America/New_York')::date =
                         (now() AT TIME ZONE 'America/New_York')::date""")
            return (await cur.fetchone())[0]

    async def _open_scanner_positions(self) -> int:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT count(*) FROM journal.positions
                   WHERE origin='scanner' AND status='OPEN'""")
            return (await cur.fetchone())[0]

    async def _open_tickers(self) -> set[str]:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT ticker FROM journal.positions WHERE status='OPEN'")
            return {r[0] for r in await cur.fetchall()}

    # -------------------------------------------------------------- journaling
    async def _journal_candidate(self, ticker: str, status: str,
                                 reason: str | None, metrics: dict,
                                 item_id: str | None = None,
                                 conn=None) -> None:
        """First occurrence per (day, ticker, status, reason) only — a 60s
        rescan repeating the same reject is not new evidence."""
        key = (self._day, ticker, status, reason)
        if key in self._journaled:
            return
        self._journaled.add(key)
        sql = """INSERT INTO journal.scanner_candidates
                 (ticker, status, reject_reason, metrics, item_id)
                 VALUES (%s,%s,%s,%s,%s)"""
        args = (ticker, status, reason, jb(metrics), item_id)
        if conn is not None:
            await conn.execute(sql, args)
            return
        pool = await get_pool()
        async with pool.connection() as c:
            await c.execute(sql, args)

    # ------------------------------------------------------------ measurement
    async def _measure(self, ticker: str, price_hint: float | None
                       ) -> CandidateMetrics | None:
        now = self.now_fn()
        open_utc = session_open(now)
        if open_utc is None:
            return None
        try:
            quote = await self.md.snapshot(ticker)
            prev = await self.md.prev_close(ticker)
            daily = await self.md.daily_bars(ticker, 25)
            minute = await self.md.minute_bars(ticker, open_utc, now)
        except Exception as e:
            log.warning("measure failed", extra=kv(ticker=ticker,
                                                   error=repr(e)[:150]))
            return None
        last = quote.price or price_hint or 0.0
        elapsed = (now - open_utc).total_seconds() / 60
        adv_sh = adv20(daily)
        hod_min = None
        day_high = None
        if minute:
            day_high = max(b["high"] for b in minute)
            hod_bar = max(minute, key=lambda b: b["high"])
            hod_min = int((now - hod_bar["ts"]).total_seconds() // 60)
        bars5 = resample_5m(minute, now)
        ref5 = bars5[-1]["close"] if bars5 else None
        return CandidateMetrics(
            ticker=ticker, price=last, prev_close=prev,
            move_pct=round(last / prev - 1, 5) if (prev and last) else None,
            adv20_dollars=round(adv_sh * prev, 0) if (adv_sh and prev) else None,
            rel_volume=rel_volume_day(minute, daily, elapsed),
            minutes_since_hod=hod_min,
            spread_bps=quote.spread_bps,
            luld_headroom_pct=luld_headroom(last, ref5),
            vwap=day_vwap(minute),
            day_high=day_high,
            detected_ts=now.isoformat())

    async def _news_match(self, ticker: str) -> tuple[str, list[dict]]:
        """strong: the news pipeline already escalated something for this
        ticker recently (it owns the move). weak: news naming the ticker
        exists but was not escalated — attach headlines as A2 context.
        none: a true dark mover."""
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT count(*) FROM journal.decisions
                   WHERE ticker=%s AND stage='TRIAGE' AND action <> 'DISCARD'
                     AND (item_id IS NULL OR item_id NOT LIKE 'scanner:%%')
                     AND ts > now() - make_interval(hours => %s)""",
                (ticker, int(self.cfg["news_strong_window_hours"])))
            if (await cur.fetchone())[0] > 0:
                return "strong", []
            cur = await conn.execute(
                """SELECT headline, source, published_ts FROM news.news_items
                   WHERE %s = ANY(symbols) AND source <> 'scanner'
                     AND received_ts > now() - make_interval(hours => %s)
                   ORDER BY received_ts DESC LIMIT 3""",
                (ticker, int(self.cfg["news_weak_window_hours"])))
            rows = await cur.fetchall()
        if rows:
            return "weak", [{"headline": r[0], "source": r[1],
                             "published_ts": r[2].isoformat()} for r in rows]
        return "none", []

    # ---------------------------------------------------------------- emission
    async def _emit(self, m: CandidateMetrics, score: float,
                    news_match: str, related: list[dict]) -> None:
        now = self.now_fn()
        item_id = f"scanner:{m.ticker}:{self._day}"
        headline = scanner_headline(m, news_match)
        summary = (f"Deterministic momentum detection. price={m.price} "
                   f"prev_close={m.prev_close} move={m.move_pct:+.2%} "
                   f"rel_volume={m.rel_volume}x vwap={m.vwap} "
                   f"spread_bps={m.spread_bps} news_match={news_match}. "
                   f"NOT a news event: the driver is unknown by construction.")
        body = {"scanner": {**m.payload(), "score": score,
                            "news_match": news_match,
                            "related_headlines": related},
                "item_ref": {"item_id": item_id, "revision": 1},
                "ticker": m.ticker}
        out = envelope(CONTRACT_SCANNER, "C10", item_id, item_id, 1, body)

        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO news.news_items
                       (item_id, revision, source, source_tier, headline,
                        summary, content_hash, symbols, channels,
                        published_ts, received_ts)
                       VALUES (%s,1,'scanner',1,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    (item_id, headline, summary,
                     content_hash(headline, summary), [m.ticker], ["scanner"],
                     now, now))
                await enqueue(OUT_QUEUE, item_id, out, conn=conn)
                await self._journal_candidate(m.ticker, "EMITTED", None,
                                              {**m.payload(), "score": score,
                                               "news_match": news_match},
                                              item_id=item_id, conn=conn)
        log.info("scanner EMIT", extra=kv(
            ticker=m.ticker, move=f"{(m.move_pct or 0):+.2%}",
            rel_vol=m.rel_volume, score=score, news=news_match))

    # -------------------------------------------------------------------- scan
    async def scan_once(self) -> int:
        """One scan cycle. Returns number of emissions (for tests)."""
        now_et = self.now_fn().astimezone(ET)
        self._roll_day(now_et.date().isoformat())
        if not in_scan_window(now_et.strftime("%H:%M"), self.cfg):
            return 0

        controls = await self._controls()
        if controls.get("scanner_enabled", "1") != "1":
            await set_health("scanner", "OK", "disabled by operator")
            return 0
        if controls.get("kill_switch") == "1":
            await set_health("scanner", "OK", "idle (kill switch armed)")
            return 0

        losses = await self._scanner_losses_today()
        if losses >= int(self.cfg["breaker_max_losses_per_day"]):
            await self._journal_candidate("*", "BREAKER", "MAX_LOSSES",
                                          {"losses_today": losses})
            await set_health("scanner", "DEGRADED",
                             f"circuit breaker: {losses} scanner losses today")
            return 0

        emitted_count, emitted_tickers = await self._emitted_today()
        if emitted_count >= int(self.cfg["max_per_day"]):
            await set_health("scanner", "OK",
                             f"daily cap reached ({emitted_count})")
            return 0

        try:
            movers = await self.screener.movers(top=20)
        except Exception as e:
            log.warning("screener fetch failed", extra=kv(error=repr(e)[:200]))
            await set_health("scanner", "DEGRADED",
                             f"screener error: {repr(e)[:120]}")
            return 0
        await set_health("scanner", "OK",
                         f"scanning ({emitted_count}/{self.cfg['max_per_day']} today)")

        open_tickers = await self._open_tickers()
        survivors: list[tuple[float, CandidateMetrics, str, list]] = []
        for mv in movers:
            t = mv["symbol"]
            if t in emitted_tickers or t in self._static_reject:
                continue
            if t in open_tickers:
                continue                    # never add to a held name
            # cheap pre-filters straight off the screener row
            if mv.get("change_pct") is not None \
                    and mv["change_pct"] < float(self.cfg["min_move_pct"]):
                continue
            if mv.get("price") is not None \
                    and mv["price"] < float(self.cfg["min_price"]):
                self._static_reject[t] = "PRICE_FLOOR"
                await self._journal_candidate(t, "FILTERED", "PRICE_FLOOR",
                                              dict(mv))
                continue
            m = await self._measure(t, mv.get("price"))
            if m is None:
                continue
            earn = await self._earnings(t)
            reject = filter_candidate(m, self.cfg,
                                      earnings_next_sessions=earn)
            if reject:
                if reject in ("ETF_EXCLUDED", "PRICE_FLOOR", "DOLLAR_VOLUME",
                              "EARNINGS_SOON"):
                    self._static_reject[t] = reject   # won't change today
                await self._journal_candidate(t, "FILTERED", reject, m.payload())
                continue
            news_match, related = await self._news_match(t)
            if news_match == "strong":
                await self._journal_candidate(t, "SUPPRESSED_NEWS",
                                              "NEWS_OWNS_IT", m.payload())
                continue
            survivors.append((score_candidate(m), m, news_match, related))

        survivors.sort(key=lambda s: s[0], reverse=True)
        emitted = 0
        open_scanner = await self._open_scanner_positions()
        for score, m, news_match, related in survivors:
            if emitted >= int(self.cfg["max_per_scan"]):
                await self._journal_candidate(m.ticker, "CAPPED", "PER_SCAN",
                                              {**m.payload(), "score": score})
                continue
            if emitted_count + emitted >= int(self.cfg["max_per_day"]):
                await self._journal_candidate(m.ticker, "CAPPED", "PER_DAY",
                                              {**m.payload(), "score": score})
                continue
            if open_scanner >= int(self.cfg["max_concurrent_positions"]):
                await self._journal_candidate(m.ticker, "CAPPED", "CONCURRENT",
                                              {**m.payload(), "score": score})
                continue
            await self._emit(m, score, news_match, related)
            emitted += 1
        return emitted

    async def _earnings(self, ticker: str) -> int | None:
        try:
            from c1_ingestion.earnings import earnings_next_sessions
            return await earnings_next_sessions(ticker)
        except Exception:
            return None


async def main() -> None:
    cfg = load_yaml(config_path("scanner.yaml"))
    await register_config_version("c10 scanner service startup")
    svc = C10Service(cfg)
    log.info("C10 up", extra=kv(window=f"{svc.cfg['session_start_et']}-"
                                       f"{svc.cfg['session_end_et']} ET",
                                caps=f"{svc.cfg['max_per_scan']}/scan "
                                     f"{svc.cfg['max_per_day']}/day"))
    await set_health("scanner", "OK", "started")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    interval = float(svc.cfg.get("scan_interval_secs", 60))
    while not stop.is_set():
        try:
            await svc.scan_once()
        except Exception as e:
            log.error("scan cycle error", extra=kv(error=repr(e)[:300]))
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    await set_health("scanner", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
