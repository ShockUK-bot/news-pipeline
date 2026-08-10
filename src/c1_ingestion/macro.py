"""Macro/economy series source (v0.12.24) — the system's first non-equity
input. Modeled directly on c1_ingestion.earnings: an oneshot REFRESH half
(macro-fetch.timer, daily 20:45 ET — 45 minutes before A5) and a defensive
LOOKUP half (macro_context, imported by A5).

REFRESH:
  1. For each configured FRED series: fetch observations — incrementally
     once the series has rows (last obs minus store.refetch_overlap_days,
     so revised prints like CPI/payrolls are re-captured), full
     store.backfill_years history on first sight.
  2. Upsert into news.macro_series (PK series_id+obs_date; re-runs and
     revisions are no-ops / value refreshes). Missing observations ('.')
     are never stored.
  3. Journal ONE SYSTEM/C1 MACRO_REFRESH row (per-series counts +
     failures) + heartbeat the 'macro' health component. Per-series
     failures never abort the run; ALL series failing -> DEGRADED +
     MACRO_REFRESH_FAILED, never a crash. A5 simply runs without a macro
     block that night, exactly like pre-v0.12.24.

Backends: keyless fredgraph CSV by default (no account, no secret); the
official api.stlouisfed.org JSON API automatically when FRED_KEY is set in
the environment (rule 22: secrets only ever from the environment).

LOOKUP (defensive by contract — any error returns None so A5 and anything
else downstream never depend on this table existing or being fresh):
  macro_context() -> compact dict for the A5 pack: per configured series,
  the transformed latest reading plus 1m/3m/1y changes, grouped and
  ordered; stale series (newest obs older than context.stale_after_days)
  are excluded and counted.
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
from datetime import date, timedelta

import httpx

from common.config import config_path, load_yaml
from common.clock import utcnow
from common.db import get_pool
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from c1_ingestion.heartbeat import set_health

log = get_logger("c1.macro")

FREDGRAPH_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
FRED_API_URL = ("https://api.stlouisfed.org/fred/series/observations"
                "?series_id={sid}&api_key={key}&file_type=json"
                "&observation_start={start}")

TRANSFORMS = ("level", "yoy_pct", "mom_diff")
GROUP_ORDER = ("rates_curve", "inflation", "labor", "growth",
               "credit_risk", "dollar_energy", "housing_money")


# --------------------------------------------------------------------------
# parsing (pure)
# --------------------------------------------------------------------------

def parse_fredgraph_csv(text: str, series_id: str) -> list[tuple[date, float]]:
    """fredgraph.csv: two columns — a date column (header 'DATE' or
    'observation_date' depending on FRED vintage) and the series column.
    Missing observations arrive as '.' and are skipped. A body that does
    not look like that CSV (HTML error page, throttle notice) raises
    ValueError with a short excerpt."""
    head = (text or "").lstrip()[:200]
    low = head.lower()
    if not (low.startswith("date") or low.startswith("observation_date")
            or low.startswith('"date"') or low.startswith('"observation_date"')):
        raise ValueError(f"not a fredgraph CSV for {series_id}: {head[:120]!r}")
    out: list[tuple[date, float]] = []
    for row in csv.DictReader(io.StringIO(text)):
        cols = {k.strip().lower(): (v or "").strip() for k, v in row.items()
                if k is not None}
        raw_date = cols.get("date") or cols.get("observation_date") or ""
        # the value column is whatever isn't the date column
        raw_val = ""
        for k, v in cols.items():
            if k not in ("date", "observation_date"):
                raw_val = v
                break
        if not raw_date or raw_val in ("", "."):
            continue
        try:
            out.append((date.fromisoformat(raw_date), float(raw_val)))
        except ValueError:
            continue
    return out


def parse_fred_api_json(payload: dict, series_id: str) -> list[tuple[date, float]]:
    obs = payload.get("observations")
    if not isinstance(obs, list):
        raise ValueError(f"unexpected FRED API shape for {series_id}: "
                         f"{str(payload)[:120]!r}")
    out: list[tuple[date, float]] = []
    for o in obs:
        raw_val = (o.get("value") or "").strip()
        raw_date = (o.get("date") or "").strip()
        if not raw_date or raw_val in ("", "."):
            continue
        try:
            out.append((date.fromisoformat(raw_date), float(raw_val)))
        except ValueError:
            continue
    return out


# --------------------------------------------------------------------------
# feature computation (pure — unit-tested directly)
# --------------------------------------------------------------------------

def _value_at(obs: list[tuple[date, float]], target: date,
              tolerance_days: int = 62) -> float | None:
    """Newest observation at or before `target`, if it is within
    `tolerance_days` of it (monthly series need slack; a years-old value
    must not impersonate 'three months ago')."""
    best = None
    for d, v in obs:
        if d <= target:
            best = (d, v)
        else:
            break
    if best is None or (target - best[0]).days > tolerance_days:
        return None
    return best[1]


def transform_series(obs: list[tuple[date, float]],
                     transform: str) -> list[tuple[date, float]]:
    """obs must be date-ascending. Returns the transformed series."""
    if transform == "level":
        return list(obs)
    if transform == "mom_diff":
        return [(obs[i][0], obs[i][1] - obs[i - 1][1])
                for i in range(1, len(obs))]
    if transform == "yoy_pct":
        out = []
        for d, v in obs:
            base = _value_at(obs, d - timedelta(days=365), tolerance_days=45)
            if base is not None and base != 0:
                out.append((d, (v / base - 1.0) * 100.0))
        return out
    raise ValueError(f"unknown transform {transform!r}")


def compute_features(obs: list[tuple[date, float]], transform: str,
                     round_dp: int = 2) -> dict | None:
    """latest transformed reading + changes over 1m/3m/1y. None when the
    series is empty after transformation."""
    series = transform_series(sorted(obs), transform)
    if not series:
        return None
    latest_date, latest = series[-1]

    def chg(days: int):
        base = _value_at(series, latest_date - timedelta(days=days))
        return None if base is None else round(latest - base, round_dp)

    return {"latest": round(latest, round_dp),
            "as_of": latest_date.isoformat(),
            "chg_1m": chg(30), "chg_3m": chg(91), "chg_1y": chg(365)}


# --------------------------------------------------------------------------
# fetch + store
# --------------------------------------------------------------------------

async def fetch_series(client: httpx.AsyncClient, series_id: str,
                       start: date, key: str) -> tuple[list, str]:
    """Returns (observations, backend_name)."""
    if key:
        r = await client.get(FRED_API_URL.format(sid=series_id, key=key,
                                                 start=start.isoformat()))
        r.raise_for_status()
        return parse_fred_api_json(r.json(), series_id), "fred-api"
    r = await client.get(FREDGRAPH_URL.format(sid=series_id,
                                              start=start.isoformat()))
    r.raise_for_status()
    return parse_fredgraph_csv(r.text, series_id), "fredgraph"


async def last_obs_date(series_id: str) -> date | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT max(obs_date) FROM news.macro_series WHERE series_id=%s",
            (series_id,))
        return (await cur.fetchone())[0]


async def upsert_series(series_id: str, rows: list[tuple[date, float]],
                        source: str) -> int:
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO news.macro_series
                     (series_id, obs_date, value, source)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (series_id, obs_date)
                   DO UPDATE SET value = EXCLUDED.value,
                                 source = EXCLUDED.source,
                                 fetched_ts = now()""",
                [(series_id, d, v, source) for d, v in rows])
    return len(rows)


async def run_refresh(cfg: dict, fetch=None) -> dict:
    """Returns the stats dict it journals. fetch: tests inject
    `async def fetch(series_id, start) -> (rows, backend)`."""
    run_date = utcnow().strftime("%Y-%m-%d")
    pcfg = cfg.get("provider") or {}
    scfg = cfg.get("store") or {}
    key = os.environ.get(pcfg.get("key_env", "FRED_KEY"), "").strip()
    backfill_start = (utcnow().date()
                      - timedelta(days=int(365 * float(scfg.get("backfill_years", 5)))))
    overlap = timedelta(days=int(scfg.get("refetch_overlap_days", 14)))
    pause = float(pcfg.get("pause_secs", 1.0))

    per_series: dict[str, int] = {}
    failures: dict[str, str] = {}
    backend_used = "injected" if fetch else ("fred-api" if key else "fredgraph")

    client = None if fetch else httpx.AsyncClient(
        timeout=float(pcfg.get("timeout_secs", 30)), follow_redirects=True)
    try:
        for spec in cfg.get("series") or []:
            sid = spec["id"]
            try:
                last = await last_obs_date(sid)
                start = (last - overlap) if last else backfill_start
                if fetch:
                    rows, backend_used = await fetch(sid, start)
                else:
                    rows, backend_used = await fetch_series(client, sid,
                                                            start, key)
                per_series[sid] = await upsert_series(sid, rows, backend_used)
            except Exception as e:                    # noqa: BLE001 — isolate per series
                failures[sid] = repr(e)[:150]
                log.warning("macro series failed",
                            extra=kv(series=sid, error=repr(e)[:150]))
            if not fetch:
                await asyncio.sleep(pause)
    finally:
        if client is not None:
            await client.aclose()

    total = sum(per_series.values())
    n_cfg = len(cfg.get("series") or [])
    stats = {"run_date": run_date, "ok": bool(per_series), "backend": backend_used,
             "series_ok": len(per_series), "series_failed": len(failures),
             "rows_upserted": total, "per_series": per_series,
             "failures": failures}

    if not per_series and n_cfg:
        await set_health("macro", "DEGRADED",
                         f"refresh failed for all {n_cfg} series")
        await write_decision(
            signal_id=f"macro-{run_date}", stage="SYSTEM", agent="C1",
            action="MACRO_REFRESH_FAILED", payload=stats,
            reason="macro refresh failed for every series — A5 runs "
                   "without a macro block (pre-v0.12.24 behavior)")
        log.error("macro refresh failed entirely", extra=kv(**{
            "failed": len(failures)}))
        return stats

    await write_decision(
        signal_id=f"macro-{run_date}", stage="SYSTEM", agent="C1",
        action="MACRO_REFRESH", payload=stats,
        reason=f"macro series refreshed: {len(per_series)}/{n_cfg} series, "
               f"{total} rows ({backend_used})")
    await set_health("macro", "OK",
                     f"{len(per_series)}/{n_cfg} series @ {run_date} "
                     f"({backend_used})")
    log.info("macro refresh done",
             extra=kv(run_date=run_date, series_ok=len(per_series),
                      series_failed=len(failures), rows=total,
                      backend=backend_used))
    return stats


# --------------------------------------------------------------------------
# lookup — the A5 context block (defensive: any error -> None)
# --------------------------------------------------------------------------

async def macro_context(cfg: dict | None = None) -> dict | None:
    try:
        cfg = cfg or load_yaml(config_path("macro.yaml"))
        ccfg = cfg.get("context") or {}
        stale_days = int(ccfg.get("stale_after_days", 45))
        round_dp = int(ccfg.get("round_dp", 2))
        today = utcnow().date()

        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT series_id, obs_date, value FROM news.macro_series
                   ORDER BY series_id, obs_date""")
            rows = await cur.fetchall()
        by_sid: dict[str, list[tuple[date, float]]] = {}
        for sid, d, v in rows:
            by_sid.setdefault(sid, []).append((d, float(v)))
        if not by_sid:
            return None

        groups: dict[str, list[dict]] = {}
        stale: list[str] = []
        for spec in cfg.get("series") or []:
            sid = spec["id"]
            obs = by_sid.get(sid)
            if not obs:
                continue
            if (today - obs[-1][0]).days > stale_days:
                stale.append(sid)
                continue
            feats = compute_features(obs, spec.get("transform", "level"),
                                     round_dp)
            if feats is None:
                continue
            groups.setdefault(spec.get("group", "other"), []).append(
                {"label": spec.get("label", sid), "unit": spec.get("unit", ""),
                 **feats})

        if not groups:
            return None
        ordered = {g: groups[g] for g in GROUP_ORDER if g in groups}
        for g in groups:                       # anything unordered goes last
            ordered.setdefault(g, groups[g])
        out = {"as_of": today.isoformat(), "groups": ordered,
               "note": ("changes are over ~1 month / 3 months / 1 year; "
                        "YoY series report the year-over-year rate and "
                        "moves in it (percentage points)")}
        if stale:
            out["stale_excluded"] = stale
        return out
    except Exception as e:                     # noqa: BLE001 — degrade, never fail
        log.warning("macro_context degraded to None",
                    extra=kv(error=repr(e)[:200]))
        return None


async def main() -> None:
    from common.db import close_pool
    cfg = load_yaml(config_path("macro.yaml"))
    await register_config_version("macro refresh run")
    try:
        await run_refresh(cfg)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
