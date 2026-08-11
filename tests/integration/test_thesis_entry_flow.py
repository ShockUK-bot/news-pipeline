"""v0.12.26 integration against real PostgreSQL 16: the C11 thesis-entry
lane end to end (planning side — C4 execution is exercised by its own
existing suite; this file proves C11 hands C4 exactly the message shape it
already consumes).

Covers: entry planning over seeded ACTIVE theses (intent row + queue-
delayed exec.intent message + THESIS_ENTRY decision + digest outbox);
don't-chase skip (the RIOT case) journaled with numbers; caps (max per
day); same-date rerun no-op; dead-thesis management (stop tightened
in-place, STOP_TIGHTENED position event, tighten-only respected);
held-ticker exclusion."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"
os.environ["BROKER"] = "fake"

from common.db import get_pool
from common.journal import register_config_version, write_decision
from common.marketdata import FakeData
from c11_thesis.service import run_thesis_entries

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Tue/Wed evenings ET (22:15 ET = 03:15 UTC next day)
D1 = datetime(2026, 8, 12, 3, 15, tzinfo=timezone.utc)   # Tue 22:15 ET
D2 = datetime(2026, 8, 13, 3, 15, tzinfo=timezone.utc)   # Wed 22:15 ET

CFG = {
    "entry": {"min_confidence": 0.5, "min_evidence": 0, "max_new_per_day": 2,
              "max_open_positions": 4, "max_per_thesis": 2,
              "risk_pct": 0.0025, "min_viable_risk": 100,
              "chase_buffer_pct": 0.02, "entry_blackout_min": 15,
              "stagger_min": 5, "expected_move": 0.20},
    "liquidity": {"min_price": 3.0, "min_adv_dollars": 20_000_000},
    "extended": {"max_above_sma20_pct": 0.10, "max_5session_gain_pct": 0.15},
    "management": {"exit_dead_theses": True, "trim_before_earnings": False},
    "digest": {"email": True},
}
PROFILES = {"profiles": {"thesis_v1": {
    "initial_stop": {"method": "atr", "k": 3.0},
    "catastrophe": {"method": "atr", "k": 4.5},
    "breakeven_at_R": 1.0,
    "trail": {"activate_at_R": 2.0, "method": "atr_weekly", "k": 4.0},
    "time_stop": None,
    "realization": {"target_fraction": 0.7, "action": "review_flag"},
    "earnings_blackout_exit": False, "overnight_hold": "default_hold"}}}


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.theses, journal.thesis_evidence, journal.outbox,
                     journal.intents, journal.positions,
                     journal.position_events, journal.orders, journal.fills,
                     journal.health, queue.messages
                     RESTART IDENTITY CASCADE""")
        await c.execute("""INSERT INTO journal.control (key, value)
                           VALUES ('broker_equity','100000'),
                                  ('trading_capital','100000')
                           ON CONFLICT (key) DO UPDATE
                           SET value=EXCLUDED.value""")
    await register_config_version("v0.12.26 thesis entry integration test")
    return {"pool": pool}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args or None)
        return await cur.fetchall()


async def seed_thesis(env, thesis_id, tickers, confidence=0.6,
                      status="ACTIVE"):
    dec = await write_decision(
        signal_id=f"seed-{thesis_id}", stage="THEMATIC", agent="A5",
        action="NEW_THESIS", payload={"thesis_id": thesis_id},
        reason=f"seed {thesis_id}")
    bens = [{"ticker": t, "relation": "beneficiary", "rationale": "r"}
            for t in tickers]
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO journal.theses
                 (thesis_id, title, driver, direction, horizon, confidence,
                  beneficiaries, invalidation, status, created_decision_id,
                  config_version)
               VALUES (%s,%s,'driver','up','LONG',%s,%s,%s,%s,%s,
                       (SELECT config_version FROM journal.config_versions
                        ORDER BY applied_ts DESC LIMIT 1))""",
            (thesis_id, f"T {thesis_id}", confidence, json.dumps(bens),
             json.dumps(["inv"]), status, dec))
    return dec


def market(quiet=("VST", "CEG"), popped=()):
    md = FakeData()
    for t in quiet:
        md.set_daily(t, FakeData.flat_daily(30, close=100.0,
                                            volume=2_000_000))
    for t in popped:
        bars = FakeData.flat_daily(30, close=100.0, volume=2_000_000)
        bars[-1]["close"] = 126.0
        bars[-1]["high"] = 127.0
        md.set_daily(t, bars)
    return md


async def test_01_plan_creates_delayed_intents_and_digest(env):
    await seed_thesis(env, "th-2026-001", ["VST", "CEG"], 0.6)
    stats = await run_thesis_entries(CFG, PROFILES, now=D1, md=market())
    assert stats["planned"] == 2

    intents = await q(env, """SELECT intent_id, side, qty, limit_price, status
                              FROM journal.intents ORDER BY intent_id""")
    assert len(intents) == 2
    assert all(i[1] == "BUY" and i[4] == "PENDING" for i in intents)
    # $100k * 0.25% = $250 over a 3*ATR stop; flat_daily ATR ~ 2% of close
    assert all(i[2] >= 1 for i in intents)
    assert all(abs(float(i[3]) - 102.0) < 0.5 for i in intents)  # close*1.02

    msgs = await q(env, """SELECT dedup_key, available_ts, payload
                           FROM queue.messages WHERE queue_name='exec.intent'
                           ORDER BY available_ts""")
    assert len(msgs) == 2
    # queue-delayed to the NEXT session open + blackout, staggered
    assert all(m[1] > D1 for m in msgs)
    assert msgs[1][1] > msgs[0][1]
    body = msgs[0][2]["body"]
    # the exact keys C4's handle_intent/_open_position consume:
    for key in ("intent_id", "ticker", "qty", "limit_price", "exit_policy",
                "horizon", "thesis_decision_id", "origin"):
        assert key in body, key
    assert body["origin"] == "thesis" and body["horizon"] == "LONG"
    assert body["exit_policy"]["atr_value"] > 0

    assert (await q(env, """SELECT count(*) FROM journal.decisions
                            WHERE stage='RISK' AND agent='C11'
                              AND action='THESIS_ENTRY'"""))[0][0] == 2
    outbox = await q(env, "SELECT subject FROM journal.outbox")
    assert len(outbox) == 1 and "2 planned" in outbox[0][0]


async def test_02_rerun_same_date_is_noop(env):
    assert await run_thesis_entries(CFG, PROFILES, now=D1, md=market()) is None
    assert (await q(env, "SELECT count(*) FROM journal.intents"))[0][0] == 2


async def test_03_popped_beneficiary_is_skipped_with_numbers(env):
    """The RIOT case: +26% overnight -> EXTENDED skip, journaled."""
    await seed_thesis(env, "th-2026-002", ["RIOT"], 0.55)
    stats = await run_thesis_entries(
        CFG, PROFILES, now=D2, md=market(quiet=(), popped=("RIOT",)))
    assert stats["planned"] == 0 and stats["skips"] == 1
    rows = await q(env, """SELECT payload FROM journal.decisions
                           WHERE action='THESIS_SKIP'
                             AND ticker='RIOT' ORDER BY ts DESC LIMIT 1""")
    p = rows[0][0]
    assert p["skip"] == "EXTENDED"
    assert p["above_sma20_pct"] > 0.10
    # no intent, no queue message for the popped name
    assert (await q(env, """SELECT count(*) FROM journal.intents
                            WHERE ticker='RIOT'"""))[0][0] == 0


async def test_04_held_ticker_excluded_and_dead_thesis_exit_armed(env):
    """Seed an open thesis position whose thesis then dies: the nightly
    pass must tighten its stop in place (tighten-only) and journal it —
    and the same ticker must never be re-entered while held."""
    pool = env["pool"]
    dec = await seed_thesis(env, "th-2026-003", ["NRG"], 0.6,
                            status="INVALIDATED")
    async with pool.connection() as c:
        await c.execute(
            """INSERT INTO journal.intents
                 (intent_id, decision_id, ticker, side, qty, limit_price,
                  exit_policy, horizon, status, config_version)
               VALUES ('seed-nrg-1',%s,'NRG','BUY',10,100.0,%s,'LONG',
                       'FILLED',
                       (SELECT config_version FROM journal.config_versions
                        ORDER BY applied_ts DESC LIMIT 1))""",
            (dec, json.dumps({"initial_stop": {"price": 94.0}})))
        await c.execute(
            """INSERT INTO journal.positions
                 (ticker, horizon, profile, status, opened_ts,
                  entry_intent_id, thesis_decision_id, qty_initial, qty_open,
                  avg_entry, initial_stop, r_unit, exit_policy, last_price,
                  origin, config_version)
               VALUES ('NRG','LONG','thesis_v1','OPEN',now(),'seed-nrg-1',
                       %s,10,10,100.0,94.0,6.0,%s,105.0,'thesis',
                       (SELECT config_version FROM journal.config_versions
                        ORDER BY applied_ts DESC LIMIT 1))""",
            (dec, json.dumps({"profile": "thesis_v1",
                              "initial_stop": {"method": "atr", "k": 3.0,
                                               "price": 94.0}})))
    # a fresh ACTIVE thesis also names NRG — must be excluded as held
    await seed_thesis(env, "th-2026-004", ["NRG"], 0.6)

    d3 = D2 + timedelta(days=1)
    # RIOT (th-2026-002) stays popped in this market: still EXTENDED, so
    # the only planning question is whether held NRG is excluded.
    stats = await run_thesis_entries(
        CFG, PROFILES, now=d3, md=market(quiet=("NRG",), popped=("RIOT",)))
    assert stats["dead_exits_armed"] == 1
    assert stats["planned"] == 0                      # NRG held -> excluded

    rows = await q(env, """SELECT exit_policy FROM journal.positions
                           WHERE ticker='NRG' AND status='OPEN'""")
    policy = rows[0][0]
    assert policy["current_stop"] == pytest.approx(104.475, abs=0.01)  # 105*.995
    ev = await q(env, """SELECT event_type, new_value
                         FROM journal.position_events
                         ORDER BY event_id DESC LIMIT 1""")
    assert ev[0][0] == "STOP_TIGHTENED"
    assert "thesis INVALIDATED" in ev[0][1]["reason"]

    # rerun next night: stop already at target -> tighten-only no-op
    d4 = d3 + timedelta(days=1)
    stats2 = await run_thesis_entries(
        CFG, PROFILES, now=d4, md=market(quiet=("NRG",), popped=("RIOT",)))
    assert stats2["dead_exits_armed"] == 0
