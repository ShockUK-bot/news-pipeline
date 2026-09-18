"""Sector data source (v0.19.0): SEC SIC codes mapped to eleven sectors.

Why EDGAR: the pipeline already keeps the SEC CIK map (var/cik_map.json) and
polls EDGAR with a registered User-Agent. Each company's submissions record
(https://data.sec.gov/submissions/CIK##########.json) carries `sic` and
`sicDescription`; a static SIC range table turns that into a sector. No new
provider, no key, no cost. Coverage: US filers only (ADRs without a CIK stay
unknown, which the sizing chain already tolerates: SECTOR_UNKNOWN flag).

Consumers: A3 sizing (sector heat clip, live since this release), A2 analyst
context (`sector`), and later a scanner cluster rule.

CLI: python -m common.sectors --refresh [--days 7]   (nightly sector-map.timer)
     python -m common.sectors --lookup AAPL
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from common.config import config_path, load_yaml
from common.db import get_pool, close_pool
from common.log import get_logger, kv

log = get_logger("common.sectors")
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
REFRESH_DAYS = 90

# SIC ranges (inclusive) -> sector. Order matters: first match wins, so the
# narrower ranges sit above the broad divisions they carve out of.
SIC_SECTORS: list[tuple[int, int, str]] = [
    # narrow carve-outs first (first match wins)
    (2833, 2836, "Health Care"),        # drugs, biologicals
    (2840, 2844, "Consumer Staples"),   # soap, cosmetics
    (3812, 3812, "Industrials"),        # search, navigation (defense electronics)
    (3826, 3826, "Health Care"),        # lab analytical instruments
    (3841, 3851, "Health Care"),        # surgical, medical, dental, ophthalmic
    (3720, 3729, "Industrials"),        # aircraft and parts
    (3760, 3769, "Industrials"),        # guided missiles, space vehicles
    (5045, 5045, "Information Technology"),   # computers wholesale
    (5047, 5047, "Health Care"),        # medical equipment wholesale
    (5912, 5912, "Health Care"),        # drug stores
    (5171, 5172, "Energy"),             # petroleum wholesale
    (5140, 5149, "Consumer Staples"),   # groceries wholesale
    (5400, 5499, "Consumer Staples"),   # food stores
    (5810, 5813, "Consumer Discretionary"),   # restaurants
    (6500, 6599, "Real Estate"),        # real estate operators, agents
    (6798, 6798, "Real Estate"),        # REITs
    (1000, 1099, "Materials"),          # metal mining
    (3533, 3533, "Energy"),             # oil and gas field machinery (energy equipment)
    (3630, 3639, "Consumer Discretionary"),   # household appliances
    (3651, 3652, "Consumer Discretionary"),   # household audio and video
    (3600, 3629, "Industrials"),        # electrical equipment, motors and generators (3621)
    (3640, 3649, "Industrials"),        # lighting, wiring
    (8731, 8731, "Health Care"),        # commercial physical and biological research
    # broad divisions
    (1100, 1499, "Energy"),             # coal, oil and gas (1311, 1381, 1389)
    (1500, 1799, "Industrials"),        # construction
    (2000, 2099, "Consumer Staples"),   # food
    (2100, 2199, "Consumer Staples"),   # tobacco
    (2300, 2399, "Consumer Discretionary"),   # apparel
    (2600, 2699, "Materials"),          # paper
    (2711, 2741, "Communication Services"),   # newspapers, periodicals, publishing
    (2800, 2899, "Materials"),          # chemicals (after the carve-outs)
    (2911, 2911, "Energy"),             # petroleum refining
    (3200, 3299, "Materials"),          # stone, clay, glass
    (3300, 3399, "Materials"),          # primary metals
    (3400, 3569, "Industrials"),        # fabricated metal, machinery
    (3570, 3579, "Information Technology"),   # computers, office machines
    (3580, 3599, "Industrials"),        # refrigeration, industrial machinery
    (3660, 3699, "Information Technology"),   # comms equipment (3663), semiconductors (3674), components
    (3700, 3710, "Industrials"),        # transportation equipment
    (3711, 3799, "Consumer Discretionary"),   # motor vehicles, boats, motorcycles
    (3820, 3829, "Information Technology"),   # measuring and controlling instruments
    (3942, 3949, "Consumer Discretionary"),   # toys, sporting goods
    (4000, 4599, "Industrials"),        # railroads, trucking, air transport
    (4610, 4619, "Energy"),             # pipelines
    (4700, 4799, "Industrials"),        # transportation services
    (4810, 4899, "Communication Services"),   # telephone, radio, cable
    (4900, 4991, "Utilities"),          # electric, gas, water
    (5000, 5099, "Industrials"),        # wholesale durable goods
    (5200, 5999, "Consumer Discretionary"),   # retail
    (6000, 6799, "Financials"),         # banks, brokers, insurance
    (7000, 7099, "Consumer Discretionary"),   # hotels
    (7200, 7299, "Consumer Discretionary"),   # personal services
    (7300, 7369, "Industrials"),        # business services
    (7370, 7379, "Information Technology"),   # software, data processing
    (7380, 7399, "Industrials"),        # misc business services
    (7500, 7549, "Consumer Discretionary"),   # auto repair, rental
    (7810, 7841, "Communication Services"),   # motion pictures
    (7900, 7999, "Communication Services"),   # amusement, recreation
    (8000, 8099, "Health Care"),        # health services
    (8700, 8799, "Industrials"),        # engineering, management services
]

SECTORS = sorted({s for _, _, s in SIC_SECTORS})


def sector_for_sic(sic: Optional[int | str]) -> Optional[str]:
    """First matching SIC range, None for unknown or blank."""
    try:
        n = int(str(sic).strip())
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    for lo, hi, sector in SIC_SECTORS:
        if lo <= n <= hi:
            return sector
    return None


def parse_submissions(payload: dict) -> dict:
    """The fields we keep from a submissions record."""
    sic = payload.get("sic")
    return {"cik": int(payload.get("cik") or 0) or None,
            "sic": int(sic) if str(sic or "").strip().isdigit() else None,
            "sic_description": (payload.get("sicDescription") or None),
            "sector": sector_for_sic(sic),
            "name": payload.get("name")}


def load_ticker_ciks(path: Optional[str] = None) -> dict[str, int]:
    """ticker -> CIK from the cached SEC company_tickers.json (first wins)."""
    if path is None:
        path = ((load_yaml(config_path("sources.yaml")).get("edgar") or {})
                .get("cik_map_path") or "/opt/pipeline/var/cik_map.json")
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("cik map unreadable", extra=kv(path=path, error=repr(e)[:120]))
        return {}
    out: dict[str, int] = {}
    for rec in raw.values():
        t = str(rec.get("ticker") or "").strip().upper()
        try:
            cik = int(rec.get("cik_str"))
        except (TypeError, ValueError):
            continue
        if t and t not in out:
            out[t] = cik
    return out


def sector_heat_from(open_rows: list[tuple], sector: str) -> float:
    """Gross open risk dollars for positions in `sector`. rows: (sector, risk[, side])."""
    return round(sum(float(r[1]) for r in open_rows if r[0] == sector), 2)


def sector_heat_breakdown(open_rows: list[tuple], sector: str) -> dict:
    """v0.20.0: {gross, net, long, short, n} for `sector`. rows: (sector, risk, side).
    net = |long - short| so a pair trade in one sector does not double count."""
    long_r = sum(float(r[1]) for r in open_rows if r[0] == sector and (r[2] or "LONG") != "SHORT")
    short_r = sum(float(r[1]) for r in open_rows if r[0] == sector and (r[2] or "LONG") == "SHORT")
    return {"gross": round(long_r + short_r, 2), "net": round(abs(long_r - short_r), 2),
            "long": round(long_r, 2), "short": round(short_r, 2),
            "n": sum(1 for r in open_rows if r[0] == sector)}


# ---------------------------------------------------------------- store
async def lookup(ticker: str) -> Optional[str]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT sector FROM journal.sectors WHERE ticker=%s", (ticker.upper(),))
        row = await cur.fetchone()
    return row[0] if row else None


async def fetch_one(client: httpx.AsyncClient, ticker: str, cik: int) -> Optional[dict]:
    try:
        resp = await client.get(SUBMISSIONS_URL.format(cik=cik))
        if resp.status_code == 404:
            return {"cik": cik, "sic": None, "sic_description": None, "sector": None, "name": None}
        resp.raise_for_status()
        return parse_submissions(resp.json())
    except (httpx.HTTPError, ValueError) as e:
        log.warning("submissions fetch failed", extra=kv(ticker=ticker, cik=cik, error=repr(e)[:120]))
        return None


async def store(conn, ticker: str, rec: dict, source: str = "edgar_submissions") -> None:
    await conn.execute(
        """INSERT INTO journal.sectors (ticker, cik, sic, sic_description, sector, name, source, updated_ts)
           VALUES (%s,%s,%s,%s,%s,%s,%s, now())
           ON CONFLICT (ticker) DO UPDATE SET cik=EXCLUDED.cik, sic=EXCLUDED.sic,
               sic_description=EXCLUDED.sic_description, sector=EXCLUDED.sector,
               name=EXCLUDED.name, source=EXCLUDED.source, updated_ts=now()""",
        (ticker.upper(), rec.get("cik"), rec.get("sic"), rec.get("sic_description"),
         rec.get("sector"), rec.get("name"), source))


def _client() -> httpx.AsyncClient:
    from c1_ingestion.sources.edgar import user_agent
    return httpx.AsyncClient(timeout=10.0,
                             headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"})


async def lookup_or_fetch(ticker: str, timeout_secs: float = 6.0) -> Optional[str]:
    """Sector from the store, else one EDGAR fetch (bounded), stored either way
    so a miss is not retried on every sizing call. None on any failure."""
    ticker = ticker.upper()
    known = await lookup(ticker)
    if known is not None:
        return known
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM journal.sectors WHERE ticker=%s", (ticker,))
        if await cur.fetchone():
            return None                        # known unknown
    cik = load_ticker_ciks().get(ticker)
    if cik is None:
        async with pool.connection() as conn:
            await store(conn, ticker, {"cik": None, "sector": None}, source="no_cik")
        return None
    try:
        async with _client() as client:
            rec = await asyncio.wait_for(fetch_one(client, ticker, cik), timeout=timeout_secs)
    except (asyncio.TimeoutError, RuntimeError) as e:
        log.warning("sector fetch skipped", extra=kv(ticker=ticker, error=repr(e)[:120]))
        return None
    if rec is None:
        return None
    async with pool.connection() as conn:
        await store(conn, ticker, rec)
    return rec.get("sector")


async def open_sector_heat(sector: Optional[str]) -> Optional[dict]:
    """Open risk dollars already committed to `sector` (stop based, like the
    lane heat): {gross, net, long, short, n}. None when sector is unknown."""
    if not sector:
        return None
    from a3_risk.sizing import open_risk_dollars
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT s.sector, p.qty_open, p.avg_entry,
                      (p.exit_policy->'initial_stop'->>'price')::numeric, p.side
               FROM journal.positions p JOIN journal.sectors s USING (ticker)
               WHERE p.status='OPEN' AND s.sector=%s""", (sector,))
        rows = await cur.fetchall()
    trip = [(s, open_risk_dollars(q, float(e), float(st or 0), side or "LONG"), side) for s, q, e, st, side in rows]
    return sector_heat_breakdown(trip, sector)


async def open_scanner_in_sector(sector: Optional[str]) -> int:
    """v0.20.0: open origin=scanner positions already in `sector`."""
    if not sector:
        return 0
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT count(*) FROM journal.positions p JOIN journal.sectors s USING (ticker)
               WHERE p.status='OPEN' AND p.origin='scanner' AND s.sector=%s""", (sector,))
        return int((await cur.fetchone())[0])


# ---------------------------------------------------------------- nightly refresh
async def remap() -> int:
    """Recompute `sector` from the stored SIC for every row (after a table
    change). No network."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT ticker, sic, sector FROM journal.sectors WHERE sic IS NOT NULL")
        rows = await cur.fetchall()
        changed = 0
        for ticker, sic, old in rows:
            new = sector_for_sic(sic)
            if new != old:
                await conn.execute("UPDATE journal.sectors SET sector=%s, updated_ts=now() WHERE ticker=%s",
                                   (new, ticker))
                changed += 1
    log.info("sector remap done", extra=kv(rows=len(rows), changed=changed))
    return changed


async def refresh(days: int = 7, rate_per_sec: float = 8.0, dry: bool = False) -> dict:
    """Map every ticker the pipeline touched recently that has no row or a
    row older than REFRESH_DAYS. SEC fair access: under 10 requests/s."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """WITH u AS (
                 SELECT ticker FROM journal.positions
                 UNION SELECT ticker FROM journal.scanner_candidates
                       WHERE status IN ('EMITTED','CAPPED') AND scan_date >= current_date - %s
                 UNION SELECT ticker FROM journal.decisions
                       WHERE stage IN ('GATE','RISK','ANALYST','THESIS') AND ticker IS NOT NULL
                         AND ts >= now() - make_interval(days => %s)
                 UNION SELECT DISTINCT symbol FROM journal.burst_events WHERE ts >= now() - interval '30 days')
               SELECT u.ticker FROM u LEFT JOIN journal.sectors s USING (ticker)
               WHERE u.ticker IS NOT NULL AND u.ticker <> ''
                 AND (s.ticker IS NULL OR s.updated_ts < now() - make_interval(days => %s))
               ORDER BY 1""", (days, days, REFRESH_DAYS))
        todo = [r[0] for r in await cur.fetchall()]
    ciks = load_ticker_ciks()
    summary = {"candidates": len(todo), "fetched": 0, "mapped": 0, "no_cik": 0, "failed": 0}
    if dry:
        print(f"would map {len(todo)} tickers ({sum(1 for t in todo if t in ciks)} with a CIK)")
        return summary
    delay = 1.0 / max(rate_per_sec, 0.5)
    async with _client() as client:
        async with pool.connection() as conn:
            for t in todo:
                cik = ciks.get(t)
                if cik is None:
                    await store(conn, t, {"cik": None, "sector": None}, source="no_cik")
                    summary["no_cik"] += 1
                    continue
                rec = await fetch_one(client, t, cik)
                if rec is None:
                    summary["failed"] += 1
                    continue
                await store(conn, t, rec)
                summary["fetched"] += 1
                if rec.get("sector"):
                    summary["mapped"] += 1
                await asyncio.sleep(delay)
    try:
        from c1_ingestion.heartbeat import set_health
        await set_health("sectors", "OK" if summary["failed"] == 0 else "WARN",
                         ", ".join(f"{k} {v}" for k, v in summary.items()))
    except Exception as e:                                        # noqa: BLE001
        log.warning("heartbeat failed", extra=kv(error=repr(e)[:120]))
    log.info("sector refresh done", extra=kv(**summary))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--lookup")
    ap.add_argument("--remap", action="store_true", help="recompute sectors from stored SIC codes")
    args = ap.parse_args()

    async def _run():
        if args.lookup:
            print(args.lookup.upper(), await lookup_or_fetch(args.lookup))
        elif args.refresh:
            s = await refresh(args.days, dry=args.dry_run)
            print(s)
        elif args.remap:
            print("remapped", await remap())
        await close_pool()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
