"""Phase 2 integration: DedupedSignal in -> TRIAGE decision row + routed
TriagedSignal out, against real PostgreSQL 16 through the actual A1Service.

Covers: config_version registration; ESCALATE with market-open routing;
overnight routing with priority ordering; DISCARD journaling; thesis-lane for
untagged material items; guard fan-out on a held position (incl. the
immaterial-but-held case); REJECT after retry exhaustion (raw output
journaled); retry-then-success consuming exactly two model calls; retrieval
promotion on material=true; decision+routing atomicity via the shared tx.
"""
import json
import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ.setdefault("QDRANT_PATH", "/tmp/qdrant-test-p2")

from common.db import get_pool
from common.journal import register_config_version
from common.queue import claim, enqueue
from a1_triage.backends import StubBackend
from a1_triage.service import A1Service
from c2_dedup.vectorstore import VectorStore
from router import facts as facts_mod

pytestmark = pytest.mark.asyncio(loop_scope="session")

CFG = {
    "model": {"backend": "stub", "retries_on_invalid": 1},
    "router": {"tier_weight": {1: 6, 2: 4, 3: 1},
               "urgency_weight": {"high": 6, "medium": 3, "low": 0},
               "corroboration_bonus_per_outlet": 1, "corroboration_bonus_cap": 3,
               "overnight_base": 50},
}

OPEN_TS = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)     # Tue 11:00 ET
CLOSED_TS = datetime(2026, 7, 7, 1, 0, tzinfo=timezone.utc)    # Mon 21:00 ET


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    import shutil
    # Qdrant local mode is single-client-per-path: this suite must not share
    # the Phase 1 suite's path when both run in one process. Force ours here
    # (module-level setdefault loses to an exported QDRANT_PATH).
    os.environ["QDRANT_PATH"] = "/tmp/qdrant-test-p2"
    shutil.rmtree("/tmp/qdrant-test-p2", ignore_errors=True)
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     queue.messages RESTART IDENTITY CASCADE""")
    await register_config_version("phase2 integration test")
    yield {"pool": pool, "store": VectorStore()}


def deduped(item_id: str, headline: str, *, revision=1, tier=2, symbols=None,
            outlets=1, new_story=True, summary="") -> dict:
    return {"envelope": {"msg_schema": "signal.dedup/1", "producer": "C2",
                         "trace": {"signal_id": item_id, "item_id": item_id,
                                   "revision": revision}},
            "body": {"item": {"item_id": item_id, "revision": revision,
                              "headline": headline, "summary": summary,
                              "source": "alpaca_benzinga", "source_tier": tier,
                              "symbols": symbols or [],
                              "published_ts": "2026-07-07T14:30:00.000Z"},
                     "cluster": {"cluster_id": 1, "is_new_story": new_story,
                                 "independent_outlets": outlets, "total_items": 1,
                                 "similarity_to_canonical": 1.0}}}


def stub_reply(material, tickers, urgency="high", novelty=0.9):
    return json.dumps({"material": material, "tickers": tickers,
                       "direction_hint": "up", "urgency": urgency,
                       "novelty_score": novelty, "reason": "scripted"})


async def run_one(env, payload: dict, scripted: list[str], now, key: str):
    svc = A1Service(CFG, backend=StubBackend(scripted), store=env["store"])
    # pin the clock for market_open determinism
    orig = facts_mod.market_open_now
    facts_mod.market_open_now = lambda _=None, _now=now: orig(_now)
    try:
        await enqueue("signal.triage", key, payload)
        msg = await claim("signal.triage", "test-a1")
        assert msg is not None and msg.dedup_key == key
        await svc.handle(msg)
        from common.queue import ack
        await ack(msg.msg_id)
        return svc
    finally:
        facts_mod.market_open_now = orig


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args)
        return await cur.fetchall()


# --------------------------------------------------------------------------------

async def test_01_escalate_market_open_routes_analyst(env):
    await run_one(env, deduped("alpaca:5001", "Acme acquisition at $45", symbols=["ACME"]),
                  [stub_reply(True, ["ACME"])], OPEN_TS, "alpaca:5001:1")
    rows = await q(env, """SELECT action, ticker, model_id,
                                  payload->'routing'->>'market_open'
                           FROM journal.decisions WHERE item_id='alpaca:5001'""")
    assert rows[0] == ("ESCALATE", "ACME", "stub-0", "true")
    rows = await q(env, """SELECT payload->'body'->'triage'->>'material'
                           FROM queue.messages
                           WHERE queue_name='signal.analyst' AND dedup_key='alpaca:5001:1'""")
    assert rows[0][0] == "true"


async def test_02_market_closed_overnight_with_priority(env):
    await run_one(env, deduped("alpaca:5002", "Zenith FDA approval", tier=1, symbols=["ZNTH"]),
                  [stub_reply(True, ["ZNTH"], urgency="high", novelty=1.0)],
                  CLOSED_TS, "alpaca:5002:1")
    # score: tier1(6)+high(6)+round(1.0*4)=4+0 = 16 -> queue priority 34
    rows = await q(env, """SELECT priority FROM queue.messages
                           WHERE queue_name='signal.overnight' AND dedup_key='alpaca:5002:1'""")
    assert rows[0][0] == 34


async def test_03_discard_journaled_no_routes(env):
    await run_one(env, deduped("alpaca:5003", "TechWave wins innovation award"),
                  [stub_reply(False, [])], OPEN_TS, "alpaca:5003:1")
    rows = await q(env, "SELECT action FROM journal.decisions WHERE item_id='alpaca:5003'")
    assert rows[0][0] == "DISCARD"
    rows = await q(env, """SELECT count(*) FROM queue.messages
                           WHERE dedup_key='alpaca:5003:1'
                             AND queue_name != 'signal.triage'""")
    assert rows[0][0] == 0


async def test_04_material_no_ticker_thesis_lane(env):
    await run_one(env, deduped("edgar:acc-1", "8-K - MYSTERY CORP: FDA CRL received", tier=1),
                  [stub_reply(True, [])], OPEN_TS, "edgar:acc-1:1")
    rows = await q(env, """SELECT queue_name FROM queue.messages
                           WHERE dedup_key='edgar:acc-1:1' AND queue_name != 'signal.triage'""")
    assert [r[0] for r in rows] == ["signal.thesis"]      # never intraday, even market-open


async def test_05_guard_fanout_on_held_position(env):
    # Plant an open position with its full FK chain (decision -> intent ->
    # position), exactly as Phase 4's A3/C4 will create it.
    async with env["pool"].connection() as c:
        cur = await c.execute(
            """INSERT INTO journal.decisions
               (signal_id, ticker, stage, agent, action, payload, config_version)
               VALUES ('sig-fixture', 'ACME', 'RISK', 'A3', 'SIZED', '{}',
                       (SELECT config_version FROM journal.config_versions LIMIT 1))
               RETURNING decision_id""")
        dec_id = (await cur.fetchone())[0]
        await c.execute(
            """INSERT INTO journal.intents
               (intent_id, decision_id, ticker, side, qty, limit_price,
                horizon, status, config_version)
               VALUES ('int-fixture-1', %s, 'ACME', 'BUY', 50, 100.20,
                       'SHORT', 'FILLED',
                       (SELECT config_version FROM journal.config_versions LIMIT 1))""",
            (dec_id,))
        await c.execute(
            """INSERT INTO journal.positions
               (ticker, horizon, profile, status, opened_ts, entry_intent_id,
                thesis_decision_id, qty_initial, qty_open, avg_entry,
                initial_stop, r_unit, exit_policy, config_version)
               VALUES ('ACME', 'SHORT', 'default', 'OPEN', now(),
                       'int-fixture-1', %s, 50, 50, 100.00, 95.00, 5.00, '{}',
                       (SELECT config_version FROM journal.config_versions LIMIT 1))""",
            (dec_id,))
    await run_one(env, deduped("alpaca:5005", "Acme guidance cut", revision=2, symbols=["ACME"]),
                  [stub_reply(True, ["ACME"])], OPEN_TS, "alpaca:5005:2")
    rows = await q(env, """SELECT queue_name, priority FROM queue.messages
                           WHERE dedup_key='alpaca:5005:2' AND queue_name != 'signal.triage'
                           ORDER BY queue_name""")
    assert ("signal.guard", 0) in [tuple(r) for r in rows]
    assert ("signal.analyst", 100) in [tuple(r) for r in rows]


async def test_06_immaterial_but_held_still_guards(env):
    await run_one(env, deduped("alpaca:5006", "Acme sponsors charity golf event", symbols=["ACME"]),
                  [stub_reply(False, ["ACME"])], OPEN_TS, "alpaca:5006:1")
    rows = await q(env, "SELECT action FROM journal.decisions WHERE item_id='alpaca:5006'")
    assert rows[0][0] == "DISCARD"
    rows = await q(env, """SELECT queue_name FROM queue.messages
                           WHERE dedup_key='alpaca:5006:1' AND queue_name != 'signal.triage'""")
    assert [r[0] for r in rows] == ["signal.guard"]


async def test_07_reject_after_retry_exhaustion(env):
    bad = "the item is clearly material because"
    await run_one(env, deduped("alpaca:5007", "Acme merger talk"),
                  [bad, bad], OPEN_TS, "alpaca:5007:1")
    rows = await q(env, """SELECT action, payload->>'raw_output', payload->>'attempts'
                           FROM journal.decisions WHERE item_id='alpaca:5007'""")
    action, raw, attempts = rows[0]
    assert action == "REJECT" and raw.startswith("the item") and attempts == "2"
    rows = await q(env, """SELECT count(*) FROM queue.messages
                           WHERE dedup_key='alpaca:5007:1' AND queue_name != 'signal.triage'""")
    assert rows[0][0] == 0                        # rejected items route nowhere


async def test_08_retry_then_success_two_calls(env):
    svc = await run_one(env, deduped("alpaca:5008", "Acme buyback", symbols=["ACME"]),
                        ["not json at all", stub_reply(True, ["ACME"])],
                        OPEN_TS, "alpaca:5008:1")
    assert len(svc.backend.calls) == 2
    assert "previous response was invalid" in svc.backend.calls[1][-1]["content"]
    rows = await q(env, "SELECT action FROM journal.decisions WHERE item_id='alpaca:5008'")
    assert rows[0][0] == "ESCALATE"


async def test_09_retrieval_promotion_material_only(env):
    store = env["store"]
    n = store.client.count(store.retrieval).count
    # material items promoted so far: 5001, 5002, acc-1, 5005, 5008 = 5
    assert n == 5, f"expected 5 promoted items, got {n}"
    # a discarded item must NOT be in retrieval
    hits = store.client.scroll(store.retrieval, limit=50)[0]
    ids = {h.payload["item_id"] for h in hits}
    assert "alpaca:5003" not in ids and "alpaca:5006" not in ids


async def test_10_config_version_stamped(env):
    rows = await q(env, """SELECT DISTINCT config_version FROM journal.decisions""")
    assert len(rows) == 1
    rows2 = await q(env, """SELECT count(*) FROM journal.config_versions
                            WHERE config_version = %s""", rows[0][0])
    assert rows2[0][0] == 1
