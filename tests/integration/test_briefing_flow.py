"""Phase 9 integration against real PostgreSQL 16: the A8 consolidated
morning briefing over a seeded journal.

Covers: full briefing assembly (A4 sheet embedded with re-fetched
headlines, thesis store, open position with earnings clock + A6
recommendation attached, ops section) -> MORNING_BRIEFING outbox row +
BRIEFING anchor; same-date rerun no-op; degraded morning (no A4 row for
the date, invalid narrative) still ships a visibly-degraded email; and the
v0.11.0 A4 email consolidation (report.email default False: SHEET decision
written, no outbox row; True restores the old behavior)."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"
os.environ["BROKER"] = "fake"

from common.clock import utcnow
from common.db import get_pool, jb
from common.journal import register_config_version, write_decision
from a1_triage.backends import StubBackend
from a4_premarket.service import run_premarket
from a8_briefing.service import run_briefing
from c1_ingestion.heartbeat import set_health

pytestmark = pytest.mark.asyncio(loop_scope="session")

B1 = datetime(2026, 7, 21, 11, 35, tzinfo=timezone.utc)   # Tue 07:35 ET
B2 = datetime(2026, 7, 22, 11, 35, tzinfo=timezone.utc)   # Wed 07:35 ET
P1 = datetime(2026, 7, 23, 11, 0, tzinfo=timezone.utc)    # Thu 07:00 ET
P2 = datetime(2026, 7, 24, 11, 0, tzinfo=timezone.utc)    # Fri 07:00 ET

CFG = {"report": {"send_on_nonsession": True},
       "briefing": {"blackout_warn_sessions": 2},
       "narrative": {"retries_on_invalid": 1},
       "heavy": {}, "analyst_fallback": {"enabled": False}}

A4_CFG = {"report": {"send_on_nonsession": True},
          "sheet": {"max_age_hours": 72, "top_k": 15, "fallback_open_k": 2,
                    "blackout_min": 15, "batch_max": 300},
          "narrative": {"retries_on_invalid": 1}}


def narrative_reply():
    return json.dumps({"summary": "Trim NVDA before its report; one fresh "
                                  "candidate worth the open.",
                       "watch_items": ["NVDA reports within the blackout "
                                       "window"]})


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.intents, journal.orders, journal.fills,
                     journal.positions, journal.position_events,
                     journal.exits, journal.guard_ledger, journal.outbox,
                     journal.theses, journal.thesis_evidence, journal.health,
                     news.cluster_members, news.clusters, news.news_items,
                     news.earnings_calendar, queue.messages
                     RESTART IDENTITY CASCADE""")
    await register_config_version("phase9 briefing integration test")
    return {"pool": pool}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args or None)
        return await cur.fetchall()


def next_nyse_session(after):
    import pandas_market_calendars as mcal
    sched = mcal.get_calendar("NYSE").schedule(
        start_date=(after + timedelta(days=1)).isoformat(),
        end_date=(after + timedelta(days=10)).isoformat())
    return sched.index[0].date()


async def seed_world(env):
    """A4 sheet for B1's date + news item + thesis + position + A6 review +
    earnings row inside the blackout window + one DEGRADED health row."""
    async with env["pool"].connection() as c:
        ts = utcnow()
        await c.execute(
            """INSERT INTO news.news_items
               (item_id, revision, source, source_tier, headline, summary,
                content_hash, symbols, published_ts, received_ts)
               VALUES ('n:1',1,'alpaca_benzinga',2,'Acme wins supply deal',
                       's','h-n1',ARRAY['ACME'],%s,%s)""", (ts, ts))
    await write_decision(
        signal_id="premarket-2026-07-21", stage="PREMARKET", agent="A4",
        action="SHEET",
        payload={"session_date": "2026-07-21", "fresh": 5,
                 "open_candidates": 1, "guard_routed": 0, "thesis_routed": 2,
                 "ignored": 2, "expired_bulk": 10, "slot": "heavy",
                 "entry_ts": "2026-07-21T13:45:00+00:00",
                 "sheet": {"summary": "One actionable story.", "items": []},
                 "open_forwarded": [{"item_id": "n:1", "tickers": ["ACME"],
                                     "rank": 1}]})
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO journal.theses (thesis_id, title, driver,
                 direction, horizon, confidence, beneficiaries, invalidation,
                 config_version)
               SELECT 'th-2026-001','Grid capex supercycle','grid spend',
                      'up','LONG',0.55,
                      '[{"ticker":"VRT","relation":"pure play","rationale":"backlog"}]'::jsonb,
                      '[]'::jsonb, config_version
               FROM journal.config_versions LIMIT 1""")
        # position chain (real FKs)
        cur = await c.execute(
            """INSERT INTO journal.decisions
                 (signal_id, stage, agent, action, ticker, payload, reason,
                  config_version)
               SELECT 'seed:NVDA','ANALYST','A2','THESIS','NVDA',%s,'t',
                      config_version FROM journal.config_versions LIMIT 1
               RETURNING decision_id""",
            (jb({"thesis": {"ticker": "NVDA", "magnitude_est": 0.06}}),))
        thesis_id = (await cur.fetchone())[0]
        await c.execute(
            """INSERT INTO journal.intents
                 (intent_id, decision_id, ticker, side, qty, limit_price,
                  config_version)
               SELECT 'it-NVDA-1',%s,'NVDA','BUY',50,100.0, config_version
               FROM journal.config_versions LIMIT 1""", (thesis_id,))
        await c.execute(
            """INSERT INTO journal.positions
                 (ticker, horizon, profile, status, opened_ts,
                  entry_intent_id, thesis_decision_id, item_id, qty_initial,
                  qty_open, avg_entry, initial_stop, r_unit, exit_policy,
                  last_price, last_price_ts, config_version)
               SELECT 'NVDA','SHORT','short_term_v1','OPEN',
                      now() - interval '2 days','it-NVDA-1',%s,'seed',50,50,
                      100.0,96.0,4.0,%s,104.0,now(), config_version
               FROM journal.config_versions LIMIT 1""",
            (thesis_id, jb({"profile": "short_term_v1",
                            "current_stop": 100.0})))
        await c.execute(
            """INSERT INTO news.earnings_calendar
                 (ticker, report_date, source)
               VALUES ('NVDA', %s, 'test')""",
            (next_nyse_session(utcnow().date()),))
    await write_decision(
        signal_id="posrev-2026-07-20", stage="POSITION_REVIEW", agent="A6",
        action="REVIEW",
        payload={"run_date": "2026-07-20", "reviewed": 1,
                 "recommendations": 1, "stale_flagged": 0, "holds": 0,
                 "slot": "heavy",
                 "recos": [{"position_id": 1, "ticker": "NVDA",
                            "action": "TRIM_RECO",
                            "rationale": "move mostly realized"}]})
    await set_health("earnings", "DEGRADED", "test degradation")


async def test_01_full_briefing_ships_one_email(env):
    await seed_world(env)
    outbox_id = await run_briefing(
        CFG, backend_override=StubBackend([narrative_reply()]), now=B1)
    assert outbox_id is not None

    rows = await q(env, """SELECT kind, subject, body FROM journal.outbox
                           WHERE message_id=%s""", outbox_id)
    kind, subject, body = rows[0]
    assert kind == "MORNING_BRIEFING"
    assert "1 candidate" in subject and "1 position reco" in subject
    assert "Acme wins supply deal" in body          # A4 sheet + headline
    assert "A6 recommends TRIM_RECO" in body        # last night's review
    assert "EARNINGS in 1 session" in body          # blackout clock
    assert "th-2026-001" in body                    # thesis store
    assert "HEALTH DEGRADED: earnings" in body      # ops section
    assert "Trim NVDA" in body                      # narrative

    anchors = await q(env, """SELECT count(*) FROM journal.decisions
                              WHERE stage='SYSTEM' AND agent='A8'
                                AND action='BRIEFING'""")
    assert anchors[0][0] == 1
    # same-date rerun -> no-op
    assert await run_briefing(
        CFG, backend_override=StubBackend([narrative_reply()]),
        now=B1) is None


async def test_02_degraded_morning_still_ships(env):
    # B2's date has no A4 SHEET row; narrative invalid twice
    bad = StubBackend(["not json", "still not json"])
    outbox_id = await run_briefing(CFG, backend_override=bad, now=B2)
    assert outbox_id is not None
    rows = await q(env, """SELECT body FROM journal.outbox
                           WHERE message_id=%s""", outbox_id)
    body = rows[0][0]
    assert "not available yet this morning" in body
    assert "(narrative unavailable" in body


async def test_03_a4_email_consolidated_into_a8(env):
    # default (email absent -> False): SHEET decision, no outbox row
    before = (await q(env, "SELECT count(*) FROM journal.outbox"))[0][0]
    assert await run_premarket(A4_CFG, backend_override=StubBackend([]),
                               now=P1) is None
    sheets = await q(env, """SELECT count(*) FROM journal.decisions
                             WHERE stage='PREMARKET' AND action='SHEET'
                               AND payload->>'session_date'='2026-07-23'""")
    assert sheets[0][0] == 1
    after = (await q(env, "SELECT count(*) FROM journal.outbox"))[0][0]
    assert after == before

    # email: true restores the standalone sheet email
    cfg = {**A4_CFG, "report": {**A4_CFG["report"], "email": True}}
    outbox_id = await run_premarket(cfg, backend_override=StubBackend([]),
                                    now=P2)
    assert outbox_id is not None
