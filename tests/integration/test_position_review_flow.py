"""Phase 8 integration against real PostgreSQL 16: A6 position review over
seeded positions (thesis decision -> intent -> position, real FKs).

Covers: nightly deep review (per-position verdicts journaled + mirrored into
position_events; code-side STALE_FLAG fires before/without the model; REVIEW
anchor; trim/exit/stale recommendations render one ALERT outbox row);
same-date rerun no-op; EOD overnight-hold check (SHORT lane only, verdicts
journaled, model-omitted positions default to hold; EOD_SHEET anchor);
no-model degradation (code rule still reports); empty-book skip."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"
os.environ["BROKER"] = "fake"

from common.clock import utcnow
from common.db import get_pool, jb
from common.journal import register_config_version
from a1_triage.backends import StubBackend
from a6_position_review.service import run_eod, run_nightly

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Weekday ET pins (sessions), one run-date per test.
N1 = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)     # Mon 20:00 ET
E2 = datetime(2026, 7, 21, 19, 45, tzinfo=timezone.utc)   # Tue 15:45 ET
E3 = datetime(2026, 7, 22, 19, 45, tzinfo=timezone.utc)   # Wed 15:45 ET
N4 = datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)     # Thu 20:00 ET
N5 = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)     # Fri 20:00 ET

CFG = {"report": {"send_on_nonsession": False},
       "review": {"stale_weeks": 4, "max_positions": 20},
       "alert": {"email": True},
       "eod": {"retries_on_invalid": 1},
       "narrative": {"retries_on_invalid": 1},
       "heavy": {}, "analyst_fallback": {"enabled": False}}


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.intents, journal.orders, journal.fills,
                     journal.positions, journal.position_events,
                     journal.exits, journal.guard_ledger, journal.outbox,
                     journal.theses, journal.thesis_evidence
                     RESTART IDENTITY CASCADE""")
    await register_config_version("phase8 position-review integration test")
    return {"pool": pool}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args or None)
        return await cur.fetchall()


async def seed_position(env, ticker, horizon="SHORT", opened_days_ago=1.0,
                        last_price=102.0):
    """Thesis decision -> intent -> position, minimally but with real FKs."""
    opened = utcnow() - timedelta(days=opened_days_ago)
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
                            "magnitude_est": 0.06,
                            "expected_move_window": "2_sessions",
                            "horizon": horizon, "confidence": 0.7,
                            "invalidation": {
                                "machine_checkable": ["close_below_prenews"],
                                "news_checkable": ["counterparty_denial"]},
                            "reason": "repricing underway"}})))
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
                 (ticker, horizon, profile, status, opened_ts,
                  entry_intent_id, thesis_decision_id, item_id, qty_initial,
                  qty_open, avg_entry, initial_stop, r_unit, exit_policy,
                  last_price, last_price_ts, config_version)
               SELECT %s,%s,%s,'OPEN',%s,%s,%s,'seed-item',50,50,
                      100.0,96.0,4.0,%s,%s,now(), config_version
               FROM journal.config_versions LIMIT 1
               RETURNING position_id""",
            (ticker, horizon,
             "short_term_v1" if horizon == "SHORT" else "long_term_v1",
             opened, intent_id, thesis_id,
             jb({"profile": "short_term_v1", "current_stop": 96.0}),
             last_price))
        return (await cur.fetchone())[0]


async def seed_escalation(env, ticker):
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO journal.decisions
                 (signal_id, stage, agent, action, ticker, config_version)
               SELECT %s,'TRIAGE','A1','ESCALATE',%s, config_version
               FROM journal.config_versions LIMIT 1""",
            (f"news:{ticker}", ticker))


def review_reply(verdict, staleness="fresh", rationale="thesis on track"):
    return json.dumps({"verdict": verdict, "thesis_intact": verdict == "hold",
                       "staleness": staleness, "guard_review": "none",
                       "confidence": 0.7, "rationale": rationale})


async def test_01_nightly_review_verdicts_stale_flag_and_alert(env):
    p_short = await seed_position(env, "NVDA", "SHORT", opened_days_ago=1.0)
    p_long = await seed_position(env, "ACME", "LONG", opened_days_ago=45.0)
    await seed_escalation(env, "NVDA")            # NVDA has fresh news flow

    backend = StubBackend([
        review_reply("hold"),
        review_reply("exit", "stale", "no confirming evidence in 6 weeks"),
    ])
    outbox_id = await run_nightly(CFG, backend_override=backend, now=N1)
    assert outbox_id is not None

    acts = dict(await q(env, """SELECT action, ticker FROM journal.decisions
                                WHERE stage='POSITION_REVIEW'
                                  AND action IN ('HOLD','EXIT_RECO',
                                                 'STALE_FLAG')"""))
    assert acts == {"HOLD": "NVDA", "EXIT_RECO": "ACME",
                    "STALE_FLAG": "ACME"}
    anchors = await q(env, """SELECT payload->>'reviewed',
                                     payload->>'stale_flagged'
                              FROM journal.decisions
                              WHERE stage='POSITION_REVIEW'
                                AND action='REVIEW'""")
    assert anchors == [("2", "1")]
    ev = dict(await q(env, """SELECT event_type, count(*)
                              FROM journal.position_events
                              GROUP BY event_type"""))
    assert ev["POSITION_REVIEW"] == 2 and ev["STALE_FLAG"] == 1
    ob = await q(env, """SELECT subject, body FROM journal.outbox
                         WHERE fact_sheet->>'run_date' = '2026-07-20'""")
    assert len(ob) == 1 and "ACME" in ob[0][0] and "EXIT_RECO" in ob[0][1]

    # same-date rerun -> anchor no-op
    assert await run_nightly(CFG, backend_override=backend, now=N1) is None
    return p_short, p_long


async def test_02_eod_check_short_lane_only(env):
    sheet = json.dumps({"verdicts": [
        {"position_id": 1, "verdict": "hold_overnight", "confidence": 0.8,
         "rationale": "half the estimated move left; window open"}]})
    reviewed = await run_eod(CFG, backend_override=StubBackend([sheet]),
                             now=E2)
    assert reviewed == 1                          # the LONG book is not eod's

    rows = await q(env, """SELECT action, ticker FROM journal.decisions
                           WHERE stage='POSITION_REVIEW'
                             AND payload->>'run_date' = '2026-07-21'
                             AND payload->>'lane' = 'eod'
                             AND action <> 'EOD_SHEET'""")
    assert rows == [("HOLD_OVERNIGHT", "NVDA")]
    anchors = await q(env, """SELECT payload->>'reviewed'
                              FROM journal.decisions
                              WHERE action='EOD_SHEET'""")
    assert anchors == [("1",)]
    ev = await q(env, """SELECT count(*) FROM journal.position_events
                         WHERE event_type='OVERNIGHT_HOLD_DECISION'""")
    assert ev[0][0] == 1

    assert await run_eod(CFG, backend_override=StubBackend([sheet]),
                         now=E2) == 0             # rerun no-op


async def test_03_eod_model_omission_defaults_to_hold(env):
    empty = json.dumps({"verdicts": []})
    reviewed = await run_eod(CFG, backend_override=StubBackend([empty]),
                             now=E3)
    assert reviewed == 1
    rows = await q(env, """SELECT payload->>'rationale'
                           FROM journal.decisions
                           WHERE action='HOLD_OVERNIGHT'
                             AND payload->>'run_date' = '2026-07-22'""")
    assert rows and "model omitted" in rows[0][0]


async def test_04_nightly_no_model_code_rule_still_reports(env):
    outbox_id = await run_nightly(CFG, backend_override=None, now=N4)
    # ACME is still stale -> STALE_FLAG + REVIEW anchor + alert, no verdicts
    acts = [r[0] for r in await q(
        env, """SELECT action FROM journal.decisions
                WHERE stage='POSITION_REVIEW'
                  AND payload->>'run_date' = '2026-07-23'""")]
    assert "STALE_FLAG" in acts and "REVIEW" in acts
    assert "HOLD" not in acts and "EXIT_RECO" not in acts
    assert outbox_id is not None


async def test_05_empty_book_skips(env):
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.positions SET status='CLOSED', "
                        "closed_ts=now()")
    assert await run_nightly(CFG, backend_override=None, now=N5) is None
    acts = [r[0] for r in await q(
        env, """SELECT action FROM journal.decisions
                WHERE stage='POSITION_REVIEW'
                  AND payload->>'run_date' = '2026-07-24'""")]
    assert acts == ["SKIPPED_NO_POSITIONS"]
