"""Phase 4 chunk-2 integration on live PG16: the exit engine through real
persistence with FakeBroker.

Covers: trail ratchet persisted + events; stop exit with cancel-catastrophe
-> fill -> exits attribution -> position CLOSED; reinstatement when the exit
doesn't fill inside the window; catastrophe-already-filled race; scale-out
re-placing the catastrophe for the remainder; MIP invalidation fired
end-to-end (armed forms journaled); D1 overnight EXIT flow + forced-hold
reprice; drawdown breaker trip on marked losses; dead-man ladder block +
recovery + ownership rule.
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
from common.db import get_pool, jb
from common.journal import register_config_version
from c4_exec.breaker import check_breaker
from c4_exec.deadman import check as deadman_check
from c4_exec.engine import PositionEngine
from c4_exec.flags import ensure_defaults, get_flag, set_flag
from c4_exec.state import open_positions

pytestmark = pytest.mark.asyncio(loop_scope="session")

PIN = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)

POLICY = {
    "profile": "short_term_v1",
    "initial_stop": {"method": "atr", "k": 2.0, "price": 96.0},
    "catastrophe_stop_broker": {"k": 3.5, "price": 93.0},
    "breakeven_at_R": 1.0,
    "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
    "time_stop": {"window": "2_sessions", "min_progress_R": 0.5},
    "realization": {"target_fraction": 0.7, "action": "scale_out_50"},
    "machine_invalidations": ["close_below_prenews"],
    "magnitude_est": 0.055,
    "atr_14": 2.0,
}
ON_CFG = {"hold_min_unrealized_R": 0.3, "young_max_age_sessions": 1,
          "young_max_realized_fraction": 0.5, "check_time_et": "15:45"}


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.regime_snapshots, journal.intents, journal.orders,
                     journal.fills, journal.positions, journal.position_events,
                     journal.exits, journal.audit, journal.health,
                     news.news_items, queue.messages RESTART IDENTITY CASCADE""")
        await c.execute("DELETE FROM journal.control")
    await register_config_version("phase4 chunk2 integration")
    await ensure_defaults()
    await set_flag("trading_capital", "50000", "TEST")
    await set_flag("broker_equity", "48000", "TEST")
    await set_flag("settled_cash", "48000", "TEST")
    return {"pool": pool}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args)
        return await cur.fetchall()


async def seed_position(env, broker, ticker="ACME", qty=60, avg_entry=100.0,
                        policy_over=None, opened_ts=None,
                        prenews: float = 99.0) -> dict:
    """Create an OPEN position + broker state + resting catastrophe, the way
    chunk-1 entry flow leaves the world. Returns the positions row dict."""
    policy = json.loads(json.dumps(POLICY))
    if policy_over:
        policy.update(policy_over)
    # ArmContext for stdlib close_below_prenews needs prenews_price -- keep it
    # in policy for the engine's ArmContext build (engine reads atr_14; the
    # stdlib predicate resolves prenews via ctx.prenews_price)
    policy["prenews_price"] = prenews
    pool = env["pool"]
    async with pool.connection() as c:
        cur = await c.execute(
            """INSERT INTO journal.decisions (signal_id, stage, agent, action,
                 ticker, reason, config_version)
               SELECT %s,'ANALYST','A2','THESIS',%s,'t', config_version
               FROM journal.config_versions LIMIT 1 RETURNING decision_id""",
            (f"sig:{ticker}", ticker))
        thesis = (await cur.fetchone())[0]
        await c.execute(
            """INSERT INTO journal.intents (intent_id, decision_id, ticker,
                 side, qty, limit_price, status, config_version)
               SELECT %s,%s,%s,'BUY',%s,%s,'FILLED', config_version
               FROM journal.config_versions LIMIT 1""",
            (f"int-{ticker}", thesis, ticker, qty, avg_entry))
        cur = await c.execute(
            """INSERT INTO journal.positions (ticker, horizon, profile, status,
                 opened_ts, entry_intent_id, thesis_decision_id, qty_initial,
                 qty_open, avg_entry, initial_stop, r_unit, exit_policy,
                 config_version)
               SELECT %s,'SHORT','short_term_v1','OPEN',%s,%s,%s,%s,%s,%s,%s,
                      %s,%s, config_version
               FROM journal.config_versions LIMIT 1 RETURNING position_id""",
            (ticker, opened_ts or PIN, f"int-{ticker}", thesis, qty, qty,
             avg_entry, policy["initial_stop"]["price"],
             avg_entry - policy["initial_stop"]["price"], jb(policy)))
        pid = (await cur.fetchone())[0]
    broker.inject_position(ticker, qty, avg_entry)
    stop_order = await broker.submit_stop(ticker, "SELL", qty, 93.0,
                                          client_order_id=f"cat-{ticker}")
    async with pool.connection() as c:
        cur = await c.execute(
            """INSERT INTO journal.orders (position_id, broker_order_id,
                 order_role, state, qty, stop_price)
               VALUES (%s,%s,'CATASTROPHE_STOP','ACCEPTED',%s,93.0)
               RETURNING order_id""", (pid, stop_order.broker_order_id, qty))
        cat_row = (await cur.fetchone())[0]
        await c.execute(
            """UPDATE journal.positions SET catastrophe_stop_order_id=%s
               WHERE position_id=%s""", (cat_row, pid))
    rows = await q(env, "SELECT position_id, ticker, horizon, qty_open, "
                        "avg_entry, r_unit, initial_stop, exit_policy, "
                        "opened_ts, last_price FROM journal.positions "
                        "WHERE position_id=%s", pid)
    r = rows[0]
    return {"position_id": r[0], "ticker": r[1], "horizon": r[2],
            "qty_open": r[3], "avg_entry": float(r[4]), "r_unit": float(r[5]),
            "initial_stop": float(r[6]), "exit_policy": r[7],
            "opened_ts": r[8], "last_price": r[9]}


def engine_for(broker):
    return PositionEngine(broker, now_fn=lambda: PIN,
                          unprotected_max_secs=0.03, poll_sleep=0.01,
                          session_age_fn=lambda o, n: 0)


def bar(o=100.0, h=100.5, l=99.5, c=100.0, **kw):
    return {"ts": int(PIN.timestamp()), "open": o, "high": h, "low": l,
            "close": c, **kw}


# ---------------------------------------------------------------------------

async def test_01_trail_ratchet_persists(env):
    broker = FakeBroker()
    pos = await seed_position(env, broker, "ACME",
                              policy_over={"magnitude_est": 0.15})
    eng = engine_for(broker)                     # target 110.5: no scale-out
    applied = await eng.step(pos, bar(h=106.5, l=105.0, c=106.0))
    assert any(a.startswith("SET_STOP:trail:101.5") for a in applied)
    rows = await q(env, """SELECT exit_policy->>'current_stop',
                                  exit_policy->>'stop_basis',
                                  exit_policy->>'hwm', last_price
                           FROM journal.positions WHERE ticker='ACME'""")
    stop, basis, hwm, mark = rows[0]
    assert (float(stop), basis, float(hwm)) == (101.5, "trail", 106.5)
    assert float(mark) == 106.0
    rows = await q(env, """SELECT event_type, r_progress FROM
                           journal.position_events pe
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='ACME' AND event_type='TRAIL_UPDATED'""")
    assert rows and float(rows[0][1]) == pytest.approx(1.5)


async def test_02_stop_exit_full_mechanics(env):
    """Trail stop hit -> cancel catastrophe -> exit fills -> exits row with
    TRAIL attribution -> position CLOSED -> orders trail complete."""
    broker = FakeBroker()
    rows = await q(env, "SELECT position_id FROM journal.positions WHERE ticker='ACME'")
    # reuse the ACME position; refresh broker state (new FakeBroker)
    broker.inject_position("ACME", 60, 100.0)
    stop_order = await broker.submit_stop("ACME", "SELL", 60, 93.0,
                                          client_order_id="cat-ACME-2")
    async with env["pool"].connection() as c:
        cur = await c.execute(
            """INSERT INTO journal.orders (position_id, broker_order_id,
                 order_role, state, qty, stop_price)
               VALUES (%s,%s,'CATASTROPHE_STOP','ACCEPTED',60,93.0)
               RETURNING order_id""", (rows[0][0], stop_order.broker_order_id))
        await c.execute("""UPDATE journal.positions
                           SET catastrophe_stop_order_id=%s
                           WHERE position_id=%s""",
                        ((await cur.fetchone())[0], rows[0][0]))
    prows = await q(env, "SELECT position_id, ticker, horizon, qty_open, "
                         "avg_entry, r_unit, initial_stop, exit_policy, "
                         "opened_ts, last_price FROM journal.positions "
                         "WHERE ticker='ACME'")
    r = prows[0]
    pos = {"position_id": r[0], "ticker": r[1], "horizon": r[2],
           "qty_open": r[3], "avg_entry": float(r[4]), "r_unit": float(r[5]),
           "initial_stop": float(r[6]), "exit_policy": r[7], "opened_ts": r[8],
           "last_price": r[9]}
    eng = engine_for(broker)
    applied = await eng.step(pos, bar(o=102, h=102, l=101.2, c=101.4,
                                      bid=101.35))
    assert "EXIT:TRAIL:FILLED" in applied

    rows = await q(env, """SELECT exit_layer, qty, price, r_multiple, is_partial
                           FROM journal.exits e
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='ACME'""")
    layer, qty, price, r_mult, is_partial = rows[0]
    assert (layer, qty, is_partial) == ("TRAIL", 60, False)
    assert float(price) == 101.35
    assert float(r_mult) == pytest.approx((101.35 - 100.0) / 4.0, abs=0.01)

    rows = await q(env, "SELECT status, qty_open, realized_pnl "
                        "FROM journal.positions WHERE ticker='ACME'")
    status, qty_open, pnl = rows[0]
    assert (status, qty_open) == ("CLOSED", 0)
    assert float(pnl) == pytest.approx(60 * 1.35, abs=0.1)
    # catastrophe was cancelled at the broker
    assert stop_order.broker_order_id in broker.cancels


async def test_03_exit_reinstates_when_unfilled(env):
    broker = FakeBroker()
    pos = await seed_position(env, broker, "BETA")
    broker.set_behavior("BETA", "rest")             # exit limit won't fill
    eng = engine_for(broker)
    applied = await eng.step(pos, bar(l=95.5, c=96.2, bid=96.1))
    assert "EXIT:STOP:REINSTATED" in applied
    rows = await q(env, """SELECT status, qty_open FROM journal.positions
                           WHERE ticker='BETA'""")
    assert tuple(rows[0]) == ("OPEN", 60)           # still open, protected
    # a NEW catastrophe stop rests at the broker
    stops = [o for o in broker.orders.values()
             if o.order_type == "stop" and not o.terminal]
    assert len(stops) == 1 and stops[0].qty == 60
    rows = await q(env, """SELECT detail FROM journal.position_events pe
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='BETA' AND event_type='GUARD_ACTION'""")
    assert any("EXIT_REINSTATED" in (d or "") for (d,) in rows)


async def test_04_catastrophe_filled_race(env):
    """Cancel fails because the broker stop already filled: record the
    CATASTROPHE exit, close, never submit a redundant exit."""
    broker = FakeBroker()
    pos = await seed_position(env, broker, "GAMA")
    cat_id = [o.broker_order_id for o in broker.orders.values()
              if o.order_type == "stop"][0]
    broker.fill_order(cat_id, price=92.8)           # tier-1 fired on its own
    eng = engine_for(broker)
    applied = await eng.step(pos, bar(l=92.5, c=93.0))
    assert "EXIT:STOP:CATASTROPHE_FILLED" in applied
    rows = await q(env, """SELECT exit_layer, price FROM journal.exits e
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='GAMA'""")
    assert rows[0][0] == "CATASTROPHE"
    assert float(rows[0][1]) == pytest.approx(92.8)
    rows = await q(env, "SELECT status FROM journal.positions WHERE ticker='GAMA'")
    assert rows[0][0] == "CLOSED"
    # exactly one SELL reached the broker: the original stop (no double sell)
    sells = [s for s in broker.submissions if s["side"] == "SELL"]
    assert len(sells) == 1


async def test_05_scale_out_resizes_catastrophe(env):
    broker = FakeBroker()
    pos = await seed_position(env, broker, "DLTA")
    eng = engine_for(broker)
    applied = await eng.step(pos, bar(h=104.0, l=103.0, c=103.9, bid=103.85))
    assert "SCALE_OUT:TARGET:FILLED" in applied
    rows = await q(env, """SELECT exit_layer, qty, is_partial FROM journal.exits e
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='DLTA'""")
    assert tuple(rows[0]) == ("TARGET", 30, True)
    rows = await q(env, "SELECT status, qty_open FROM journal.positions "
                        "WHERE ticker='DLTA'")
    assert tuple(rows[0]) == ("OPEN", 30)
    resting = [o for o in broker.orders.values()
               if o.order_type == "stop" and not o.terminal]
    assert len(resting) == 1 and resting[0].qty == 30   # re-sized protection


async def test_06_mip_invalidation_fires_end_to_end(env):
    broker = FakeBroker()
    pos = await seed_position(env, broker, "EPSN", prenews=99.0)
    eng = engine_for(broker)
    # close_below_prenews is a SESSION predicate: intraday closes below
    # prenews must NOT fire it (that's the whole point of session tf)
    a1 = await eng.step(pos, bar(l=98.6, c=98.7, bid=98.65))
    assert not any(x.startswith("EXIT") for x in a1)
    # session close above prenews: still no fire
    pos2 = [p for p in await open_positions() if p["ticker"] == "EPSN"][0]
    a2 = await eng.step(pos2, bar(l=98.9, c=99.4, bid=99.35, tf="session"))
    assert not any(x.startswith("EXIT") for x in a2)
    # session close BELOW prenews: fires, full exit
    pos3 = [p for p in await open_positions() if p["ticker"] == "EPSN"][0]
    a3 = await eng.step(pos3, bar(l=98.4, c=98.5, bid=98.45, tf="session"))
    assert "EXIT:INVALIDATION:FILLED" in a3
    rows = await q(env, """SELECT event_type FROM journal.position_events pe
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='EPSN' ORDER BY event_id""")
    kinds = [r[0] for r in rows]
    assert "INVALIDATION_ARMED" in kinds and "INVALIDATION_FIRED" in kinds
    rows = await q(env, """SELECT exit_layer FROM journal.exits e
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='EPSN'""")
    assert rows[0][0] == "INVALIDATION"


async def test_07_overnight_exit_and_forced_hold(env):
    # scope: close stragglers from earlier tests (their brokers are gone)
    async with env["pool"].connection() as c:
        await c.execute("""UPDATE journal.positions SET status='CLOSED',
                           closed_ts=now(), qty_open=0 WHERE status='OPEN'""")
    broker = FakeBroker()
    # stale flat position: opened 2 sessions ago, no progress -> D1 EXIT
    pos = await seed_position(env, broker, "ZETA",
                              opened_ts=PIN - timedelta(days=3))
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.positions SET last_price=100.2 "
                        "WHERE ticker='ZETA'")
    eng = PositionEngine(broker, now_fn=lambda: PIN,
                         unprotected_max_secs=0.03, poll_sleep=0.01,
                         session_age_fn=lambda o, n: 2)
    results = await eng.overnight_pass(ON_CFG, pass_label="15:45")
    zeta = [r for r in results if r[0] == "ZETA"][0]
    assert (zeta[1], zeta[2]) == ("EXIT", "stale_flat")
    rows = await q(env, """SELECT exit_layer FROM journal.exits e
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='ZETA'""")
    assert rows[0][0] == "OVERNIGHT"

    # winner holds: fresh position marked +0.5R
    pos2 = await seed_position(env, broker, "ETAA")
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.positions SET last_price=102.0 "
                        "WHERE ticker='ETAA'")
    results = await eng.overnight_pass(ON_CFG, pass_label="15:45")
    etaa = [r for r in results if r[0] == "ETAA"][0]
    assert (etaa[1], etaa[2]) == ("HOLD", "unrealized_R_threshold")

    # forced hold on the 15:55 pass when the exit can't fill
    pos3 = await seed_position(env, broker, "THTA",
                               opened_ts=PIN - timedelta(days=3))
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.positions SET last_price=100.1 "
                        "WHERE ticker='THTA'")
    broker.set_behavior("THTA", "rest")
    eng2 = PositionEngine(broker, now_fn=lambda: PIN,
                          unprotected_max_secs=0.03, poll_sleep=0.01,
                          session_age_fn=lambda o, n: 2)
    await eng2.overnight_pass(ON_CFG, pass_label="15:55")
    rows = await q(env, """SELECT new_value->>'decision'
                           FROM journal.position_events pe
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='THTA'
                             AND event_type='OVERNIGHT_HOLD_DECISION'
                           ORDER BY event_id""")
    decisions = [r[0] for r in rows]
    assert decisions == ["EXIT", "FORCED_HOLD"]
    rows = await q(env, "SELECT status FROM journal.positions WHERE ticker='THTA'")
    assert rows[0][0] == "OPEN"                    # protected, held


async def test_08_drawdown_breaker_trips(env):
    """ETAA reversed hard: mark deep red -> day PnL < -2% of effective."""
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.positions SET last_price=76.0 "
                        "WHERE ticker='ETAA'")     # (76-100)*60 = -1440
    tripped = await check_breaker(0.02)            # threshold -960 on 48k
    assert tripped
    assert await get_flag("drawdown_breaker") == "1"
    rows = await q(env, """SELECT detail FROM journal.audit
                           WHERE action='DRAWDOWN_BREAKER_SET'
                           ORDER BY audit_id DESC LIMIT 1""")
    assert "BREAKER_TRIP" in rows[0][0]
    await set_flag("drawdown_breaker", "0", "TEST", "reset")


async def test_09_deadman_ladder_and_ownership(env):
    from c1_ingestion.heartbeat import set_health
    cfg = {"components": {"ingestion": {"alert_min": 3, "block_entries_min": 10},
                          "marketdata": {"alert_min": 2, "block_entries_min": 2,
                                         "exit_engine_suspend_min": 10}}}
    now = PIN
    # fresh heartbeats -> nothing
    await set_health("ingestion", "OK", "hb")
    await set_health("marketdata", "OK", "hb")
    async with env["pool"].connection() as c:      # make both fresh at PIN
        await c.execute("UPDATE journal.health SET updated_ts=%s", (now,))
    actions = await deadman_check(cfg, now, in_session=True)
    assert actions == {"alerts": [], "block": False, "unblock": False,
                       "exit_suspend": False, "exit_resume": False}

    # ingestion stale 12 min -> ALERT + BLOCK_ENTRIES (deadman-owned)
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.health SET updated_ts=%s "
                        "WHERE component='ingestion'",
                        (now - timedelta(minutes=12),))
    actions = await deadman_check(cfg, now, in_session=True)
    assert actions["block"] and ("ingestion", 12.0) in actions["alerts"]
    assert await get_flag("block_entries") == "1"

    # recovery -> deadman clears ITS OWN block
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.health SET updated_ts=%s "
                        "WHERE component='ingestion'", (now,))
    actions = await deadman_check(cfg, now, in_session=True)
    assert actions["unblock"] and await get_flag("block_entries") == "0"

    # operator block is NEVER cleared by the monitor
    await set_flag("block_entries", "1", "OPERATOR", "manual")
    actions = await deadman_check(cfg, now, in_session=True)
    assert not actions["unblock"] and await get_flag("block_entries") == "1"
    await set_flag("block_entries", "0", "TEST", "reset")

    # marketdata stale 12 min -> exit engine suspend; recovery resumes
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.health SET updated_ts=%s "
                        "WHERE component='marketdata'",
                        (now - timedelta(minutes=12),))
    actions = await deadman_check(cfg, now, in_session=True)
    assert actions["exit_suspend"]
    assert await get_flag("exit_engine_suspended") == "1"
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.health SET updated_ts=%s "
                        "WHERE component='marketdata'", (now,))
    actions = await deadman_check(cfg, now, in_session=True)
    assert actions["exit_resume"]
    assert await get_flag("exit_engine_suspended") == "0"

    # off-hours: stale never escalates, only alerts
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.health SET updated_ts=%s",
                        (now - timedelta(minutes=30),))
    actions = await deadman_check(cfg, now, in_session=False)
    assert actions["alerts"] and not actions["block"] \
        and not actions["exit_suspend"]


async def test_10_halt_heuristic_freeze_resume(env):
    broker = FakeBroker()
    pos = await seed_position(env, broker, "IOTA")
    clock = {"now": PIN}
    eng = PositionEngine(broker, now_fn=lambda: clock["now"],
                         unprotected_max_secs=0.03, poll_sleep=0.01,
                         halt_stale_min=10.0, session_age_fn=lambda o, n: 0)
    await eng.step(pos, bar())                      # bar seen at PIN
    clock["now"] = PIN + timedelta(minutes=12)      # 12 min of silence
    assert await eng.check_halt(pos) is True
    rows = await q(env, """SELECT event_type FROM journal.position_events pe
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='IOTA'
                             AND event_type IN ('HALT_FROZEN','HALT_RESUMED')
                           ORDER BY event_id""")
    assert [r[0] for r in rows] == ["HALT_FROZEN"]
    # bar returns -> resume
    prows = [p for p in await open_positions() if p["ticker"] == "IOTA"]
    await eng.step(prows[0], bar())
    rows = await q(env, """SELECT event_type FROM journal.position_events pe
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='IOTA'
                             AND event_type IN ('HALT_FROZEN','HALT_RESUMED')
                           ORDER BY event_id""")
    assert [r[0] for r in rows] == ["HALT_FROZEN", "HALT_RESUMED"]

