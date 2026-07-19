"""Earnings-calendar source (closes the D7 P1 deferral) — two halves:

REFRESH (oneshot, earnings-calendar.timer, daily 05:15 ET — before A4):
  1. fetch the upcoming-earnings calendar from the configured provider.
     Default: Alpha Vantage EARNINGS_CALENDAR — ONE call returns a 3-month
     CSV for the whole US market (symbol, name, reportDate,
     fiscalDateEnding, estimate, currency). The free key allows 25
     calls/day; this service makes exactly one. The CSV carries no
     before/after-market timing, so session stays 'UNKNOWN' and the
     blackout math is conservative by design (see sessions_until below).
  2. upsert into news.earnings_calendar (PK ticker+report_date — re-runs
     and provider re-sends are no-ops that refresh fetched_ts), prune rows
     older than prune_days.
  3. journal ONE SYSTEM/C1 EARNINGS_REFRESH row (counts) + heartbeat the
     'earnings' health component. Unconfigured key / provider error ->
     DEGRADED + journal, never crash: A3 simply keeps flagging
     EARNINGS_UNKNOWN, exactly the pre-v0.10.0 behavior.

LOOKUPS (imported by A3 sizing and A2 context — defensive by contract:
any error returns None so trading paths never depend on this table):
  next_report(ticker)            -> (report_date, session) | None
  earnings_next_sessions(ticker) -> int | None
      NYSE sessions in (today, report_date]: 0 = reports today,
      1 = next session. With session timing UNKNOWN, the A3 veto at
      <= 1 session covers both the report-day gap (BMO tomorrow) and the
      report-evening gap (AMC today) — the conservative reading.

Secrets: the API key comes ONLY from the environment (ALPHAVANTAGE_KEY in
/etc/pipeline/pipeline.env), never from git-tracked config (rule 22).
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from common.config import config_path, load_yaml
from common.clock import utcnow
from common.db import get_pool
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from c1_ingestion.heartbeat import set_health

log = get_logger("c1.earnings")

ET = ZoneInfo("America/New_York")

AV_URL = ("https://www.alphavantage.co/query?function=EARNINGS_CALENDAR"
          "&horizon={horizon}&apikey={key}")


# --------------------------------------------------------------------------
# provider: Alpha Vantage CSV
# --------------------------------------------------------------------------

def parse_alphavantage_csv(text: str) -> list[dict]:
    """Rows: {ticker, report_date (date), eps_estimate, fiscal_ending}.
    Alpha Vantage answers errors (bad key, rate limit) as HTTP-200 JSON —
    a body that does not start with the CSV header is treated as a
    provider error (raises ValueError with a short excerpt)."""
    head = (text or "").lstrip()[:200]
    if not head.lower().startswith("symbol"):
        raise ValueError(f"not an earnings CSV: {head[:120]!r}")
    out: list[dict] = []
    for row in csv.DictReader(io.StringIO(text)):
        sym = (row.get("symbol") or "").strip().upper()
        raw_date = (row.get("reportDate") or "").strip()
        if not sym or not raw_date:
            continue
        try:
            rd = date.fromisoformat(raw_date)
        except ValueError:
            continue
        est_raw = (row.get("estimate") or "").strip()
        try:
            est = float(est_raw) if est_raw else None
        except ValueError:
            est = None
        fe_raw = (row.get("fiscalDateEnding") or "").strip()
        try:
            fe = date.fromisoformat(fe_raw) if fe_raw else None
        except ValueError:
            fe = None
        out.append({"ticker": sym, "report_date": rd, "eps_estimate": est,
                    "fiscal_ending": fe})
    return out


async def fetch_alphavantage(cfg: dict) -> str:
    key = os.environ.get(cfg.get("key_env", "ALPHAVANTAGE_KEY"), "").strip()
    if not key:
        raise RuntimeError("ALPHAVANTAGE_KEY not set in the environment")
    url = AV_URL.format(horizon=cfg.get("horizon", "3month"), key=key)
    timeout = float(cfg.get("timeout_secs", 60))
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

async def upsert_rows(rows: list[dict], source: str) -> int:
    if not rows:
        return 0
    pool = await get_pool()
    n = 0
    async with pool.connection() as conn:
        async with conn.transaction():
            for r in rows:
                await conn.execute(
                    """INSERT INTO news.earnings_calendar
                         (ticker, report_date, eps_estimate, fiscal_ending,
                          source, fetched_ts)
                       VALUES (%s,%s,%s,%s,%s, now())
                       ON CONFLICT (ticker, report_date) DO UPDATE
                         SET eps_estimate = EXCLUDED.eps_estimate,
                             fiscal_ending = EXCLUDED.fiscal_ending,
                             source = EXCLUDED.source,
                             fetched_ts = now()""",
                    (r["ticker"], r["report_date"], r["eps_estimate"],
                     r["fiscal_ending"], source))
                n += 1
    return n


async def prune(older_than_days: int) -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """DELETE FROM news.earnings_calendar
               WHERE report_date < current_date - %s::int""",
            (older_than_days,))
        return cur.rowcount or 0


# --------------------------------------------------------------------------
# lookups (A3 sizing / A2 context) — defensive: errors degrade to None
# --------------------------------------------------------------------------

_sessions_cache: dict[tuple[str, str], int] = {}


def sessions_until(today_et: date, report: date) -> int:
    """NYSE sessions in (today_et, report]. 0 = reports today (or on a
    non-session day rolled back conservatively), 1 = next session."""
    import pandas_market_calendars as mcal
    key = (today_et.isoformat(), report.isoformat())
    if key not in _sessions_cache:
        if report <= today_et:
            _sessions_cache[key] = 0
        else:
            sched = mcal.get_calendar("NYSE").schedule(
                start_date=(today_et + timedelta(days=1)).isoformat(),
                end_date=report.isoformat())
            _sessions_cache[key] = len(sched)
    return _sessions_cache[key]


async def next_report(ticker: str,
                      now: datetime | None = None
                      ) -> Optional[tuple[date, str]]:
    try:
        today_et = (now or utcnow()).astimezone(ET).date()
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT report_date, session FROM news.earnings_calendar
                   WHERE ticker = %s AND report_date >= %s
                   ORDER BY report_date LIMIT 1""",
                (ticker.strip().upper(), today_et))
            row = await cur.fetchone()
            return (row[0], row[1]) if row else None
    except Exception as e:                        # table missing / db error
        log.warning("next_report degraded to None: %s", repr(e)[:150])
        return None


async def earnings_next_sessions(ticker: str,
                                 now: datetime | None = None
                                 ) -> Optional[int]:
    nxt = await next_report(ticker, now)
    if nxt is None:
        return None
    try:
        today_et = (now or utcnow()).astimezone(ET).date()
        return sessions_until(today_et, nxt[0])
    except Exception as e:                        # calendar failure
        log.warning("earnings_next_sessions degraded to None: %s",
                    repr(e)[:150])
        return None


# --------------------------------------------------------------------------
# the refresh run
# --------------------------------------------------------------------------

async def run_refresh(cfg: dict, fetch=None) -> dict:
    """Returns the stats dict it journals. fetch: tests inject a fake."""
    run_date = utcnow().astimezone(ET).strftime("%Y-%m-%d")
    pcfg = cfg.get("provider") or {}
    source = pcfg.get("name", "alphavantage")
    fetch = fetch or (lambda: fetch_alphavantage(pcfg))
    try:
        text = await fetch()
        rows = parse_alphavantage_csv(text)
    except Exception as e:
        detail = repr(e)[:200]
        await set_health("earnings", "DEGRADED", f"refresh failed: {detail}")
        await write_decision(
            signal_id=f"earnings-{run_date}", stage="SYSTEM", agent="C1",
            action="EARNINGS_REFRESH_FAILED",
            payload={"run_date": run_date, "source": source,
                     "error": detail},
            reason="earnings refresh failed — A3 keeps flagging "
                   "EARNINGS_UNKNOWN (pre-v0.10.0 behavior)")
        log.error("earnings refresh failed", extra=kv(error=detail))
        return {"run_date": run_date, "ok": False, "error": detail}

    upserted = await upsert_rows(rows, source)
    pruned = await prune(int((cfg.get("store") or {}).get("prune_days", 7)))
    stats = {"run_date": run_date, "ok": True, "source": source,
             "rows_fetched": len(rows), "rows_upserted": upserted,
             "rows_pruned": pruned}
    await write_decision(
        signal_id=f"earnings-{run_date}", stage="SYSTEM", agent="C1",
        action="EARNINGS_REFRESH", payload=stats,
        reason=f"earnings calendar refreshed: {upserted} rows "
               f"({source}, {pruned} pruned)")
    await set_health("earnings", "OK",
                     f"{upserted} rows @ {run_date} ({source})")
    log.info("earnings refresh done", extra=kv(**stats))
    return stats


async def main() -> None:
    from common.db import close_pool
    cfg = load_yaml(config_path("earnings.yaml"))
    await register_config_version("earnings calendar refresh")
    try:
        await run_refresh(cfg)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
