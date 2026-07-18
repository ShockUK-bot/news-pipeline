"""Phase 5 integration against real PostgreSQL 16: A12 Position Guard through
the actual service with StubBackend + FakeData.

Covers: HOLD verdict -> GUARD decision + guard_ledger row and NOTHING else
(no orders, no position mutation — verdict-only v1); watch-list-hit EXIT;
staleness gate (EXPIRED, zero model calls) — the pre-Phase-5 backlog drain
path; closed/unknown positions (NO_POSITION); invalid model output -> REJECT
after retries; cross-field discipline (intact=false + hold is invalid, model
gets a retry); redelivery no-op; multi-position fan-out; analyst-slot-down
degradation (ALERT_ONLY, baseline §11.2).
"""
import json
import os
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"
os.environ["BROKER"] = "fake"

from common.clock import utcnow
from common.db import get_pool, jb
from common.journal import register_config_version
from common.marketdata import FakeData
from common.queue import ack, claim, enqueue
from a1_triage.backends import StubBackend
from a12_guard.service import A12Service

pytestmark = pytest.mark.asyncio(loop_scope="session")

CFG = {"model": {"backend": "stub", "retries_on_invalid": 1},
       "guard": {"max_age_minutes": 240},
       "wake": {"enabled": False}}


def verdict(**over):
    base = {"thesis_intact": True, "recommended_action": "hold",
            "urgency": "low", "confidence": 0.7, "watch_hits": [],
            "reason": "reiteration of known information; thesis unaffected"}
    base.update(over)
    return json.dumps(base)


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.regime_snapshots, journal.intents, journal.orders,
                     journal.fills, journal.positions, journal.position_events,
                     journal.exits, journal.guard_ledger, journal.audit,
                     news.cluster_members, news.clusters, news.news_items,
                     queue.messages
                     RESTART IDENTITY CASCADE""")
    await register_config_version("phase5 guard integration test")
    return {"pool": pool}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args)
        return await cur.fetchall()


async def seed_item(env, item_id, headline, age_minutes=5, revision=1,
                    is_correction=False, symbols=("ACME",)):
    ts = utcnow() - timedelta(minutes=age_minutes)
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO news.news_items
               (item_id, revision, is_correction, source, source_tier,
                headline, summary, content_hash, symbols, published_ts,
                received_ts)
               VALUES (%s,%s,%s,'alpaca_benzinga',2,%s,'summary',%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
            (item_id, revision, is_correction, headline, f"hash-{item_id}-{revision}",
             list(symbols), ts, ts))


async def seed_position(env, ticker="ACME", news_checkable=("counterparty_denial",),
                        status="OPEN"):
    """Thesis decision -> intent -> position, minimally but with real FKs."""
    async with env["pool"].connection() as c:
        cur = await c.execute(
            """INSERT INTO journal.decisions
                 (signal_id, stage, agent, action, ticker, payload, reason,
                  config_version)
               SELECT %s,'ANALYST','A2','THESIS',%s,%s,'test thesis',
                      config_version FROM journal.config_versions LIMIT 1
               RETURNING decision_id""",
            (f"seed:{ticker}", ticker,
             jb({"thesis": {"ticker": ticker, "direction": "up",
                            "magnitude_est": 0.05,
                            "expected_move_window": "2_sessions",
                            "horizon": "SHORT", "confidence": 0.7,
                            "priced_in_assessment": "partial",
                            "source_risk": "low",
                            "invalidation": {
                                "machine_checkable": ["close_below_prenews"],
                                "news_checkable": list(news_checkable)},
                            "reason": "acquisition premium not yet priced"}})))
        thesis_id = (await cur.fetchone())[0]
        intent_id = f"it-{ticker}-{thesis_id}"
        await c.execute(
            """INSERT INTO journal.intents
                 (intent_id, decision_id, ticker, side, qty, limit_price,
                  config_version)
               SELECT %s,%s,%s,'BUY',50,100.0, config_version
               FROM journal.config_versions LIMIT 1""",
            (intent_id, thesis_id, ticker))
        cur = await c.execute(
            """INSERT INTO journal.positions
                 (ticker, horizon, profile, status, opened_ts, entry_intent_id,
                  thesis_decision_id, item_id, qty_initial, qty_open,
                  avg_entry, initial_stop, r_unit, exit_policy, config_version)
               SELECT %s,'SHORT','short_term_v1',%s, now() - interval '1 day',
                      %s,%s,'seed-item',50,50,100.0,96.0,4.0,%s, config_version
               FROM journal.config_versions LIMIT 1
               RETURNING position_id""",
            (ticker, status, intent_id, thesis_id,
             jb({"profile": "short_term_v1", "current_stop": 96.0,
                 "news_invalidations": list(news_checkable),
                 "machine_invalidations": ["close_below_prenews"]})))
        return (await cur.fetchone())[0]


def guard_msg(item_id, position_ids, tickers=("ACME",), revision=1):
    return {"envelope": {"msg_schema": "signal.triaged/1", "producer": "A1",
                         "trace": {"signal_id": item_id, "item_id": item_id,
                                   "revision": revision}},
            "body": {"item_ref": {"item_id": item_id, "revision": revision,
                                  "cluster_id": 1},
                     "triage": {"material": True, "tickers": list(tickers),
                                "direction_hint": "down", "urgency": "high",
                                "novelty_score": 0.8, "confidence": 0.9,
                                "reason": "touches held name"},
                     "routing": {"market_open": True,
                                 "position_ids": list(position_ids),
                                 "thesis_matches": [], "priority_score": 20}}}


def svc(replies):
    return A12Service(CFG, backend=StubBackend(replies), md=FakeData())


async def run_msg(payload, service, key=None):
    key = key or f"{payload['body']['item_ref']['item_id']}:g"
    await enqueue("signal.guard", key, payload, priority=0)
    msg = await claim("signal.guard", "test-a12")
    assert msg is not None
    await service.handle(msg)
    await ack(msg.msg_id)
    return msg


# ---------------------------------------------------------------------------

async def test_01_hold_verdict_journals_and_ledgers_only(env):
    pid = await seed_position(env, "ACME")
    await seed_item(env, "n:hold-1", "Acme reiterates guidance")
    backend = StubBackend([verdict()])
    service = A12Service(CFG, backend=backend, md=FakeData())
    await run_msg(guard_msg("n:hold-1", [pid]), service)

    rows = await q(env, """SELECT action, ticker, confidence, payload
                           FROM journal.decisions WHERE stage='GUARD'""")
    assert len(rows) == 1
    action, ticker, confidence, payload = rows[0]
    assert (action, ticker) == ("HOLD", "ACME")
    assert confidence == pytest.approx(0.7)
    assert payload["position_id"] == pid
    assert payload["position"]["watch_list"] == ["counterparty_denial"]

    ledger = await q(env, """SELECT position_id, thesis_intact,
                                    recommended_action, auto_executed,
                                    action_taken
                             FROM journal.guard_ledger""")
    assert ledger == [(pid, True, "HOLD", False, "JOURNALED")]
    # verdict-only v1: nothing else moved
    assert await q(env, "SELECT count(*) FROM journal.orders") == [(0,)]
    assert await q(env, "SELECT count(*) FROM journal.position_events") == [(0,)]


async def test_02_watchlist_hit_exit(env):
    pid = await seed_position(env, "DENY", news_checkable=("counterparty_denial",))
    await seed_item(env, "n:deny-1", "Counterparty denies Acme talks",
                    symbols=("DENY",))
    service = svc([verdict(thesis_intact=False, recommended_action="exit",
                           urgency="high", confidence=0.9,
                           watch_hits=["counterparty_denial"],
                           reason="the entry story is denied outright")])
    await run_msg(guard_msg("n:deny-1", [pid], tickers=("DENY",)), service)

    rows = await q(env, """SELECT action, payload FROM journal.decisions
                           WHERE stage='GUARD' AND ticker='DENY'""")
    assert len(rows) == 1
    assert rows[0][0] == "EXIT"
    assert rows[0][1]["verdict"]["watch_hits"] == ["counterparty_denial"]
    ledger = await q(env, """SELECT thesis_intact, recommended_action, urgency
                             FROM journal.guard_ledger WHERE position_id=%s""", pid)
    assert ledger == [(False, "EXIT", "high")]


async def test_03_intact_false_hold_is_invalid_then_retried(env):
    pid = await seed_position(env, "XFLD")
    await seed_item(env, "n:xf-1", "Xfld thesis wobble", symbols=("XFLD",))
    backend = StubBackend([
        verdict(thesis_intact=False, recommended_action="hold"),   # invalid
        verdict(thesis_intact=False, recommended_action="tighten_stop",
                urgency="medium"),
    ])
    service = A12Service(CFG, backend=backend, md=FakeData())
    await run_msg(guard_msg("n:xf-1", [pid], tickers=("XFLD",)), service)
    rows = await q(env, """SELECT action FROM journal.decisions
                           WHERE stage='GUARD' AND ticker='XFLD'""")
    assert rows == [("TIGHTEN_STOP",)]
    assert len(backend.calls) == 2                     # retry happened


async def test_04_invalid_output_exhausts_to_reject(env):
    pid = await seed_position(env, "RJCT")
    await seed_item(env, "n:rj-1", "Rjct news", symbols=("RJCT",))
    service = svc(["not json at all", '{"nope": 1}'])
    await run_msg(guard_msg("n:rj-1", [pid], tickers=("RJCT",)), service)
    rows = await q(env, """SELECT action, payload FROM journal.decisions
                           WHERE stage='GUARD' AND ticker='RJCT'""")
    assert len(rows) == 1
    assert rows[0][0] == "REJECT"
    assert rows[0][1]["attempts"] == 2
    assert await q(env, """SELECT count(*) FROM journal.guard_ledger
                           WHERE position_id=%s""", pid) == [(0,)]


async def test_05_stale_signal_expires_without_model_call(env):
    pid = await seed_position(env, "OLDN")
    await seed_item(env, "n:old-1", "Ancient news", age_minutes=600,
                    symbols=("OLDN",))
    backend = StubBackend([verdict()])
    service = A12Service(CFG, backend=backend, md=FakeData())
    await run_msg(guard_msg("n:old-1", [pid], tickers=("OLDN",)), service)
    rows = await q(env, """SELECT action, payload FROM journal.decisions
                           WHERE stage='GUARD' AND signal_id='n:old-1'""")
    assert len(rows) == 1
    assert rows[0][0] == "EXPIRED"
    assert rows[0][1]["position_ids_routed"] == [pid]
    assert backend.calls == []                          # zero tokens


async def test_06_closed_position_is_no_position(env):
    pid = await seed_position(env, "GONE", status="CLOSED")
    await seed_item(env, "n:gone-1", "Gone news", symbols=("GONE",))
    backend = StubBackend([verdict()])
    service = A12Service(CFG, backend=backend, md=FakeData())
    await run_msg(guard_msg("n:gone-1", [pid], tickers=("GONE",)), service)
    rows = await q(env, """SELECT action FROM journal.decisions
                           WHERE stage='GUARD' AND signal_id='n:gone-1'""")
    assert rows == [("NO_POSITION",)]
    assert backend.calls == []


async def test_07_redelivery_is_noop(env):
    pid = await seed_position(env, "DUPE")
    await seed_item(env, "n:dupe-1", "Dupe news", symbols=("DUPE",))
    service = svc([verdict(), verdict()])
    payload = guard_msg("n:dupe-1", [pid], tickers=("DUPE",))
    await run_msg(payload, service, key="n:dupe-1:g1")
    await run_msg(payload, service, key="n:dupe-1:g2")   # redelivery
    rows = await q(env, """SELECT count(*) FROM journal.decisions
                           WHERE stage='GUARD' AND signal_id='n:dupe-1'""")
    assert rows == [(1,)]
    assert await q(env, """SELECT count(*) FROM journal.guard_ledger
                           WHERE position_id=%s""", pid) == [(1,)]


async def test_08_multi_position_message_fans_out(env):
    p1 = await seed_position(env, "TWIN")
    p2 = await seed_position(env, "TWOB")
    await seed_item(env, "n:multi-1", "Sector-wide recall touches both",
                    symbols=("TWIN", "TWOB"))
    service = svc([verdict(), verdict(thesis_intact=False,
                                      recommended_action="tighten_stop",
                                      urgency="medium")])
    await run_msg(guard_msg("n:multi-1", [p1, p2], tickers=("TWIN", "TWOB")),
                  service)
    rows = await q(env, """SELECT (payload->>'position_id')::bigint, action
                           FROM journal.decisions
                           WHERE stage='GUARD' AND signal_id='n:multi-1'
                           ORDER BY 1""")
    assert rows == [(p1, "HOLD"), (p2, "TIGHTEN_STOP")]
    assert await q(env, "SELECT count(*) FROM journal.guard_ledger") is not None


async def test_09_transport_error_degrades_to_alert_only(env):
    pid = await seed_position(env, "DOWN")
    await seed_item(env, "n:down-1", "News while model is down",
                    symbols=("DOWN",))

    class DeadBackend:
        model_id = "dead"
        async def complete(self, messages, schema):
            raise httpx.ConnectError("connection refused")

    service = A12Service(CFG, backend=DeadBackend(), md=FakeData())
    await run_msg(guard_msg("n:down-1", [pid], tickers=("DOWN",)), service)
    rows = await q(env, """SELECT action, payload FROM journal.decisions
                           WHERE stage='GUARD' AND signal_id='n:down-1'""")
    assert len(rows) == 1
    assert rows[0][0] == "ALERT_ONLY"
    assert rows[0][1]["positions"] == [{"position_id": pid, "ticker": "DOWN"}]
    health = await q(env, """SELECT status FROM journal.health
                             WHERE component='guard'""")
    assert health == [("DEGRADED",)]
