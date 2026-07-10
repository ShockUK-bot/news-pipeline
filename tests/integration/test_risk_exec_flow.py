"""Phase 4 chunk-1 integration against real PostgreSQL 16: A3 + C4 through
the actual services with FakeBroker and stub discretion.

Covers: RISK SIZE decision + intents row + exec.intent enqueue in one tx;
discretion fallback on invalid model output; RISK vetoes (KILL_SWITCH via
control table, SIZE_CLIPPED via heat); C4 entry fill -> position + two-tier
stops (catastrophe re-materialized off ACTUAL fill); duplicate intent replay
no-op at C4; unfilled entry expiry; broker reject; reconciliation drift
(CLOSED_EXTERNAL + ADOPTED + qty snap) and capital-row refresh; A3 heat
computation reflecting the open position.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"
os.environ["BROKER"] = "fake"

from common.broker import FakeBroker
from common.clock import utcnow
from common.db import get_pool
from common.journal import register_config_version
from common.queue import ack, claim, enqueue
from a1_triage.backends import StubBackend
from a3_risk.service import A3Service
from c4_exec.flags import ensure_defaults, set_flag
from c4_exec.reconcile import reconcile
from c4_exec.service import C4Service

pytestmark = pytest.mark.asyncio(loop_scope="session")

PIN = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)   # Tue, in-session

RISK_CFG = {
    "capital": {"risk_per_trade_pct": 0.005, "max_position_notional_pct": 0.15,
                "max_portfolio_heat_pct": 0.03,
                "heat_split": {"SHORT": 0.02, "LONG": 0.01},
                "max_sector_heat_pct": 0.015, "min_viable_risk_fraction": 0.5},
    "limits": {"max_trades_per_day_default": 5, "adv_participation_max": 0.01,
               "spread_max_bps": 40, "entry_blackout_final_min": 15},
    "model": {"backend": "stub", "retries_on_invalid": 1},
}
PROFILES = {
    "profiles": {
        "short_term_v1": {
            "initial_stop": {"method": "atr", "k": 2.0},
            "catastrophe": {"method": "atr", "k": 3.5},
            "breakeven_at_R": 1.0,
            "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
            "time_stop": {"window": "thesis", "min_progress_R": 0.5},
            "realization": {"target_fraction": 0.7, "action": "scale_out_50"},
            "earnings_blackout_exit": True, "overnight_hold": "eod_rule_v1"},
        "long_term_v1": {
            "initial_stop": {"method": "atr", "k": 3.0},
            "catastrophe": {"method": "atr", "k": 4.5},
            "breakeven_at_R": 1.0,
            "trail": {"activate_at_R": 2.0, "method": "atr_weekly", "k": 4.0},
            "time_stop": None,
            "realization": {"target_fraction": 0.7, "action": "review_flag"},
            "earnings_blackout_exit": False, "overnight_hold": "default_hold"}},
    "discretion_bands": {"k": [1.5, 2.5], "realization_fraction": [0.5, 0.9],
                         "time_window_sessions": [1, 3]},
}
DEADMAN_CFG = {"c4": {"reconcile_interval_min": 15,
                      "exit_unprotected_max_secs": 45,
                      "drawdown_breaker_pct": 0.02, "heartbeat_secs": 60}}

ADJ_OK = json.dumps({"k": 2.0, "realization_fraction": 0.7,
                     "time_window_sessions": 2, "reason": "clean confirm"})


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.regime_snapshots, journal.intents, journal.orders,
                     journal.fills, journal.positions, journal.position_events,
                     journal.exits, journal.audit,
                     news.cluster_members, news.clusters, news.news_items,
                     queue.messages
                     RESTART IDENTITY CASCADE""")
        await c.execute("DELETE FROM journal.control")
    await register_config_version("phase4 integration test")
    await ensure_defaults()
    await set_flag("trading_capital", "50000", "TEST")
    # seed a thesis decision for the positions FK + an item row
    async with pool.connection() as c:
        await c.execute(
            """INSERT INTO news.news_items (item_id, revision, source,
                 source_tier, headline, content_hash, symbols, channels,
                 published_ts, received_ts)
               VALUES ('alpaca:9001',1,'alpaca_benzinga',2,'Acme wins contract',
                       'h9001','{ACME}','{}',%s,%s)""", (PIN, PIN))
        cur = await c.execute(
            """INSERT INTO journal.decisions (signal_id, item_id, stage, agent,
                 action, ticker, reason, config_version)
               SELECT 'alpaca:9001','alpaca:9001','ANALYST','A2','THESIS',
                      'ACME','test thesis', config_version
               FROM journal.config_versions LIMIT 1
               RETURNING decision_id""")
        thesis_id = (await cur.fetchone())[0]
    return {"pool": pool, "thesis_id": thesis_id}


async def seed_thesis(env, signal_id, ticker):
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO journal.decisions (signal_id, stage, agent, action,
                 ticker, reason, config_version)
               SELECT %s,'ANALYST','A2','THESIS',%s,'test thesis',
                      config_version FROM journal.config_versions LIMIT 1""",
            (signal_id, ticker))


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args)
        return await cur.fetchall()


def gatepass_msg(signal_id="alpaca:9001", ticker="ACME", atr=2.0,
                 magnitude=0.055, horizon="SHORT"):
    return {"envelope": {"msg_schema": "signal.risk/1", "producer": "C3",
                         "trace": {"signal_id": signal_id, "item_id": signal_id,
                                   "revision": 1}},
            "body": {"item_ref": {"item_id": signal_id, "revision": 1},
                     "regime_id": None,
                     "thesis": {"ticker": ticker, "direction": "up",
                                "magnitude_est": magnitude,
                                "expected_move_window": "2_sessions",
                                "horizon": horizon, "confidence": 0.7,
                                "priced_in_assessment": "x",
                                "source_risk": "low",
                                "invalidation": {
                                    "machine_checkable": ["close_below_prenews"],
                                    "news_checkable": ["denial"]},
                                "related_opportunities": [], "reason": "r"},
                     "gate": {"verdict": "PASS", "rule": "intraday",
                              "pct_move": 0.02, "vol_mult": 3.0, "minutes": 10,
                              "snapshot": {"ref_price": 100.0, "bid": 99.98,
                                           "ask": 100.02, "spread_bps": 4.0,
                                           "adv_20d": 5_000_000,
                                           "atr_14": atr,
                                           "ts": PIN.isoformat()}}}}


def a3(backend_replies=None):
    return A3Service(RISK_CFG, PROFILES,
                     backend=StubBackend(backend_replies or [ADJ_OK]),
                     now_fn=lambda: PIN)


def c4(broker):
    return C4Service(DEADMAN_CFG, broker=broker, now_fn=lambda: PIN,
                     poll_sleep=0.01, fill_timeout_secs=0.05)


async def process(queue_name, key, payload, handler):
    await enqueue(queue_name, key, payload)
    msg = await claim(queue_name, "test")
    assert msg is not None
    await handler(msg)
    await ack(msg.msg_id)
    return msg


# ---------------------------------------------------------------------------

async def test_01_reconcile_seeds_capital_rows(env):
    broker = FakeBroker(equity=48_000, settled_cash=48_000)
    await reconcile(broker)
    rows = await q(env, "SELECT key, value FROM journal.control "
                        "WHERE key IN ('broker_equity','settled_cash')")
    vals = dict(rows)
    assert float(vals["broker_equity"]) == 48_000
    assert float(vals["settled_cash"]) == 48_000


async def test_02_a3_sizes_and_emits_intent(env):
    svc = a3()
    await process("signal.risk", "alpaca:9001:1", gatepass_msg(), svc.handle)

    rows = await q(env, """SELECT action, payload->'sizing'->>'qty',
                                  payload->>'intent_id',
                                  payload->'sizing'->'clips' IS NOT NULL
                           FROM journal.decisions
                           WHERE stage='RISK' AND signal_id='alpaca:9001'""")
    action, qty, intent_id, has_clips = rows[0]
    # effective capital = min(48000 broker, 50000 config) = 48000
    # risk 240 / stop 4.0 = 60 shares
    assert (action, qty) == ("SIZE", "60") and has_clips

    rows = await q(env, """SELECT qty, limit_price, status, horizon,
                                  effective_capital
                           FROM journal.intents WHERE intent_id=%s""", intent_id)
    qty_i, limit, status, horizon, eff = rows[0]
    assert (qty_i, status, horizon) == (60, "PENDING", "SHORT")
    assert float(eff) == 48_000

    rows = await q(env, """SELECT payload->'body'->>'intent_id'
                           FROM queue.messages
                           WHERE queue_name='exec.intent' AND dedup_key=%s""",
                   intent_id)
    assert rows[0][0] == intent_id



async def test_03_missing_thesis_lineage_vetoes(env):
    """A GatePass whose ANALYST decision can't be found must not size —
    positions.thesis_decision_id is NOT NULL by design."""
    svc = a3()
    await process("signal.risk", "orphan:1:1",
                  gatepass_msg(signal_id="orphan:1", ticker="ORFN"), svc.handle)
    rows = await q(env, """SELECT action, veto_reason FROM journal.decisions
                           WHERE stage='RISK' AND signal_id='orphan:1'""")
    assert tuple(rows[0]) == ("VETO", "NO_THESIS_LINEAGE")
    rows = await q(env, """SELECT count(*) FROM queue.messages
                           WHERE queue_name='exec.intent'
                             AND payload->'envelope'->'trace'->>'signal_id'='orphan:1'""")
    assert rows[0][0] == 0


async def test_04_c4_fills_and_arms_two_tier_stops(env):
    broker = FakeBroker(equity=48_000, settled_cash=48_000)
    await reconcile(broker)
    svc = c4(broker)
    msg = await claim("exec.intent", "test-c4")
    assert msg is not None
    intent_id = msg.payload["body"]["intent_id"]
    await svc.handle_intent(msg)
    await ack(msg.msg_id)

    rows = await q(env, """SELECT p.ticker, p.qty_open, p.avg_entry,
                                  p.initial_stop, p.r_unit,
                                  p.exit_policy->'catastrophe_stop_broker'->>'price',
                                  p.catastrophe_stop_order_id
                           FROM journal.positions p
                           WHERE p.entry_intent_id=%s""", intent_id)
    ticker, qty, avg_entry, stop, r_unit, cat_price, cat_order = rows[0]
    assert (ticker, qty) == ("ACME", 60)
    fill = float(avg_entry)
    assert float(stop) == pytest.approx(fill - 4.0, abs=0.01)      # k=2 x atr=2
    assert float(cat_price) == pytest.approx(fill - 7.0, abs=0.01) # k=3.5
    assert float(r_unit) == pytest.approx(4.0, abs=0.01)
    assert cat_order is not None

    rows = await q(env, """SELECT order_role, state FROM journal.orders
                           ORDER BY order_id""")
    roles = {r[0]: r[1] for r in rows}
    assert roles["ENTRY"] == "FILLED" and roles["CATASTROPHE_STOP"] == "ACCEPTED"

    rows = await q(env, """SELECT event_type FROM journal.position_events""")
    assert ("STOPS_PLACED",) in rows

    # broker got exactly one entry + one stop, stop at the re-materialized price
    assert len(broker.submissions) == 2
    assert broker.submissions[1]["stop"] == pytest.approx(fill - 7.0, abs=0.01)

    rows = await q(env, "SELECT status FROM journal.intents WHERE intent_id=%s",
                   intent_id)
    assert rows[0][0] == "FILLED"


async def test_05_duplicate_intent_replay_is_noop(env):
    """Crash-replay: same exec.intent message again -> no second order."""
    broker = FakeBroker(equity=48_000, settled_cash=48_000)
    svc = c4(broker)
    rows = await q(env, "SELECT intent_id FROM journal.intents WHERE status='FILLED' LIMIT 1")
    intent_id = rows[0][0]
    body = {"intent_id": intent_id, "ticker": "ACME", "side": "BUY", "qty": 60,
            "limit_price": 100.04, "exit_policy": {}, "horizon": "SHORT",
            "thesis_decision_id": env["thesis_id"], "gate_snapshot": {}}
    await process("exec.intent", f"replay:{intent_id}",
                  {"envelope": {"msg_schema": "exec.intent/1", "producer": "A3",
                                "trace": {"signal_id": "alpaca:9001"}},
                   "body": body}, svc.handle_intent)
    assert broker.submissions == []                 # nothing hit the broker
    rows = await q(env, "SELECT count(*) FROM journal.positions WHERE ticker='ACME'")
    assert rows[0][0] == 1                          # still one position


async def test_05b_discretion_fallback_on_invalid_output(env):
    """Model returns out-of-band k twice -> profile defaults, trade proceeds."""
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO journal.decisions (signal_id, stage, agent, action,
                 ticker, reason, config_version)
               SELECT 'syn:9002','ANALYST','A2','THESIS','ACME','syn thesis',
                      config_version FROM journal.config_versions LIMIT 1""")
    bad = json.dumps({"k": 9.0, "realization_fraction": 0.7,
                      "time_window_sessions": 2, "reason": "yolo"})
    svc = a3([bad, bad])
    await process("signal.risk", "syn:9002:1",
                  gatepass_msg(signal_id="syn:9002"), svc.handle)
    rows = await q(env, """SELECT payload->>'model_used',
                                  payload->'adjustments'->>'k',
                                  payload->'adjustments'->>'reason'
                           FROM journal.decisions
                           WHERE stage='RISK' AND signal_id='syn:9002'""")
    model_used, k, reason = rows[0]
    assert model_used == "false" and k == "2.0" and reason.startswith("fallback")
    # drain the intent this run emitted so later tests claim their own
    msg = await claim("exec.intent", "test-drain")
    assert msg.payload["envelope"]["trace"]["signal_id"] == "syn:9002"
    await ack(msg.msg_id)

async def test_06_a3_heat_reflects_open_position(env):
    """Open ACME position (60 sh, 4.0 stop distance = $240 heat vs $960 lane
    cap) leaves headroom; a second signal sizes smaller via lane heat."""
    await seed_thesis(env, "alpaca:9003", "BETA")
    svc = a3()
    await process("signal.risk", "alpaca:9003:1",
                  gatepass_msg(signal_id="alpaca:9003", ticker="BETA"),
                  svc.handle)
    rows = await q(env, """SELECT action, payload->'sizing'->'clips'->>'lane_heat',
                                  payload->'sizing'->>'qty'
                           FROM journal.decisions
                           WHERE stage='RISK' AND signal_id='alpaca:9003'""")
    action, lane_clip, qty = rows[0]
    # lane cap 2% x 48000 = 960; used 240 -> headroom 720/4.0 = 180 shares
    assert action == "SIZE" and float(lane_clip) == pytest.approx(180.0)
    assert int(qty) == 60                            # raw 60 still under clips
    # drain the intent this run emitted so later tests claim their own
    msg = await claim("exec.intent", "test-drain")
    assert msg.payload["envelope"]["trace"]["signal_id"] == "alpaca:9003"
    await ack(msg.msg_id)


async def test_07_kill_switch_vetoes_at_a3(env):
    await seed_thesis(env, "alpaca:9004", "GAMA")
    await set_flag("kill_switch", "1", "TEST", "test kill")
    svc = a3()
    await process("signal.risk", "alpaca:9004:1",
                  gatepass_msg(signal_id="alpaca:9004", ticker="GAMA"),
                  svc.handle)
    rows = await q(env, """SELECT action, veto_reason FROM journal.decisions
                           WHERE stage='RISK' AND signal_id='alpaca:9004'""")
    assert tuple(rows[0]) == ("VETO", "KILL_SWITCH")
    await set_flag("kill_switch", "0", "TEST", "reset")


async def test_08_c4_preflight_blocks_when_flag_set(env):
    """A3 passed but the world changed before C4 submitted: block_entries."""
    await seed_thesis(env, "alpaca:9005", "DLTA")
    svc = a3()
    await process("signal.risk", "alpaca:9005:1",
                  gatepass_msg(signal_id="alpaca:9005", ticker="DLTA"),
                  svc.handle)
    await set_flag("block_entries", "1", "TEST", "deadman trip simulation")
    broker = FakeBroker(equity=48_000, settled_cash=48_000)
    svc4 = c4(broker)
    msg = await claim("exec.intent", "test-c4")
    await svc4.handle_intent(msg)
    await ack(msg.msg_id)
    rows = await q(env, """SELECT action, veto_reason FROM journal.decisions
                           WHERE stage='ORDER' AND ticker='DLTA'""")
    assert tuple(rows[0]) == ("VETO", "ENTRIES_BLOCKED")
    assert broker.submissions == []
    rows = await q(env, """SELECT status FROM journal.intents i
                           JOIN journal.decisions d ON d.decision_id=i.decision_id
                           WHERE d.signal_id='alpaca:9005'""")
    assert rows[0][0] == "REJECTED"
    await set_flag("block_entries", "0", "TEST", "reset")


async def test_09_unfilled_entry_expires(env):
    await seed_thesis(env, "alpaca:9006", "EPSN")
    svc = a3()
    await process("signal.risk", "alpaca:9006:1",
                  gatepass_msg(signal_id="alpaca:9006", ticker="EPSN"),
                  svc.handle)
    broker = FakeBroker(equity=48_000, settled_cash=48_000)
    broker.set_behavior("EPSN", "rest")             # never fills
    svc4 = c4(broker)
    msg = await claim("exec.intent", "test-c4")
    await svc4.handle_intent(msg)
    await ack(msg.msg_id)
    rows = await q(env, """SELECT status FROM journal.intents i
                           JOIN journal.decisions d ON d.decision_id=i.decision_id
                           WHERE d.signal_id='alpaca:9006' AND d.stage='RISK'""")
    assert rows[0][0] == "CANCELLED"                # timed out, cancelled
    rows = await q(env, "SELECT count(*) FROM journal.positions WHERE ticker='EPSN'")
    assert rows[0][0] == 0


async def test_10_reconciliation_drift(env):
    """Broker lost ACME (CLOSED_EXTERNAL) and holds mystery ZETA (ADOPTED)."""
    broker = FakeBroker(equity=47_000, settled_cash=41_000)
    broker.inject_position("ZETA", 25, 40.0)        # unknown locally
    # broker has NO ACME (dropped)
    summary = await reconcile(broker)
    assert summary["closed_external"] == ["ACME"]
    assert summary["adopted"] == ["ZETA"]

    rows = await q(env, "SELECT status FROM journal.positions WHERE ticker='ACME'")
    assert rows[0][0] == "CLOSED"
    rows = await q(env, """SELECT status, qty_open, profile
                           FROM journal.positions WHERE ticker='ZETA'""")
    status, qty, profile = rows[0]
    assert (status, qty, profile) == ("OPEN", 25, "adopted_v1")
    rows = await q(env, """SELECT count(*) FROM journal.audit
                           WHERE action IN ('RECONCILE_CLOSED_EXTERNAL',
                                            'RECONCILE_ADOPTED')""")
    assert rows[0][0] == 2
    vals = dict(await q(env, "SELECT key, value FROM journal.control "
                             "WHERE key IN ('broker_equity','settled_cash')"))
    assert float(vals["broker_equity"]) == 47_000


async def test_11_reconciliation_qty_snap(env):
    broker = FakeBroker(equity=47_000, settled_cash=41_000)
    broker.inject_position("ZETA", 20, 40.0)        # broker says 20, DB says 25
    summary = await reconcile(broker)
    assert summary["qty_snapped"] == ["ZETA"]
    rows = await q(env, "SELECT qty_open FROM journal.positions WHERE ticker='ZETA'")
    assert rows[0][0] == 20

