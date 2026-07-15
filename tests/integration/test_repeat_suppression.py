"""v0.4.7 integration: story-level repeat suppression (A1) + revision policy
(C2) + confidence journaling, against real PostgreSQL 16.

Replays the 2026-07-15 incident shapes: the same story re-syndicated in the
RELATED band re-entering A1 for hours ("Apple Stock Hits 52-Week High"
escalated ~14 times), and article-update revisions re-entering by design.

Covers, A1 side: SUPPRESS decision on a repeat (zero model calls, zero
enqueues, prior decision referenced); repeats after DISCARD also suppressed;
is_correction bypass; corroboration-crossing bypass; held-ticker bypass with
guard fan-out; confidence journaled on ESCALATE/DISCARD, NULL on SUPPRESS;
min_confidence lever discarding a low-confidence material verdict.

Covers, C2 side: cosmetic revision (>=0.90 to its predecessor) dropped;
semantically changed revision forwarded; cosmetic revision on a HELD ticker
forwarded (A12 mandate).
"""
import json
import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ.setdefault("QDRANT_PATH", "/tmp/qdrant-test-supp")

from common.db import get_pool
from common.journal import register_config_version
from common.queue import ack, claim, enqueue
from a1_triage.backends import StubBackend
from a1_triage.service import A1Service
from c2_dedup.cluster import Deduper
from c2_dedup.embedder import get_embedder
from c2_dedup.service import handle_message as c2_handle
from c2_dedup.vectorstore import VectorStore
from router import facts as facts_mod

pytestmark = pytest.mark.asyncio(loop_scope="session")

CFG = {
    "model": {"backend": "stub", "retries_on_invalid": 1},
    "router": {"tier_weight": {1: 6, 2: 4, 3: 1},
               "urgency_weight": {"high": 6, "medium": 3, "low": 0},
               "corroboration_bonus_per_outlet": 1, "corroboration_bonus_cap": 3,
               "overnight_base": 50},
    "suppression": {"enabled": True, "window_hours": 24,
                    "corroboration_reescalate_threshold": 3},
}

OPEN_TS = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)     # Tue 11:00 ET


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    import shutil
    shutil.rmtree("/tmp/qdrant-test-supp", ignore_errors=True)
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.intents, journal.positions,
                     news.cluster_members, news.clusters, news.news_items,
                     queue.messages RESTART IDENTITY CASCADE""")
    await register_config_version("v0.4.7 suppression integration test")
    yield {"pool": pool, "store": VectorStore(path="/tmp/qdrant-test-supp")}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args)
        return await cur.fetchall()


async def plant_item(env, item_id, headline, *, revision=1, summary="",
                     is_correction=False, source="alpaca_benzinga", tier=2,
                     symbols=None):
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO news.news_items
               (item_id, revision, is_correction, source, source_tier, headline,
                summary, content_hash, symbols, channels, published_ts,
                received_ts)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}',now(),now())
               ON CONFLICT DO NOTHING""",
            (item_id, revision, is_correction, source, tier, headline, summary,
             f"h-{item_id}-{revision}", symbols or []))


async def plant_cluster(env, canonical_item, members):
    """members: [(item_id, revision, source)] — all must exist in news_items."""
    async with env["pool"].connection() as c:
        cur = await c.execute(
            "INSERT INTO news.clusters (canonical_item) VALUES (%s) RETURNING cluster_id",
            (canonical_item,))
        cid = (await cur.fetchone())[0]
        for item_id, revision, source in members:
            await c.execute(
                """INSERT INTO news.cluster_members
                   (cluster_id, item_id, revision, source, similarity)
                   VALUES (%s,%s,%s,%s,0.95) ON CONFLICT DO NOTHING""",
                (cid, item_id, revision, source))
    return cid


def deduped(item_id, headline, *, cluster_id, revision=1, tier=2, symbols=None,
            outlets=1, new_story=False, is_correction=False, summary=""):
    return {"envelope": {"msg_schema": "signal.dedup/1", "producer": "C2",
                         "trace": {"signal_id": item_id, "item_id": item_id,
                                   "revision": revision}},
            "body": {"item": {"item_id": item_id, "revision": revision,
                              "headline": headline, "summary": summary,
                              "is_correction": is_correction,
                              "source": "alpaca_benzinga", "source_tier": tier,
                              "symbols": symbols or [],
                              "published_ts": "2026-07-07T14:30:00.000Z"},
                     "cluster": {"cluster_id": cluster_id, "is_new_story": new_story,
                                 "independent_outlets": outlets, "total_items": 1,
                                 "similarity_to_canonical": 0.85}}}


def stub_reply(material, tickers, confidence=0.9):
    return json.dumps({"material": material, "tickers": tickers,
                       "direction_hint": "up", "urgency": "high",
                       "novelty_score": 0.9, "confidence": confidence,
                       "reason": "scripted"})


async def run_a1(env, payload, scripted, key, cfg=None):
    svc = A1Service(cfg or CFG, backend=StubBackend(list(scripted)),
                    store=env["store"])
    orig = facts_mod.market_open_now
    facts_mod.market_open_now = lambda _=None: orig(OPEN_TS)
    try:
        await enqueue("signal.triage", key, payload)
        msg = await claim("signal.triage", "test-a1")
        assert msg is not None and msg.dedup_key == key
        await svc.handle(msg)
        await ack(msg.msg_id)
        return svc
    finally:
        facts_mod.market_open_now = orig


# ---------------------------------------------------------------------------
# A1-side: cluster cooldown
# ---------------------------------------------------------------------------

async def test_01_first_verdict_journals_confidence_and_cluster(env):
    await plant_item(env, "alpaca:9001", "Apple Stock Hits 52-Week High",
                     symbols=["AAPL"])
    cid = await plant_cluster(env, "alpaca:9001",
                              [("alpaca:9001", 1, "alpaca_benzinga")])
    env["cid_aapl"] = cid
    # story is new the first time C2 sees it
    await run_a1(env, deduped("alpaca:9001", "Apple Stock Hits 52-Week High",
                              cluster_id=cid, symbols=["AAPL"], new_story=True),
                 [stub_reply(True, ["AAPL"], confidence=0.55)], "alpaca:9001:1")
    rows = await q(env, """SELECT action, confidence,
                                  payload->'cluster'->>'independent_outlets'
                           FROM journal.decisions WHERE item_id='alpaca:9001'""")
    action, conf, outlets = rows[0]
    assert action == "ESCALATE"
    assert conf == pytest.approx(0.55)         # v0.4.7: no more NULL confidence
    assert outlets == "1"                      # cluster state journaled for bypass


async def test_02_resyndicated_repeat_suppressed_no_model_call(env):
    """The incident shape: same story, different outlet wording (RELATED band),
    forwarded by C2 with is_new_story=false — must cost zero tokens."""
    cid = env["cid_aapl"]
    await plant_item(env, "rss:9002", "Apple shares touch fresh 52-week high",
                     source="rss:marketwatch", symbols=["AAPL"])
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO news.cluster_members
               (cluster_id, item_id, revision, source, similarity)
               VALUES (%s,'rss:9002',1,'rss:marketwatch',0.86)""", (cid,))
    svc = await run_a1(env, deduped("rss:9002",
                                    "Apple shares touch fresh 52-week high",
                                    cluster_id=cid, symbols=["AAPL"], outlets=2),
                       [stub_reply(True, ["AAPL"])], "rss:9002:1")
    assert svc.backend.calls == []             # the model was never invoked
    rows = await q(env, """SELECT action, confidence, model_id,
                                  payload->>'suppressed_by', payload->>'prior_action'
                           FROM journal.decisions WHERE item_id='rss:9002'""")
    action, conf, model_id, supp_by, prior_action = rows[0]
    assert action == "SUPPRESS" and conf is None and model_id is None
    assert prior_action == "ESCALATE" and supp_by is not None
    rows = await q(env, """SELECT count(*) FROM queue.messages
                           WHERE dedup_key='rss:9002:1' AND queue_name != 'signal.triage'""")
    assert rows[0][0] == 0                     # routed nowhere


async def test_03_correction_bypasses_suppression(env):
    cid = env["cid_aapl"]
    await plant_item(env, "alpaca:9001",
                     "Correction: Apple 52-week-high story retracted",
                     revision=2, is_correction=True, symbols=["AAPL"])
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO news.cluster_members
               (cluster_id, item_id, revision, source, similarity)
               VALUES (%s,'alpaca:9001',2,'alpaca_benzinga',1.0)""", (cid,))
    svc = await run_a1(env, deduped("alpaca:9001",
                                    "Correction: Apple 52-week-high story retracted",
                                    cluster_id=cid, revision=2, symbols=["AAPL"],
                                    is_correction=True, outlets=2),
                       [stub_reply(False, ["AAPL"])], "alpaca:9001:2")
    assert len(svc.backend.calls) == 1         # correction re-triaged
    rows = await q(env, """SELECT action FROM journal.decisions
                           WHERE item_id='alpaca:9001' AND item_revision=2""")
    assert rows[0][0] == "DISCARD"             # verdict is fresh, not SUPPRESS


async def test_04_corroboration_crossing_bypasses(env):
    """Prior verdict saw 1 outlet (DISCARD path is the interesting one: a story
    A1 discarded thinly-sourced gets re-judged once independently corroborated)."""
    await plant_item(env, "rss:9101", "SmallCap wins unspecified contract",
                     source="rss:prnewswire", symbols=["SMCP"], tier=3)
    cid = await plant_cluster(env, "rss:9101", [("rss:9101", 1, "rss:prnewswire")])
    await run_a1(env, deduped("rss:9101", "SmallCap wins unspecified contract",
                              cluster_id=cid, symbols=["SMCP"], new_story=True,
                              tier=3),
                 [stub_reply(False, [], confidence=0.5)], "rss:9101:1")
    # now three independent outlets carry it
    await plant_item(env, "alpaca:9102", "SmallCap awarded $200M defense contract",
                     symbols=["SMCP"])
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO news.cluster_members
               (cluster_id, item_id, revision, source, similarity)
               VALUES (%s,'alpaca:9102',1,'alpaca_benzinga',0.84)""", (cid,))
    svc = await run_a1(env, deduped("alpaca:9102",
                                    "SmallCap awarded $200M defense contract",
                                    cluster_id=cid, symbols=["SMCP"], outlets=3),
                       [stub_reply(True, ["SMCP"])], "alpaca:9102:1")
    assert len(svc.backend.calls) == 1         # crossing 3 outlets => re-triage
    rows = await q(env, """SELECT action FROM journal.decisions
                           WHERE item_id='alpaca:9102'""")
    assert rows[0][0] == "ESCALATE"


async def test_05_repeat_below_threshold_still_suppressed(env):
    """2 outlets, threshold 3: not crossed — the repeat is suppressed even
    though corroboration grew, and prior_action=DISCARD repeats stay quiet."""
    await plant_item(env, "rss:9201", "MicroCo signs $2M reseller agreement",
                     source="rss:prnewswire", symbols=["MCRO"], tier=3)
    cid = await plant_cluster(env, "rss:9201", [("rss:9201", 1, "rss:prnewswire")])
    await run_a1(env, deduped("rss:9201", "MicroCo signs $2M reseller agreement",
                              cluster_id=cid, symbols=["MCRO"], new_story=True,
                              tier=3),
                 [stub_reply(False, [], confidence=0.85)], "rss:9201:1")
    await plant_item(env, "rss:9202", "MicroCo announces reseller deal worth $2M",
                     source="rss:businesswire", symbols=["MCRO"], tier=3)
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO news.cluster_members
               (cluster_id, item_id, revision, source, similarity)
               VALUES (%s,'rss:9202',1,'rss:businesswire',0.88)""", (cid,))
    svc = await run_a1(env, deduped("rss:9202",
                                    "MicroCo announces reseller deal worth $2M",
                                    cluster_id=cid, symbols=["MCRO"], outlets=2,
                                    tier=3),
                       [stub_reply(False, [])], "rss:9202:1")
    assert svc.backend.calls == []
    rows = await q(env, """SELECT action, payload->>'prior_action'
                           FROM journal.decisions WHERE item_id='rss:9202'""")
    assert tuple(rows[0]) == ("SUPPRESS", "DISCARD")


async def test_06_held_ticker_bypasses_suppression_and_guards(env):
    """A repeat touching an open position takes the full path — A12 first."""
    # open position on AAPL (full FK chain, as Phase 4 creates it)
    async with env["pool"].connection() as c:
        cur = await c.execute(
            """INSERT INTO journal.decisions
               (signal_id, ticker, stage, agent, action, payload, config_version)
               VALUES ('sig-supp-fixture','AAPL','RISK','A3','SIZED','{}',
                       (SELECT config_version FROM journal.config_versions LIMIT 1))
               RETURNING decision_id""")
        dec_id = (await cur.fetchone())[0]
        await c.execute(
            """INSERT INTO journal.intents
               (intent_id, decision_id, ticker, side, qty, limit_price,
                horizon, status, config_version)
               VALUES ('int-supp-1', %s, 'AAPL', 'BUY', 10, 210.00, 'SHORT',
                       'FILLED',
                       (SELECT config_version FROM journal.config_versions LIMIT 1))""",
            (dec_id,))
        await c.execute(
            """INSERT INTO journal.positions
               (ticker, horizon, profile, status, opened_ts, entry_intent_id,
                thesis_decision_id, qty_initial, qty_open, avg_entry,
                initial_stop, r_unit, exit_policy, config_version)
               VALUES ('AAPL','SHORT','default','OPEN',now(),'int-supp-1',%s,
                       10,10,209.00,200.00,9.00,'{}',
                       (SELECT config_version FROM journal.config_versions LIMIT 1))""",
            (dec_id,))
    cid = env["cid_aapl"]
    await plant_item(env, "rss:9003", "Apple stock notches another 52-week high",
                     source="rss:seekingalpha", symbols=["AAPL"], tier=3)
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO news.cluster_members
               (cluster_id, item_id, revision, source, similarity)
               VALUES (%s,'rss:9003',1,'rss:seekingalpha',0.87)""", (cid,))
    svc = await run_a1(env, deduped("rss:9003",
                                    "Apple stock notches another 52-week high",
                                    cluster_id=cid, symbols=["AAPL"], outlets=2,
                                    tier=3),
                       [stub_reply(False, ["AAPL"])], "rss:9003:1")
    assert len(svc.backend.calls) == 1         # held name: never suppressed
    rows = await q(env, """SELECT queue_name FROM queue.messages
                           WHERE dedup_key='rss:9003:1' AND queue_name != 'signal.triage'""")
    assert [r[0] for r in rows] == ["signal.guard"]   # immaterial-but-held


async def test_07_min_confidence_lever_discards(env):
    cfg = {**CFG, "router": {**CFG["router"], "min_confidence": 0.6}}
    await plant_item(env, "alpaca:9301", "Vague strategic review chatter at Acme",
                     symbols=["ACME"])
    cid = await plant_cluster(env, "alpaca:9301",
                              [("alpaca:9301", 1, "alpaca_benzinga")])
    await run_a1(env, deduped("alpaca:9301",
                              "Vague strategic review chatter at Acme",
                              cluster_id=cid, symbols=["ACME"], new_story=True),
                 [stub_reply(True, ["ACME"], confidence=0.4)],
                 "alpaca:9301:1", cfg=cfg)
    rows = await q(env, """SELECT action, confidence FROM journal.decisions
                           WHERE item_id='alpaca:9301'""")
    action, conf = rows[0]
    assert action == "DISCARD" and conf == pytest.approx(0.4)
    rows = await q(env, """SELECT count(*) FROM queue.messages
                           WHERE dedup_key='alpaca:9301:1'
                             AND queue_name != 'signal.triage'""")
    assert rows[0][0] == 0


# ---------------------------------------------------------------------------
# C2-side: revision policy
# ---------------------------------------------------------------------------

class _Msg:
    def __init__(self, payload, dedup_key):
        self.payload = payload
        self.dedup_key = dedup_key
        self.msg_id = 0


def _news_body(item_id, revision, headline, summary, symbols=None,
               is_correction=False):
    return {"envelope": {"msg_schema": "news_item/1", "producer": "C1",
                         "trace": {"signal_id": item_id, "item_id": item_id,
                                   "revision": revision}},
            "body": {"item_id": item_id, "revision": revision,
                     "is_correction": is_correction, "source": "alpaca_benzinga",
                     "source_tier": 2, "headline": headline, "summary": summary,
                     "symbols": symbols or [], "channels": [],
                     "published_ts": "2026-07-07T14:30:00.000Z",
                     "received_ts": "2026-07-07T14:30:01.000Z"}}


async def _triage_count(env, dedup_key):
    rows = await q(env, """SELECT count(*) FROM queue.messages
                           WHERE queue_name='signal.triage' AND dedup_key=%s""",
                   dedup_key)
    return rows[0][0]


LONG = ("Quantum Devices Q2 revenue beats guidance; company raises full-year "
        "outlook on datacenter demand and reiterates margin targets for the "
        "second half of the fiscal year")


async def test_08_cosmetic_revision_dropped_semantic_forwarded(env):
    deduper = Deduper(env["store"], get_embedder())
    await plant_item(env, "alpaca:9401", LONG, summary=LONG, symbols=["QDEV"])
    await c2_handle(_Msg(_news_body("alpaca:9401", 1, LONG, LONG, ["QDEV"]),
                         "alpaca:9401:1"), deduper)
    assert await _triage_count(env, "alpaca:9401:1") == 1

    # rev 2: one token changed — cosmetic edit, is_correction by store semantics
    cosmetic = LONG.replace("datacenter", "data-center")
    await plant_item(env, "alpaca:9401", cosmetic, revision=2, summary=LONG,
                     is_correction=True, symbols=["QDEV"])
    await c2_handle(_Msg(_news_body("alpaca:9401", 2, cosmetic, LONG, ["QDEV"],
                                    is_correction=True),
                         "alpaca:9401:2"), deduper)
    assert await _triage_count(env, "alpaca:9401:2") == 0   # dropped

    # rev 3: the numbers changed — semantically different, forwards
    changed = ("Quantum Devices RESTATES Q2 revenue lower after accounting "
               "error; full-year outlook withdrawn pending audit committee "
               "review of datacenter segment recognition practices")
    await plant_item(env, "alpaca:9401", changed, revision=3, summary=changed,
                     is_correction=True, symbols=["QDEV"])
    await c2_handle(_Msg(_news_body("alpaca:9401", 3, changed, changed, ["QDEV"],
                                    is_correction=True),
                         "alpaca:9401:3"), deduper)
    assert await _triage_count(env, "alpaca:9401:3") == 1   # forwarded


async def test_09_cosmetic_revision_on_held_ticker_forwards(env):
    """A12 mandate: AAPL is held (planted in test_06) — even a cosmetic
    revision must flow so the guard path can see it."""
    deduper = Deduper(env["store"], get_embedder())
    await plant_item(env, "alpaca:9501", LONG, summary=LONG, symbols=["AAPL"])
    await c2_handle(_Msg(_news_body("alpaca:9501", 1, LONG, LONG, ["AAPL"]),
                         "alpaca:9501:1"), deduper)
    cosmetic = LONG.replace("reiterates", "re-iterates")
    await plant_item(env, "alpaca:9501", cosmetic, revision=2, summary=LONG,
                     is_correction=True, symbols=["AAPL"])
    await c2_handle(_Msg(_news_body("alpaca:9501", 2, cosmetic, LONG, ["AAPL"],
                                    is_correction=True),
                         "alpaca:9501:2"), deduper)
    assert await _triage_count(env, "alpaca:9501:2") == 1   # held => forwarded
