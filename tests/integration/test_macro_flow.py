"""v0.12.24 integration against real PostgreSQL 16: macro series refresh +
the A5 context block.

Covers: refresh over an injected fetch (backfill upsert; incremental
re-fetch windows; revision overwrite via ON CONFLICT; MACRO_REFRESH journal
row + 'macro' health OK); per-series failure isolation (one bad series
doesn't kill the run); total failure (MACRO_REFRESH_FAILED + DEGRADED,
never a crash); macro_context() live against seeded rows (grouping, YoY
transform, staleness exclusion) and its None degradation on an empty table.
"""
import os
from datetime import date, timedelta

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"
os.environ["BROKER"] = "fake"

from common.clock import utcnow
from common.db import get_pool
from common.journal import register_config_version
from c1_ingestion.macro import macro_context, run_refresh

pytestmark = pytest.mark.asyncio(loop_scope="session")

CFG = {
    "provider": {"key_env": "FRED_KEY_TEST_UNSET", "pause_secs": 0},
    "store": {"backfill_years": 2, "refetch_overlap_days": 14},
    "context": {"stale_after_days": 45, "round_dp": 2},
    "series": [
        {"id": "DGS10", "label": "10y Treasury yield", "group": "rates_curve",
         "freq": "d", "transform": "level", "unit": "%"},
        {"id": "CPIAUCSL", "label": "CPI (headline, YoY)", "group": "inflation",
         "freq": "m", "transform": "yoy_pct", "unit": "%"},
    ],
}


def _daily(latest: float, days: int = 40):
    """Recent daily observations ending yesterday."""
    end = utcnow().date() - timedelta(days=1)
    return [(end - timedelta(days=i), latest - 0.01 * i)
            for i in range(days)][::-1]


def _monthly_yoy(base: float, yoy: float, months: int = 15):
    """Monthly series whose latest YoY is ~`yoy` percent."""
    end = utcnow().date().replace(day=1)
    out = []
    for i in range(months, -1, -1):
        y, m = end.year, end.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        # growth applied over the final 12 months only
        v = base * (1 + yoy / 100.0) ** (max(0, months - i - 3) / 12.0)
        out.append((date(y, m, 1), round(v, 4)))
    return out


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.health, news.macro_series
                     RESTART IDENTITY CASCADE""")
    await register_config_version("v0.12.24 macro integration test")
    return {"pool": pool}


async def _health(pool, component):
    async with pool.connection() as c:
        cur = await c.execute(
            "SELECT status, detail FROM journal.health WHERE component=%s",
            (component,))
        return await cur.fetchone()


async def _decisions(pool, action):
    async with pool.connection() as c:
        cur = await c.execute(
            """SELECT payload FROM journal.decisions
               WHERE stage='SYSTEM' AND agent='C1' AND action=%s
               ORDER BY ts""", (action,))
        return [r[0] for r in await cur.fetchall()]


async def test_01_refresh_backfills_and_journals(env):
    dgs = _daily(4.25)
    cpi = _monthly_yoy(300.0, 3.0)

    async def fetch(sid, start):
        return {"DGS10": dgs, "CPIAUCSL": cpi}[sid], "stub"

    stats = await run_refresh(CFG, fetch=fetch)
    assert stats["ok"] and stats["series_failed"] == 0
    assert stats["per_series"]["DGS10"] == len(dgs)
    st, detail = await _health(env["pool"], "macro")
    assert st == "OK" and "2/2" in detail
    rows = await _decisions(env["pool"], "MACRO_REFRESH")
    assert len(rows) == 1 and rows[0]["rows_upserted"] > 0


async def test_02_rerun_is_idempotent_and_incremental(env):
    calls = {}

    async def fetch(sid, start):
        calls[sid] = start
        # provider re-sends the tail; ON CONFLICT refreshes in place
        return ({"DGS10": _daily(4.25)[-5:],
                 "CPIAUCSL": _monthly_yoy(300.0, 3.0)[-2:]}[sid], "stub")

    await run_refresh(CFG, fetch=fetch)
    pool = env["pool"]
    async with pool.connection() as c:
        cur = await c.execute(
            """SELECT count(*) FROM news.macro_series
               WHERE series_id='DGS10'""")
        n = (await cur.fetchone())[0]
    assert n == 40                                 # no duplicates
    # incremental: start = last obs - overlap, NOT the 2y backfill
    assert calls["DGS10"] >= utcnow().date() - timedelta(days=20)


async def test_03_one_bad_series_does_not_kill_the_run(env):
    async def fetch(sid, start):
        if sid == "CPIAUCSL":
            raise RuntimeError("provider hiccup")
        return _daily(4.30)[-3:], "stub"

    stats = await run_refresh(CFG, fetch=fetch)
    assert stats["ok"] is True
    assert stats["series_ok"] == 1 and stats["series_failed"] == 1
    assert "CPIAUCSL" in stats["failures"]
    st, _ = await _health(env["pool"], "macro")
    assert st == "OK"                              # partial success is OK


async def test_04_total_failure_degrades_never_crashes(env):
    async def fetch(sid, start):
        raise RuntimeError("FRED down")

    stats = await run_refresh(CFG, fetch=fetch)
    assert stats["ok"] is False
    st, _ = await _health(env["pool"], "macro")
    assert st == "DEGRADED"
    assert len(await _decisions(env["pool"], "MACRO_REFRESH_FAILED")) == 1


async def test_05_macro_context_groups_transforms_and_staleness(env):
    ctx = await macro_context(CFG)
    assert ctx is not None
    rates = ctx["groups"]["rates_curve"]
    assert rates[0]["label"] == "10y Treasury yield"
    assert 4.0 < rates[0]["latest"] < 4.5
    cpi = ctx["groups"]["inflation"][0]
    assert 1.0 < cpi["latest"] < 5.0               # a YoY rate, not an index level

    # staleness: age DGS10 out and it must be excluded + reported
    pool = env["pool"]
    async with pool.connection() as c:
        await c.execute(
            """UPDATE news.macro_series
               SET obs_date = obs_date - INTERVAL '120 days'
               WHERE series_id='DGS10'""")
    ctx2 = await macro_context(CFG)
    assert "rates_curve" not in ctx2["groups"]
    assert "DGS10" in ctx2.get("stale_excluded", [])


async def test_06_macro_context_none_on_empty_table(env):
    pool = env["pool"]
    async with pool.connection() as c:
        await c.execute("TRUNCATE news.macro_series")
    assert await macro_context(CFG) is None
