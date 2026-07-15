"""Full C1 -> C2 lifecycle against a REAL PostgreSQL 16 instance.

Mirrors the story of news-lifecycle-test.sql, but through the actual service
code: normalize -> store (transactional enqueue) -> C2 consume -> dedup/
cluster/corroborate -> DedupedSignal on signal.triage. Plus: revision flow,
quarantine, duplicate-echo no-op, DLQ after max_attempts, 48h prune.

Requires PIPELINE_DSN pointing at a database with schema/*.sql applied.
Tables are truncated per test session; run against a dev DB only.
"""
import asyncio
import os

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ.setdefault("QDRANT_PATH", "/tmp/qdrant-test")

from common.clock import utcnow
from common.db import get_pool
from common.queue import claim, enqueue, fail as qfail
from c1_ingestion.normalize import NormalizeError, normalize_alpaca, normalize_rss
from c1_ingestion.store import quarantine, store_item
from c2_dedup.cluster import Deduper
from c2_dedup.embedder import get_embedder
from c2_dedup.service import handle_message
from c2_dedup.vectorstore import VectorStore

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    import shutil
    shutil.rmtree("/tmp/qdrant-test", ignore_errors=True)
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE news.cluster_members, news.clusters, news.news_items,
                     news.quarantine, news.ingestion_gaps, queue.messages
            RESTART IDENTITY CASCADE""")
    store = VectorStore(path="/tmp/qdrant-test")
    deduper = Deduper(store, get_embedder())
    yield {"pool": pool, "store": store, "deduper": deduper}


def alpaca_payload(aid: int, headline: str, summary: str = "", symbols=None):
    return {"T": "n", "id": aid, "headline": headline, "summary": summary,
            "created_at": utcnow().isoformat(), "symbols": symbols or [],
            "url": f"https://example.com/{aid}", "source": "benzinga"}


async def drain_and_process(env, n: int):
    """Claim n messages from signal.dedup and run them through C2."""
    results = []
    for _ in range(n):
        msg = await claim("signal.dedup", "test-c2")
        assert msg is not None, "expected a message on signal.dedup"
        await handle_message(msg, env["deduper"])
        from common.queue import ack
        await ack(msg.msg_id)
        results.append(msg)
    return results


# -------------------------------------------------------------------------------
async def test_01_store_and_transactional_enqueue(env):
    item = normalize_alpaca(alpaca_payload(1001, "Acme Corp announces $2B buyback",
                                           "Board approves repurchase program.", ["ACME"]))
    res = await store_item(item)
    assert res.stored and res.revision == 1 and res.enqueued

    async with env["pool"].connection() as c:
        cur = await c.execute("SELECT count(*) FROM news.news_items WHERE item_id='alpaca:1001'")
        assert (await cur.fetchone())[0] == 1
        cur = await c.execute(
            "SELECT count(*) FROM queue.messages WHERE queue_name='signal.dedup' AND dedup_key='alpaca:1001:1'")
        assert (await cur.fetchone())[0] == 1


async def test_02_duplicate_echo_is_noop(env):
    """Feed replay after reconnect: same item, same content -> nothing written."""
    item = normalize_alpaca(alpaca_payload(1001, "Acme Corp announces $2B buyback",
                                           "Board approves repurchase program.", ["ACME"]))
    res = await store_item(item)
    assert not res.stored and not res.enqueued
    async with env["pool"].connection() as c:
        cur = await c.execute("SELECT count(*) FROM news.news_items WHERE item_id='alpaca:1001'")
        assert (await cur.fetchone())[0] == 1


async def test_03_changed_content_becomes_revision(env):
    """v0.4: a correction is a new revision of the same item_id."""
    item = normalize_alpaca(alpaca_payload(1001, "CORRECTED: Acme buyback is $1B, not $2B",
                                           "Board approves repurchase program.", ["ACME"]))
    res = await store_item(item)
    assert res.stored and res.revision == 2 and res.is_correction

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT revision, is_correction, supersedes FROM news.news_items_latest WHERE item_id='alpaca:1001'")
        rev, is_corr, sup = await cur.fetchone()
        assert (rev, is_corr, sup) == (2, True, 1)


async def test_04_quarantine_never_drop(env):
    try:
        normalize_alpaca({"T": "n", "id": 9999, "headline": "x", "created_at": "0000-00-00"})
        assert False
    except NormalizeError as e:
        await quarantine(e, "alpaca_benzinga")
    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT reason_code FROM news.quarantine WHERE source='alpaca_benzinga'")
        assert (await cur.fetchone())[0] == "BAD_TIMESTAMP"


async def test_05_c2_new_story_cluster(env):
    """First unique story -> new cluster, is_new_story=true, forwarded to triage."""
    await drain_and_process(env, 2)   # rev 1 + rev 2 of alpaca:1001

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT payload->'body'->'cluster'->>'is_new_story' FROM queue.messages "
            "WHERE queue_name='signal.triage' AND dedup_key='alpaca:1001:1'")
        assert (await cur.fetchone())[0] == "true"
        # revision 2 joined revision 1's cluster, not a new one
        cur = await c.execute("SELECT count(*) FROM news.clusters")
        assert (await cur.fetchone())[0] == 1
        cur = await c.execute(
            "SELECT payload->'body'->'cluster'->>'is_new_story' FROM queue.messages "
            "WHERE queue_name='signal.triage' AND dedup_key='alpaca:1001:2'")
        assert (await cur.fetchone())[0] == "false"


async def test_06_corroboration_across_independent_outlets(env):
    """Same story from a second outlet -> same cluster, independent_outlets=2.
    This is C3's credibility input (v0.2).

    2026-07-14 change: this outlet's text is IDENTICAL to the first (wire
    copy, sim >= 0.90) so it is a DUPLICATE — corroboration is recorded but
    the item is dropped, not forwarded (baseline §4 C2; the EDGAR-storm fix).
    Distinct-wording corroboration (0.80-0.90 band) still forwards — that is
    test_06b below."""
    rss_entry = {
        "title": "Acme Corp announces $2B buyback",
        "id": "https://wire.example/acme-buyback",
        "link": "https://wire.example/acme-buyback",
        "published": utcnow().isoformat(),
        "summary": "Board approves repurchase program.",
    }
    item = normalize_rss(rss_entry, feed_name="wire")
    await store_item(item)
    await drain_and_process(env, 1)

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT independent_outlets, total_items FROM news.cluster_corroboration WHERE cluster_id=1")
        outlets, total = await cur.fetchone()
        assert outlets == 2, f"expected 2 independent outlets, got {outlets}"
        assert total == 3                       # 2 alpaca revisions + 1 rss

        # duplicate (sim >= 0.90) must NOT reach triage
        cur = await c.execute(
            "SELECT count(*) FROM queue.messages "
            "WHERE queue_name='signal.triage' AND dedup_key LIKE 'rss:wire%'")
        assert (await cur.fetchone())[0] == 0


async def test_06b_distinct_wording_corroboration_still_forwards(env):
    """A second outlet with its own wording (0.80 <= sim < 0.90) is
    corroboration, not a duplicate — it joins the cluster AND forwards."""
    rss_entry = {
        # measured at sim 0.866 to the alpaca:1001 text under the hash
        # embedder — inside the 0.80-0.90 corroboration band
        "title": "Acme Corp announces $2B buyback",
        "id": "https://wire2.example/acme-repurchase",
        "link": "https://wire2.example/acme-repurchase",
        "published": utcnow().isoformat(),
        "summary": "Board approves repurchase program, shares to rise.",
    }
    item = normalize_rss(rss_entry, feed_name="wire2")
    await store_item(item)
    msgs = await drain_and_process(env, 1)

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT payload->'body'->'cluster' FROM queue.messages "
            "WHERE queue_name='signal.triage' AND dedup_key LIKE 'rss:wire2%'")
        row = await cur.fetchone()
        assert row is not None, "corroboration-band item must forward"
        cluster = row[0]
        assert cluster["is_new_story"] is False
        assert cluster["cluster_id"] == 1
        assert cluster["independent_outlets"] == 3


async def test_07_unrelated_story_new_cluster(env):
    item = normalize_alpaca(alpaca_payload(
        2002, "Zenith Pharma wins FDA approval for migraine drug",
        "Phase 3 data cleared.", ["ZNTH"]))
    await store_item(item)
    await drain_and_process(env, 1)
    async with env["pool"].connection() as c:
        cur = await c.execute("SELECT count(*) FROM news.clusters")
        assert (await cur.fetchone())[0] == 2


async def test_08_dlq_to_quarantine_after_max_attempts(env):
    """A poison message retries with backoff then dead-letters into
    news.quarantine with a queue: source prefix (spec §1)."""
    await enqueue("signal.dedup", "poison:1", {"envelope": {}, "body": {"item_id": "poison"}})
    for attempt in range(5):
        async with env["pool"].connection() as c:
            await c.execute(
                "UPDATE queue.messages SET available_ts = now() WHERE dedup_key='poison:1'")
        msg = await claim("signal.dedup", "test-c2")
        assert msg is not None and msg.dedup_key == "poison:1"
        try:
            await handle_message(msg, env["deduper"])
            assert False, "poison message should fail"
        except Exception as e:
            await qfail(msg.msg_id, repr(e))

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT count(*) FROM news.quarantine WHERE source='queue:signal.dedup'")
        assert (await cur.fetchone())[0] == 1
        cur = await c.execute(
            "SELECT done_ts IS NOT NULL FROM queue.messages WHERE dedup_key='poison:1'")
        assert (await cur.fetchone())[0] is True


async def test_09_prune_respects_window(env):
    """Points older than 48h are pruned; fresh ones survive."""
    store = env["store"]
    n_before = store.client.count(store.dedup).count
    assert n_before >= 4                        # everything ingested so far
    # age one point artificially
    pts = store.client.scroll(store.dedup, limit=1)[0]
    store.client.set_payload(store.dedup, payload={"ts": 0.0},
                             points=[pts[0].id])
    removed = store.prune_dedup(48)
    assert removed == 1
    assert store.client.count(store.dedup).count == n_before - 1


async def test_10_gap_monitor_opens_and_closes(env):
    from c1_ingestion.heartbeat import GapMonitor
    from datetime import timedelta
    mon = GapMonitor("testsource", market_threshold_secs=1, offhours_threshold_secs=1)
    mon.last_item_ts = utcnow() - timedelta(seconds=10)
    await mon.check()
    assert mon.open_gap_id is not None
    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT gap_end FROM news.ingestion_gaps WHERE source='testsource'")
        assert (await cur.fetchone())[0] is None          # ongoing
    mon.mark_activity()
    await mon.check()
    assert mon.open_gap_id is None
    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT gap_end IS NOT NULL FROM news.ingestion_gaps WHERE source='testsource'")
        assert (await cur.fetchone())[0] is True          # closed


async def test_11_health_rows_written(env):
    from c1_ingestion.heartbeat import set_health
    await set_health("ingestion", "OK", "test")
    async with env["pool"].connection() as c:
        cur = await c.execute("SELECT status FROM journal.health WHERE component='ingestion'")
        assert (await cur.fetchone())[0] == "OK"

