"""Phase 3 integration against real PostgreSQL 16: the A2 -> C3 chain through
the actual services with FakeData markets and scripted stub models.

Covers: regime snapshot write + reference on A2 decisions; THESIS decision +
signal.gate enqueue; DSL-invalid thesis -> retry -> REJECT; gate PASS ->
GatePass on signal.risk with snapshot; gate CREDIBILITY and GATE_NO_CONFIRM
vetoes journaled with numbers and no message; sympathy lane full round trip
(A2 related_opportunities -> signal.synthetic -> A1 synthetic triage with
derived_from lineage -> signal.analyst with ticker override).
"""
import json
import os
from datetime import timedelta

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"

from common.clock import utcnow
from common.db import get_pool
from common.journal import register_config_version
from common.marketdata import FakeData, Quote
from common.queue import ack, claim, enqueue
from a1_triage.backends import StubBackend
from a1_triage.service import A1Service
from a2_analyst.service import A2Service
from c2_dedup.vectorstore import VectorStore
from c3_gate.service import C3Service

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Deterministic clock: Tuesday 2026-07-07 15:00 UTC = 11:00 ET, in-session.
from datetime import datetime, timezone
PIN = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)
NEWS_TS = PIN - timedelta(minutes=10)

A1_CFG = {"model": {"backend": "stub", "retries_on_invalid": 1},
          "router": {"tier_weight": {1: 6, 2: 4, 3: 1},
                     "urgency_weight": {"high": 6, "medium": 3, "low": 0},
                     "corroboration_bonus_per_outlet": 1,
                     "corroboration_bonus_cap": 3, "overnight_base": 50}}
A2_CFG = {"model": {"backend": "stub", "retries_on_invalid": 1}}
GATE_CFG = {"gate": {"intraday_move_pct": 0.015, "intraday_vol_mult": 2.5,
                     "intraday_window_min": 30, "extended_pct": 0.06,
                     "open_blackout_min": 15, "handoff_gap_ratio": 0.5,
                     "impact_medium_min": 0.02, "impact_high_min": 0.05,
                     "required_outlets": {"low": {2: 1, 3: 1},
                                          "medium": {2: 1, 3: 2},
                                          "high": {2: 2, 3: 3}}}}


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    import shutil
    shutil.rmtree("/tmp/qdrant-test-p3", ignore_errors=True)
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.regime_snapshots, news.cluster_members,
                     news.clusters, news.news_items, queue.messages
                     RESTART IDENTITY CASCADE""")
    await register_config_version("phase3 integration test")
    yield {"pool": pool, "store": VectorStore(path="/tmp/qdrant-test-p3")}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args)
        return await cur.fetchall()


async def seed_item(env, item_id: str, headline: str, tier=2, source="alpaca_benzinga",
                    published=None, outlets_sources=None):
    """Insert an item + cluster directly (C1/C2's output state)."""
    published = published or NEWS_TS
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO news.news_items
               (item_id, revision, source, source_tier, headline, content_hash,
                symbols, channels, published_ts, received_ts)
               VALUES (%s,1,%s,%s,%s,%s,'{ACME}','{}',%s,%s)""",
            (item_id, source, tier, headline, f"hash-{item_id}",
             published, published))
        cur = await c.execute(
            "INSERT INTO news.clusters (canonical_item) VALUES (%s) RETURNING cluster_id",
            (item_id,))
        cluster_id = (await cur.fetchone())[0]
        members = [(item_id, source)] + list(outlets_sources or [])
        for i, (mid, msrc) in enumerate(members):
            if i > 0:
                await c.execute(
                    """INSERT INTO news.news_items
                       (item_id, revision, source, source_tier, headline,
                        content_hash, symbols, channels, published_ts, received_ts)
                       VALUES (%s,1,%s,%s,%s,%s,'{ACME}','{}',%s,%s)""",
                    (mid, msrc, 3, headline, f"hash-{mid}", published, published))
            await c.execute(
                """INSERT INTO news.cluster_members
                   (cluster_id, item_id, revision, source, similarity)
                   VALUES (%s,%s,1,%s,0.95)""", (cluster_id, mid, msrc))
    return cluster_id


def hot_market(now=PIN) -> FakeData:
    """ACME: +2% on 3x volume since 10 minutes ago — a clean intraday confirm."""
    md = FakeData()
    news_ts = now - timedelta(minutes=10)
    md.set_daily("ACME", FakeData.flat_daily(30, close=100.0, volume=5_000_000))
    md.set_prev_close("ACME", 100.0)
    baseline = FakeData.ramp_minute(news_ts - timedelta(days=1), 60, 100.0, 100.0, 10_000)
    since = FakeData.ramp_minute(news_ts, 10, 100.0, 102.0, 30_000)
    md.set_minute("ACME", baseline + since)
    md.set_quote("ACME", Quote(price=102.0, bid=101.98, ask=102.02, ts=now))
    return md


def thesis_reply(**over):
    base = {"ticker": "ACME", "direction": "up", "magnitude_est": 0.055,
            "expected_move_window": "2_sessions", "horizon": "SHORT",
            "confidence": 0.72, "priced_in_assessment": "2% of 5.5% captured",
            "source_risk": "low",
            "invalidation": {"machine_checkable": ["close_below_prenews"],
                             "news_checkable": ["denial"]},
            "related_opportunities": [], "reason": "supply repricing"}
    base.update(over)
    return json.dumps(base)


def triaged_msg(item_id: str, signal_id=None):
    return {"envelope": {"msg_schema": "signal.triaged/1", "producer": "A1",
                         "trace": {"signal_id": signal_id or item_id,
                                   "item_id": item_id, "revision": 1}},
            "body": {"item_ref": {"item_id": item_id, "revision": 1, "cluster_id": 1},
                     "triage": {"material": True, "tickers": ["ACME"],
                                "direction_hint": "up", "urgency": "high",
                                "novelty_score": 0.9, "reason": "t"},
                     "routing": {"market_open": True, "position_ids": [],
                                 "thesis_matches": [], "priority_score": 14}}}


async def process(queue_name, key, payload, svc, handler=None):
    await enqueue(queue_name, key, payload)
    msg = await claim(queue_name, "test")
    assert msg is not None and msg.dedup_key == key
    await (handler or svc.handle)(msg)
    await ack(msg.msg_id)


# ---------------------------------------------------------------------------------

async def test_01_regime_snapshot_written(env):
    from c8_regime.service import write_snapshot
    rid = await write_snapshot(FakeData())
    rows = await q(env, "SELECT features->>'index_trend', features->>'source' "
                        "FROM journal.regime_snapshots WHERE regime_id=%s", rid)
    assert rows[0][0] in ("above_50d", "below_50d")
    assert rows[0][1] == "etf_proxies_iex"


async def test_02_thesis_flow_to_gate_queue(env):
    await seed_item(env, "alpaca:7001", "Acme supply agreement expands",
                    outlets_sources=[("rss:wire:7001", "rss:wire")])
    svc = A2Service(A2_CFG, backend=StubBackend([thesis_reply()]),
                    md=hot_market(), store=env["store"])
    await process("signal.analyst", "alpaca:7001:1", triaged_msg("alpaca:7001"), svc)

    rows = await q(env, """SELECT action, ticker, confidence, regime_id,
                                  payload->'thesis'->>'expected_move_window'
                           FROM journal.decisions
                           WHERE item_id='alpaca:7001' AND stage='ANALYST'""")
    action, ticker, conf, regime_id, window = rows[0]
    assert (action, ticker, window) == ("THESIS", "ACME", "2_sessions")
    assert conf == pytest.approx(0.72)
    assert regime_id is not None                     # references test_01's snapshot

    rows = await q(env, """SELECT payload->'body'->'thesis'->>'magnitude_est'
                           FROM queue.messages
                           WHERE queue_name='signal.gate' AND dedup_key='alpaca:7001:1'""")
    assert rows[0][0] == "0.055"


async def test_03_dsl_invalid_thesis_rejected(env):
    await seed_item(env, "alpaca:7002", "Zed corp wins contract")
    bad = thesis_reply(invalidation={"machine_checkable": ["stock_feels_weak"],
                                     "news_checkable": []})
    svc = A2Service(A2_CFG, backend=StubBackend([bad, bad]),
                    md=hot_market(), store=env["store"])
    await process("signal.analyst", "alpaca:7002:1", triaged_msg("alpaca:7002"), svc)

    rows = await q(env, """SELECT action, payload->>'error' FROM journal.decisions
                           WHERE item_id='alpaca:7002' AND stage='ANALYST'""")
    action, err = rows[0]
    assert action == "REJECT" and "unknown stdlib predicate" in err
    rows = await q(env, "SELECT count(*) FROM queue.messages WHERE queue_name='signal.gate' "
                        "AND dedup_key='alpaca:7002:1'")
    assert rows[0][0] == 0


async def test_04_gate_pass_to_risk_queue(env):
    """Consumes the REAL message A2 enqueued in test_02 — true end-to-end."""
    svc = C3Service(GATE_CFG, md=hot_market(), now_fn=lambda: PIN)
    msg = await claim("signal.gate", "test-c3")
    assert msg is not None and msg.dedup_key == "alpaca:7001:1"
    await svc.handle(msg)
    await ack(msg.msg_id)

    rows = await q(env, """SELECT action, payload->>'pct_move', payload->'snapshot'->>'ref_price'
                           FROM journal.decisions
                           WHERE item_id='alpaca:7001' AND stage='GATE'""")
    action, pct, ref = rows[0]
    assert action == "PASS" and float(pct) == pytest.approx(0.02, abs=0.004)
    assert float(ref) == pytest.approx(102.0)

    rows = await q(env, """SELECT payload->'body'->'gate'->>'verdict',
                                  payload->'body'->'gate'->'snapshot'->>'atr_14'
                           FROM queue.messages
                           WHERE queue_name='signal.risk' AND dedup_key='alpaca:7001:1'""")
    assert rows[0][0] == "PASS" and rows[0][1] is not None


async def test_05_gate_credibility_veto_no_message(env):
    """Tier-3 single-source high-impact claim never passes alone (v0.2)."""
    await seed_item(env, "rss:blog:x1", "MicroCap to be acquired, blog says",
                    tier=3, source="rss:blog")
    svc = C3Service(GATE_CFG, md=hot_market(), now_fn=lambda: PIN)
    gate_msg = {"envelope": {"msg_schema": "signal.gate/1", "producer": "A2",
                             "trace": {"signal_id": "rss:blog:x1",
                                       "item_id": "rss:blog:x1", "revision": 1}},
                "body": {"item_ref": {"item_id": "rss:blog:x1", "revision": 1},
                         "thesis": json.loads(thesis_reply(source_risk="high")),
                         "regime_id": 1}}
    await process("signal.gate", "gate:x1:1", gate_msg, svc)

    rows = await q(env, """SELECT action, veto_reason,
                                  payload->'credibility'->>'required_outlets'
                           FROM journal.decisions
                           WHERE item_id='rss:blog:x1' AND stage='GATE'""")
    assert tuple(rows[0]) == ("VETO", "CREDIBILITY", "3")
    rows = await q(env, "SELECT count(*) FROM queue.messages WHERE queue_name='signal.risk' "
                        "AND dedup_key='gate:x1:1'")
    assert rows[0][0] == 0                       # vetoes produce no message (§8)


async def test_06_gate_no_confirm_veto(env):
    """Corroborated story but flat tape -> GATE_NO_CONFIRM."""
    await seed_item(env, "alpaca:7003", "Acme mid-cycle update", tier=2,
                    outlets_sources=[("rss:wire:7003", "rss:wire")])
    md = FakeData()   # flat default tape: no move, no volume spike
    md.set_daily("ACME", FakeData.flat_daily(30, close=100.0, volume=5_000_000))
    svc = C3Service(GATE_CFG, md=md, now_fn=lambda: PIN)
    gate_msg = {"envelope": {"msg_schema": "signal.gate/1", "producer": "A2",
                             "trace": {"signal_id": "alpaca:7003",
                                       "item_id": "alpaca:7003", "revision": 1}},
                "body": {"item_ref": {"item_id": "alpaca:7003", "revision": 1},
                         "thesis": json.loads(thesis_reply(magnitude_est=0.03)),
                         "regime_id": 1}}
    await process("signal.gate", "gate:7003:1", gate_msg, svc)
    rows = await q(env, """SELECT veto_reason FROM journal.decisions
                           WHERE item_id='alpaca:7003' AND stage='GATE'""")
    assert rows[0][0] == "GATE_NO_CONFIRM"


async def test_07_sympathy_lane_round_trip(env):
    """A2 related_opportunities -> signal.synthetic -> A1 synthetic triage
    (derived_from lineage) -> signal.analyst with ticker override."""
    await seed_item(env, "alpaca:7004", "Acme acquisition confirmed at $45")
    reply = thesis_reply(related_opportunities=[
        {"ticker": "SUPL", "relation": "supplier",
         "rationale": "sole supplier of Acme's key component"}])
    a2 = A2Service(A2_CFG, backend=StubBackend([reply]),
                   md=hot_market(), store=env["store"])
    await process("signal.analyst", "alpaca:7004:1", triaged_msg("alpaca:7004"), a2)

    # synthetic enqueued with lineage
    rows = await q(env, """SELECT payload->'body'->>'synthetic_id',
                                  payload->'body'->>'derived_from_decision'
                           FROM queue.messages WHERE queue_name='signal.synthetic'""")
    syn_id, parent_dec = rows[0]
    assert syn_id.startswith("syn-") and syn_id.endswith("-SUPL")

    # A1 consumes it via handle_synthetic
    a1 = A1Service(A1_CFG, backend=StubBackend(
        [json.dumps({"material": True, "tickers": ["SUPL"], "direction_hint": "up",
                     "urgency": "medium", "novelty_score": 0.6, "confidence": 0.7,
                     "reason": "supplier exposure to confirmed deal"})]),
        store=env["store"])
    msg = await claim("signal.synthetic", "test-a1")
    assert msg is not None
    await a1.handle_synthetic(msg)
    await ack(msg.msg_id)

    rows = await q(env, """SELECT signal_id, ticker, derived_from,
                                  payload->>'synthetic'
                           FROM journal.decisions
                           WHERE signal_id = %s AND stage='TRIAGE'""", syn_id)
    sid, ticker, derived_from, synthetic = rows[0]
    assert ticker == "SUPL" and synthetic == "true"
    assert derived_from == int(parent_dec)       # lineage to the A2 decision

    rows = await q(env, """SELECT payload->'envelope'->'trace'->>'ticker'
                           FROM queue.messages
                           WHERE queue_name IN ('signal.analyst','signal.overnight')
                             AND dedup_key = %s""", syn_id)
    assert rows[0][0] == "SUPL"                  # A2 will analyze the sympathy name


async def test_08_gate_and_thesis_share_config_version(env):
    rows = await q(env, """SELECT count(DISTINCT config_version) FROM journal.decisions""")
    assert rows[0][0] == 1

