"""v0.10.0 integration against real PostgreSQL 16: earnings-calendar
refresh + the A3/A2 wiring.

Covers: refresh over an injected provider payload (upsert + re-run
idempotency + prune + EARNINGS_REFRESH journal row + earnings health OK);
provider failure (FAILED journal row + DEGRADED health, lookups degrade to
None — the pre-v0.10.0 EARNINGS_UNKNOWN behavior); A3's
earnings_next_sessions live against seeded rows (blackout-range value for a
next-session report, None for unknown tickers); A2 context helpers."""
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
from c1_ingestion.earnings import (earnings_next_sessions, next_report,
                                   run_refresh, sessions_until)
from a3_risk.service import earnings_next_sessions as a3_lookup
from a2_analyst.context import _earnings_date, _earnings_sessions

pytestmark = pytest.mark.asyncio(loop_scope="session")

CFG = {"provider": {"name": "alphavantage"}, "store": {"prune_days": 7}}


def next_nyse_session(after: date) -> date:
    import pandas_market_calendars as mcal
    sched = mcal.get_calendar("NYSE").schedule(
        start_date=(after + timedelta(days=1)).isoformat(),
        end_date=(after + timedelta(days=10)).isoformat())
    return sched.index[0].date()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.health, news.earnings_calendar
                     RESTART IDENTITY CASCADE""")
    await register_config_version("v0.10.0 earnings integration test")
    return {"pool": pool}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args or None)
        return await cur.fetchall()


def fake_csv(today: date) -> str:
    nxt = next_nyse_session(today)
    far = today + timedelta(days=45)
    old = today - timedelta(days=10)
    return ("symbol,name,reportDate,fiscalDateEnding,estimate,currency\r\n"
            f"ACME,Acme Corp,{nxt.isoformat()},2026-06-30,1.10,USD\r\n"
            f"NVDA,NVIDIA,{far.isoformat()},2026-07-31,,USD\r\n"
            f"OLDX,Stale Row,{old.isoformat()},2026-03-31,0.2,USD\r\n")


async def test_01_refresh_upserts_journals_and_prunes(env):
    today = utcnow().date()

    async def fetch():
        return fake_csv(today)

    stats = await run_refresh(CFG, fetch=fetch)
    assert stats["ok"] and stats["rows_upserted"] == 3

    # the stale row was pruned right after upsert
    rows = await q(env, "SELECT ticker FROM news.earnings_calendar "
                        "ORDER BY ticker")
    assert [r[0] for r in rows] == ["ACME", "NVDA"]

    dec = await q(env, """SELECT action FROM journal.decisions
                          WHERE stage='SYSTEM' AND agent='C1'""")
    assert ("EARNINGS_REFRESH",) in dec
    health = await q(env, "SELECT status FROM journal.health "
                          "WHERE component='earnings'")
    assert health == [("OK",)]

    # re-run: PK conflict -> update, same visible state
    stats2 = await run_refresh(CFG, fetch=fetch)
    assert stats2["rows_upserted"] == 3
    rows2 = await q(env, "SELECT count(*) FROM news.earnings_calendar")
    assert rows2[0][0] == 2


async def test_02_lookups_and_a3_a2_wiring(env):
    today = utcnow().date()
    nxt = next_nyse_session(today)

    got = await next_report("ACME")
    assert got == (nxt, "UNKNOWN")

    # next-session report -> inside the <=1 blackout window, through BOTH
    # the module function and A3's service-level hook
    n = await earnings_next_sessions("ACME")
    assert n == sessions_until(today, nxt) and n >= 0 and n <= 1
    assert await a3_lookup("ACME") == n

    far = await earnings_next_sessions("NVDA")
    assert far is not None and far > 5          # far report: no blackout

    assert await a3_lookup("ZZZQ") is None      # unknown ticker: flag path

    # A2 context helpers (defensive wrappers)
    assert await _earnings_date("ACME") == nxt.isoformat()
    assert await _earnings_sessions("ACME") == n
    assert await _earnings_date("ZZZQ") is None


async def test_03_provider_failure_degrades_not_breaks(env):
    async def bad_fetch():
        raise RuntimeError("simulated: ALPHAVANTAGE_KEY not set")

    stats = await run_refresh(CFG, fetch=bad_fetch)
    assert stats["ok"] is False

    dec = await q(env, """SELECT count(*) FROM journal.decisions
                          WHERE action='EARNINGS_REFRESH_FAILED'""")
    assert dec[0][0] == 1
    health = await q(env, "SELECT status FROM journal.health "
                          "WHERE component='earnings'")
    assert health == [("DEGRADED",)]

    # existing rows still serve lookups; a wiped table degrades to None
    assert await a3_lookup("ACME") is not None
    async with env["pool"].connection() as c:
        await c.execute("TRUNCATE news.earnings_calendar")
    assert await a3_lookup("ACME") is None       # EARNINGS_UNKNOWN path
