"""Phase 7 integration against real PostgreSQL 16: A4 pre-market review over
a seeded overnight queue.

Covers: bulk expiry of the stale backlog (one summary decision, no per-item
rows); code-first routing (held ticker -> signal.guard before any model
involvement; no-ticker -> signal.thesis); model-ranked candidates ->
signal.analyst with DELAYED available_ts (the 9:45 open handoff) and
payload forwarded verbatim (A2-compatible TriagedSignal); ignore lane
journaled with no route; MORNING_BRIEFING outbox row + SHEET decision;
same-day rerun no-op; deterministic fallback when the model output is
invalid; overnight queue fully drained."""
import json
import os
from datetime import timedelta

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"
os.environ["BROKER"] = "fake"

from common.clock import utcnow
from common.db import get_pool, jb
from common.journal import register_config_version
from a1_triage.backends import StubBackend
from a4_premarket.service import next_entry_ts, run_premarket

pytestmark = pytest.mark.asyncio(loop_scope="session")

CFG = {"report": {"send_on_nonsession": True},
       "sheet": {"max_age_hours": 72, "top_k": 15, "fallback_open_k": 2,
                 "blackout_min": 15, "batch_max": 300},
       "narrative": {"retries_on_invalid": 1}}


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.regime_snapshots, journal.intents, journal.orders,
                     journal.fills, journal.positions, journal.position_events,
                     journal.exits, journal.guard_ledger, journal.outbox,
                     journal.audit,
                     news.cluster_members, news.clusters, news.news_items,
                     queue.messages
                     RESTART IDENTITY CASCADE""")
    await register_config_version("phase7 premarket integration test")
    return {"pool": pool}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args or None)
        return await cur.fetchall()


async def seed_item(env, item_id, headline, age_hours=6.0, symbols=("ACME",),
                    tier=2):
    ts = utcnow() - timedelta(hours=age_hours)
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO news.news_items
               (item_id, revision, source, source_tier, headline, summary,
                content_hash, symbols, published_ts, received_ts)
               VALUES (%s,1,'alpaca_benzinga',%s,%s,'s',%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
            (item_id, tier, headline, f"h-{item_id}", list(symbols), ts, ts))


def overnight_payload(item_id, tickers, urgency="high"):
    return {"envelope": {"msg_schema": "signal.triaged/1", "producer": "A1",
                         "trace": {"signal_id": item_id, "item_id": item_id,
                                   "revision": 1}},
            "body": {"item_ref": {"item_id": item_id, "revision": 1,
                                  "cluster_id": 7},
                     "triage": {"material": True, "tickers": list(tickers),
                                "direction_hint": "up", "urgency": urgency,
                                "novelty_score": 0.7, "confidence": 0.9,
                                "reason": "overnight catalyst"},
                     "routing": {"market_open": False, "position_ids": [],
                                 "thesis_matches": [], "priority_score": 30}}}


async def seed_overnight_msg(env, item_id, tickers, priority=20,
                             age_hours=None):
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO queue.messages (queue_name, dedup_key, priority,
                                            payload, enqueued_ts)
               VALUES ('signal.overnight', %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (f"{item_id}:1", priority, jb(overnight_payload(item_id, tickers)),
             utcnow() - timedelta(hours=age_hours or 1)))


async def seed_position(env, ticker):
    async with env["pool"].connection() as c:
        cur = await c.execute(
            """INSERT INTO journal.decisions (signal_id, stage, agent, action,
                 ticker, config_version)
               SELECT %s,'ANALYST','A2','THESIS',%s, config_version
               FROM journal.config_versions LIMIT 1 RETURNING decision_id""",
            (f"seed:{ticker}", ticker))
        thesis_id = (await cur.fetchone())[0]
        await c.execute(
            """INSERT INTO journal.intents (intent_id, decision_id, ticker,
                 side, qty, limit_price, config_version)
               SELECT %s,%s,%s,'BUY',10,50.0, config_version
               FROM journal.config_versions LIMIT 1""",
            (f"it-{ticker}", thesis_id, ticker))
        await c.execute(
            """INSERT INTO journal.positions (ticker, horizon, profile,
                 status, opened_ts, entry_intent_id, thesis_decision_id,
                 qty_initial, qty_open, avg_entry, initial_stop, r_unit,
                 exit_policy, config_version)
               SELECT %s,'SHORT','short_term_v1','OPEN', now(), %s, %s,
                      10,10,50.0,48.0,2.0,'{}'::jsonb, config_version
               FROM journal.config_versions LIMIT 1""",
            (ticker, f"it-{ticker}", thesis_id))


SHEET = json.dumps({
    "items": [
        {"item_id": "n:cand-1", "lane": "open_candidate", "rank": 1,
         "rationale": "corroborated acquisition, likely follow-through"},
        {"item_id": "n:cand-2", "lane": "thesis", "rank": 2,
         "rationale": "structural sector shift, not an open trade"},
        {"item_id": "n:cand-3", "lane": "ignore", "rank": 3,
         "rationale": "recap of Friday's move"},
    ],
    "summary": "Moderate overnight flow; one actionable name."})


async def test_01_full_premarket_run(env):
    # stale backlog (beyond 72h)
    for i in range(5):
        await seed_item(env, f"n:old-{i}", f"Old {i}", age_hours=200)
        await seed_overnight_msg(env, f"n:old-{i}", ["OLD"], age_hours=100)
    # held ticker -> guard lane (code, pre-model)
    await seed_position(env, "HELD")
    await seed_item(env, "n:held-1", "Held name misses earnings",
                    symbols=("HELD",))
    await seed_overnight_msg(env, "n:held-1", ["HELD"], priority=10)
    # no ticker -> thesis lane (code)
    await seed_item(env, "n:macro-1", "Fed speaker on rate path", symbols=())
    await seed_overnight_msg(env, "n:macro-1", [], priority=30)
    # three model-ranked candidates
    for iid, hl in (("n:cand-1", "Acme to be acquired"),
                    ("n:cand-2", "Sector regulation overhaul"),
                    ("n:cand-3", "Stock recap Friday")):
        await seed_item(env, iid, hl, symbols=("ACME",))
        await seed_overnight_msg(env, iid, ["ACME"], priority=20)

    outbox_id = await run_premarket(CFG, backend_override=StubBackend([SHEET]))
    assert outbox_id is not None

    # bulk expiry: one summary decision, stale messages done
    bulk = await q(env, """SELECT payload FROM journal.decisions
                           WHERE stage='PREMARKET' AND action='EXPIRED_BULK'""")
    assert len(bulk) == 1 and bulk[0][0]["expired_count"] == 5

    # guard routing happened in code with priority 0
    g = await q(env, """SELECT priority FROM queue.messages
                        WHERE queue_name='signal.guard'
                          AND dedup_key='n:held-1:1:a4guard'""")
    assert g == [(0,)]
    assert await q(env, """SELECT count(*) FROM journal.decisions
                           WHERE stage='PREMARKET' AND action='GUARD'""") == [(1,)]

    # thesis lane: macro item + model-assigned cand-2
    t = await q(env, """SELECT dedup_key FROM queue.messages
                        WHERE queue_name='signal.thesis' ORDER BY dedup_key""")
    assert [r[0] for r in t] == ["n:cand-2:1:a4thesis", "n:macro-1:1:a4thesis"]

    # open candidate: delayed analyst enqueue, payload forwarded verbatim
    a = await q(env, """SELECT payload, available_ts, priority
                        FROM queue.messages
                        WHERE queue_name='signal.analyst'
                          AND dedup_key='n:cand-1:1:handoff'""")
    assert len(a) == 1
    payload, available_ts, priority = a[0]
    assert payload["body"]["triage"]["tickers"] == ["ACME"]     # A2-compatible
    assert available_ts == next_entry_ts(blackout_min=15)
    assert priority == 41                                        # 40 + rank 1

    # ignore lane journaled, no route
    ig = await q(env, """SELECT count(*) FROM journal.decisions
                         WHERE stage='PREMARKET' AND action='IGNORE'""")
    assert ig == [(1,)]

    # briefing email queued
    ob = await q(env, """SELECT kind, subject, body FROM journal.outbox
                         WHERE message_id=%s""", outbox_id)
    kind, subject, body = ob[0]
    assert kind == "MORNING_BRIEFING"
    assert "1 open candidate" in subject
    assert "Acme to be acquired" in body
    assert "Held name misses earnings" in body
    assert "5 stale" in body

    # overnight queue fully drained
    left = await q(env, """SELECT count(*) FROM queue.messages
                           WHERE queue_name='signal.overnight'
                             AND done_ts IS NULL""")
    assert left == [(0,)]


async def test_02_same_day_rerun_is_noop(env):
    assert await run_premarket(CFG, backend_override=StubBackend([SHEET])) is None
    assert await q(env, """SELECT count(*) FROM journal.outbox
                           WHERE kind='MORNING_BRIEFING'""") == [(1,)]


async def test_03_fallback_when_model_invalid(env):
    # clear the day's sheet so a fresh run happens
    async with env["pool"].connection() as c:
        await c.execute("""DELETE FROM journal.outbox
                           WHERE kind='MORNING_BRIEFING';
                           DELETE FROM journal.decisions
                           WHERE stage='PREMARKET' AND action='SHEET'""")
    await seed_item(env, "n:fb-1", "Fallback story A", symbols=("FBA",))
    await seed_overnight_msg(env, "n:fb-1", ["FBA"], priority=5)
    await seed_item(env, "n:fb-2", "Fallback story B", symbols=("FBB",))
    await seed_overnight_msg(env, "n:fb-2", ["FBB"], priority=6)
    await seed_item(env, "n:fb-3", "Fallback story C", symbols=("FBC",))
    await seed_overnight_msg(env, "n:fb-3", ["FBC"], priority=7)

    outbox_id = await run_premarket(
        CFG, backend_override=StubBackend(["bad", "still bad"]))
    assert outbox_id is not None

    # fallback_open_k=2: two highest-priority become open candidates
    a = await q(env, """SELECT dedup_key FROM queue.messages
                        WHERE queue_name='signal.analyst'
                          AND dedup_key LIKE 'n:fb-%' ORDER BY priority""")
    assert [r[0] for r in a] == ["n:fb-1:1:handoff", "n:fb-2:1:handoff"]
    ob = await q(env, """SELECT body FROM journal.outbox
                         WHERE message_id=%s""", outbox_id)
    assert "model offline" in ob[0][0]
    sheet_dec = await q(env, """SELECT payload FROM journal.decisions
                                WHERE stage='PREMARKET' AND action='SHEET'""")
    assert sheet_dec[0][0]["slot"] == "fallback"
