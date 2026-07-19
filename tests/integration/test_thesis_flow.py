"""Phase 8 integration against real PostgreSQL 16: A5 thematic pass over a
seeded thesis lane.

Covers: new-thesis creation (code-minted id, anchor evidence row, watchlist
view + router thesis_matches activation); evidence attachment with the
evidence-clock update; ignore lane; bulk expiry of the stale backlog;
deterministic staleness expiry (code rule, no model); digest decision +
ALERT outbox row; same-date rerun no-op; no-slot skip leaves the lane
unclaimed; invalid model output releases claims and writes no digest."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"
os.environ["BROKER"] = "fake"

from common.clock import utcnow
from common.db import get_pool
from common.journal import register_config_version
from common.queue import enqueue
from a1_triage.backends import StubBackend
from a5_thematic.service import run_thematic
from router.facts import thesis_matches

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Mon/Tue/Wed/Thu ET evenings (deep=False), one date per test.
D1 = datetime(2026, 7, 7, 1, 30, tzinfo=timezone.utc)    # Mon 21:30 ET
D2 = datetime(2026, 7, 8, 1, 30, tzinfo=timezone.utc)    # Tue 21:30 ET
D3 = datetime(2026, 7, 9, 1, 30, tzinfo=timezone.utc)    # Wed 21:30 ET
D4 = datetime(2026, 7, 10, 1, 30, tzinfo=timezone.utc)   # Thu 21:30 ET
D5 = datetime(2026, 7, 11, 1, 30, tzinfo=timezone.utc)   # Fri 21:30 ET
D6 = datetime(2026, 7, 14, 1, 30, tzinfo=timezone.utc)   # Mon 21:30 ET

CFG = {"lane": {"max_age_hours": 168, "top_k": 25, "deep_top_k": 60},
       "store": {"stale_weeks": 6},
       "digest": {"email": True},
       "narrative": {"retries_on_invalid": 1},
       "heavy": {}, "analyst_fallback": {"enabled": False}}


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.theses, journal.thesis_evidence, journal.outbox,
                     news.cluster_members, news.clusters, news.news_items,
                     queue.messages
                     RESTART IDENTITY CASCADE""")
        await c.execute("ALTER SEQUENCE journal.thesis_seq RESTART WITH 1")
    await register_config_version("phase8 thesis integration test")
    return {"pool": pool}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args or None)
        return await cur.fetchall()


async def seed_item(env, item_id, headline, received, summary="s"):
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO news.news_items
               (item_id, revision, source, source_tier, headline, summary,
                content_hash, symbols, published_ts, received_ts)
               VALUES (%s,1,'rss:wire',3,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
            (item_id, headline, summary, f"h-{item_id}", [],
             received, received))


def thesis_payload(item_id, tickers=(), reason="macro item"):
    return {"envelope": {"msg_schema": "signal.triaged/1", "producer": "A1",
                         "trace": {"signal_id": item_id, "item_id": item_id,
                                   "revision": 1}},
            "body": {"item_ref": {"item_id": item_id, "revision": 1,
                                  "cluster_id": 1},
                     "triage": {"material": True, "tickers": list(tickers),
                                "direction_hint": "up", "urgency": "low",
                                "novelty_score": 0.7, "confidence": 0.8,
                                "reason": reason}}}


async def seed_lane(env, item_id, received, tickers=()):
    await seed_item(env, item_id, f"headline {item_id}", received)
    await enqueue("signal.thesis", f"{item_id}:1",
                  thesis_payload(item_id, tickers))


def stub(reply: dict) -> StubBackend:
    return StubBackend([json.dumps(reply)])


NEW_THESIS_REPLY = {
    "items": [],
    "new_theses": [{
        "anchor_item_id": "m:1",
        "title": "Grid capex supercycle",
        "driver": "Structural utility grid spend on load growth; multi-year "
                  "equipment backlogs.",
        "direction": "up", "horizon": "LONG", "confidence": 0.55,
        "beneficiaries": [
            {"ticker": "VRT", "relation": "pure play",
             "rationale": "grid equipment backlog"},
            {"ticker": "ETN", "relation": "diversified supplier",
             "rationale": "electrical segment leverage"}],
        "invalidation": ["utility capex guidance cuts"]}],
    "reviews": [],
    "summary": "One durable theme emerged from tonight's lane."}


async def test_01_new_thesis_created_and_watchlist_live(env):
    await seed_lane(env, "m:1", D1 - timedelta(hours=5))
    await seed_lane(env, "m:2", D1 - timedelta(hours=4))   # will be ignored
    reply = dict(NEW_THESIS_REPLY)
    reply["items"] = [{"item_id": "m:2", "op": "ignore",
                       "note": "one-off commentary"}]
    outbox_id = await run_thematic(CFG, backend_override=stub(reply), now=D1)
    assert outbox_id is not None

    rows = await q(env, """SELECT thesis_id, status, confidence,
                                  evidence_count FROM journal.theses""")
    assert rows == [("th-2026-001", "ACTIVE", pytest.approx(0.55), 1)]
    ev = await q(env, """SELECT item_id, polarity FROM journal.thesis_evidence
                         WHERE thesis_id='th-2026-001'""")
    assert ev == [("m:1", "SUPPORTS")]

    acts = dict(await q(env, """SELECT action, count(*)
                                FROM journal.decisions
                                WHERE stage='THEMATIC'
                                GROUP BY action"""))
    assert acts["NEW_THESIS"] == 1 and acts["IGNORE"] == 1
    assert acts["DIGEST"] == 1

    # queue drained; watchlist + router fact live
    depth = await q(env, """SELECT count(*) FROM queue.messages
                            WHERE queue_name='signal.thesis'
                              AND done_ts IS NULL""")
    assert depth[0][0] == 0
    wl = await q(env, "SELECT ticker, thesis_id FROM journal.thesis_watchlist "
                      "ORDER BY ticker")
    assert wl == [("ETN", "th-2026-001"), ("VRT", "th-2026-001")]
    assert await thesis_matches(["VRT"]) == ["th-2026-001"]
    assert await thesis_matches(["ZZZ"]) == []

    ob = await q(env, "SELECT kind, subject FROM journal.outbox")
    assert ob[0][0] == "ALERT" and "Thesis digest" in ob[0][1]


async def test_02_evidence_attach_updates_clock_and_rerun_noop(env):
    await seed_lane(env, "m:3", D2 - timedelta(hours=3))
    reply = {"items": [{"item_id": "m:3", "op": "evidence",
                        "thesis_id": "th-2026-001",
                        "polarity": "contradicts",
                        "note": "capex pushback from a major utility"}],
             "new_theses": [], "reviews": [],
             "summary": "Contradicting evidence logged."}
    await run_thematic(CFG, backend_override=stub(reply), now=D2)

    rows = await q(env, """SELECT evidence_count, last_evidence_ts
                           FROM journal.theses
                           WHERE thesis_id='th-2026-001'""")
    assert rows[0][0] == 2 and rows[0][1] is not None
    ev = await q(env, """SELECT polarity FROM journal.thesis_evidence
                         WHERE item_id='m:3'""")
    assert ev == [("CONTRADICTS",)]

    # same-date rerun: DIGEST anchor -> no-op
    assert await run_thematic(CFG, backend_override=stub(reply),
                              now=D2) is None


async def test_03_review_ops_and_staleness_expiry(env):
    # a second thesis, then invalidate it by model review; the first thesis
    # goes stale via the code rule (backdated evidence clock).
    async with env["pool"].connection() as c:
        await c.execute(
            """UPDATE journal.theses SET last_evidence_ts = %s, created_ts = %s
               WHERE thesis_id='th-2026-001'""",
            (D3 - timedelta(weeks=8), D3 - timedelta(weeks=10)))
        await c.execute(
            """INSERT INTO journal.theses (thesis_id, title, driver,
                 direction, horizon, confidence, beneficiaries, invalidation,
                 config_version)
               SELECT 'th-2026-002','Shipping rate spike','Red Sea rerouting',
                      'up','LONG',0.5,'[]'::jsonb,'[]'::jsonb, config_version
               FROM journal.config_versions LIMIT 1""")
    reply = {"items": [], "new_theses": [],
             "reviews": [{"thesis_id": "th-2026-002", "op": "invalidate",
                          "confidence": 0.2,
                          "note": "rates normalized; invalidation met"}],
             "summary": "Store maintenance night."}
    # deep pass forces the model call with an empty item list
    cfg = {**CFG, "lane": {**CFG["lane"], "force_deep": True}}
    await run_thematic(cfg, backend_override=stub(reply), now=D3)

    rows = dict(await q(env, "SELECT thesis_id, status FROM journal.theses"))
    assert rows["th-2026-001"] == "EXPIRED"        # code staleness rule
    assert rows["th-2026-002"] == "INVALIDATED"    # model review
    acts = [r[0] for r in await q(
        env, """SELECT action FROM journal.decisions
                WHERE stage='THEMATIC'
                  AND payload->>'run_date' = '2026-07-08'""")]
    assert "THESIS_EXPIRED" in acts and "THESIS_INVALIDATED" in acts
    assert await thesis_matches(["VRT"]) == []     # expired left the watchlist


async def test_04_no_slot_leaves_lane_unclaimed(env):
    await seed_lane(env, "m:4", D4 - timedelta(hours=2))
    assert await run_thematic(CFG, backend_override=None, now=D4) is None
    acts = [r[0] for r in await q(
        env, """SELECT action FROM journal.decisions
                WHERE stage='THEMATIC'
                  AND payload->>'run_date' = '2026-07-09'""")]
    assert acts == ["SKIPPED_NO_MODEL"]
    depth = await q(env, """SELECT count(*) FROM queue.messages
                            WHERE queue_name='signal.thesis'
                              AND done_ts IS NULL AND claimed_ts IS NULL""")
    assert depth[0][0] == 1                        # m:4 still waiting


async def test_05_invalid_model_releases_claims_no_digest(env):
    bad = StubBackend(["not json", "still not json"])
    assert await run_thematic(CFG, backend_override=bad, now=D5) is None
    acts = [r[0] for r in await q(
        env, """SELECT action FROM journal.decisions
                WHERE stage='THEMATIC'
                  AND payload->>'run_date' = '2026-07-10'""")]
    assert "REJECT" in acts and "DIGEST" not in acts
    depth = await q(env, """SELECT count(*) FROM queue.messages
                            WHERE queue_name='signal.thesis'
                              AND done_ts IS NULL AND claimed_ts IS NULL""")
    assert depth[0][0] == 1                        # released for tomorrow


async def test_06_quiet_night_digests_without_any_slot(env):
    # drain the lane left over from test 05, then run a quiet non-deep night
    async with env["pool"].connection() as c:
        await c.execute("""UPDATE queue.messages SET done_ts = now()
                           WHERE queue_name='signal.thesis'
                             AND done_ts IS NULL""")
    # backend_override=None + empty slot config: would be SKIPPED_NO_MODEL
    # if there were work — but an empty lane must digest quietly instead
    # (and must NOT try to start the heavy model).
    await run_thematic(CFG, backend_override=None, now=D6)
    acts = [r[0] for r in await q(
        env, """SELECT action FROM journal.decisions
                WHERE stage='THEMATIC'
                  AND payload->>'run_date' = '2026-07-13'""")]
    assert acts == ["DIGEST"]
