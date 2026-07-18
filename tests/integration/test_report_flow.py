"""Phase 6 integration against real PostgreSQL 16: A7 fact sheet + report
run + outbox + C5 mailer pass, on seeded journal data.

Covers: migration 003 applies idempotently; fact sheet numbers computed by
SQL from seeded decisions/positions/exits/guard rows; report run writes ONE
TRANSACTION (SYSTEM/A7/REPORT decision + outbox QUEUED row) with narrative
from a stub backend; report ships WITHOUT narrative when the model output is
invalid; same-day re-run is a no-op (idempotent under timer retries); mailer
sends PENDING -> SENT via a fake transport, failure -> retry accounting ->
FAILED at max attempts; unconfigured mailer leaves rows untouched and flags
DEGRADED health.
"""
import json
import os
from datetime import timedelta

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"
os.environ["BROKER"] = "fake"

from common.clock import utcnow
from common.db import get_pool, jb
from common.journal import register_config_version
from a1_triage.backends import StubBackend
from a7_report import service as a7
from a7_report.facts import build_facts
from c5_mailer.service import MAX_ATTEMPTS, process_outbox

pytestmark = pytest.mark.asyncio(loop_scope="session")

CFG = {"report": {"send_on_nonsession": True},   # tests may run on weekends
       "narrative": {"retries_on_invalid": 1}}

NARR = json.dumps({"summary": "Quiet day; one ACME partial realization.",
                   "notables": ["GATE vetoes dominated by LONG_ONLY"],
                   "data_quality": "ok"})


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        # migration 003 is idempotent — apply it here so the suite never
        # depends on deploy order
        with open(os.path.join(os.path.dirname(__file__), "..", "..",
                               "schema", "migrations", "003-outbox.sql")) as f:
            await c.execute(f.read())
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.regime_snapshots, journal.intents, journal.orders,
                     journal.fills, journal.positions, journal.position_events,
                     journal.exits, journal.guard_ledger, journal.outbox,
                     journal.audit,
                     news.cluster_members, news.clusters, news.news_items,
                     queue.messages
                     RESTART IDENTITY CASCADE""")
        await c.execute("DELETE FROM journal.control")
        await c.execute("""INSERT INTO journal.control (key, value, updated_ts)
                           VALUES ('kill_switch','0',now()),
                                  ('trading_capital','50000',now()),
                                  ('max_trades_per_day','5',now())""")
    await register_config_version("phase6 report integration test")
    return {"pool": pool}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args)
        return await cur.fetchall()


async def seed_day(env):
    """A miniature trading day: news item, decisions, a position with a
    partial TARGET exit, a guard verdict."""
    now = utcnow()
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO news.news_items
               (item_id, revision, source, source_tier, headline, summary,
                content_hash, symbols, published_ts, received_ts)
               VALUES ('n:acme-1',1,'alpaca_benzinga',2,
                       'Acme wins defense contract','s','h1',
                       ARRAY['ACME'], %s, %s) ON CONFLICT DO NOTHING""",
            (now - timedelta(hours=3), now - timedelta(hours=3)))
        # decisions: triage + thesis + a veto
        cur = await c.execute(
            """INSERT INTO journal.decisions (signal_id, stage, agent, action,
                 ticker, payload, reason, config_version)
               SELECT 'n:acme-1','ANALYST','A2','THESIS','ACME',
                      %s,'thesis', config_version
               FROM journal.config_versions LIMIT 1 RETURNING decision_id""",
            (jb({"thesis": {"direction": "up"}}),))
        thesis_id = (await cur.fetchone())[0]
        await c.execute(
            """INSERT INTO journal.decisions (signal_id, stage, agent, action,
                 veto_reason, config_version)
               SELECT 'n:other-1','GATE','C3','VETO','LONG_ONLY',
                      config_version FROM journal.config_versions LIMIT 1""")
        await c.execute(
            """INSERT INTO journal.intents (intent_id, decision_id, ticker,
                 side, qty, limit_price, config_version)
               SELECT 'it-acme-1',%s,'ACME','BUY',50,100.0, config_version
               FROM journal.config_versions LIMIT 1""", (thesis_id,))
        cur = await c.execute(
            """INSERT INTO journal.positions (ticker, horizon, profile,
                 status, opened_ts, entry_intent_id, thesis_decision_id,
                 item_id, qty_initial, qty_open, avg_entry, initial_stop,
                 r_unit, last_price, exit_policy, realized_pnl,
                 config_version)
               SELECT 'ACME','SHORT','short_term_v1','OPEN', %s,
                      'it-acme-1',%s,'n:acme-1',50,25,100.0,96.0,4.0,103.5,
                      %s, 100.0, config_version
               FROM journal.config_versions LIMIT 1 RETURNING position_id""",
            (utcnow() - timedelta(hours=2), thesis_id,
             jb({"current_stop": 100.0, "stop_basis": "BREAKEVEN",
                 "news_invalidations": ["contract_cancelled"]})))
        pid = (await cur.fetchone())[0]
        await c.execute(
            """INSERT INTO journal.exits (position_id, ts, exit_layer, qty,
                 price, realized_pnl, r_multiple, is_partial)
               VALUES (%s, %s,'TARGET',25,104.0,100.0,1.0,TRUE)""",
            (pid, now - timedelta(hours=1)))
        cur = await c.execute(
            """INSERT INTO journal.decisions (signal_id, stage, agent, action,
                 ticker, payload, config_version)
               SELECT 'n:acme-1','GUARD','A12','HOLD','ACME',%s,
                      config_version FROM journal.config_versions LIMIT 1
               RETURNING decision_id""",
            (jb({"position_id": pid}),))
        guard_dec = (await cur.fetchone())[0]
        await c.execute(
            """INSERT INTO journal.guard_ledger (decision_id, position_id,
                 item_id, ts, thesis_intact, recommended_action, urgency,
                 auto_executed, action_taken)
               VALUES (%s,%s,'n:acme-1', %s, TRUE,'HOLD','low',FALSE,
                       'JOURNALED')""", (guard_dec, pid, now))
    return pid


class FakeTransport:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.sent: list[tuple[str, str]] = []
        self.mail_to = ["op@example.com"]

    def configured(self):
        return True

    def send(self, subject, body):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("smtp boom")
        self.sent.append((subject, body))


# ---------------------------------------------------------------------------

async def test_01_facts_computed_from_journal(env):
    pid = await seed_day(env)
    facts = await build_facts()
    assert facts["trades"]["realized_pnl_today"] == 100.0
    assert facts["trades"]["exits"][0]["layer"] == "TARGET"
    assert facts["open_positions"][0]["position_id"] == pid
    assert facts["open_positions"][0]["unrealized_r"] == 0.88
    assert facts["open_positions"][0]["current_stop"] == 100.0
    assert facts["trades"]["opened"][0]["headline"] == \
        "Acme wins defense contract"
    assert {"stage": "GATE", "reason": "LONG_ONLY", "count": 1} in \
        facts["activity"]["vetoes"]
    assert facts["guard"]["verdicts"][0]["recommended_action"] == "HOLD"


async def test_02_report_run_writes_decision_and_outbox(env):
    outbox_id = await a7.run_report(CFG, backend_override=StubBackend([NARR]))
    assert outbox_id is not None
    rows = await q(env, """SELECT kind, subject, body, status, decision_id
                           FROM journal.outbox WHERE message_id=%s""", outbox_id)
    kind, subject, body, status, decision_id = rows[0]
    assert kind == "EOD_REPORT" and status == "QUEUED"
    assert "1 opened / 1 exits" in subject and "$100.00" in subject
    assert "Quiet day; one ACME partial realization." in body
    assert "OPENED ACME 50 @ $100.00" in body
    dec = await q(env, """SELECT action, payload FROM journal.decisions
                          WHERE decision_id=%s""", decision_id)
    assert dec[0][0] == "REPORT"
    assert dec[0][1]["narrative"]["summary"].startswith("Quiet day")
    assert dec[0][1]["facts"]["trades"]["realized_pnl_today"] == 100.0


async def test_03_same_day_rerun_is_noop(env):
    assert await a7.run_report(CFG, backend_override=StubBackend([NARR])) is None
    assert await q(env, "SELECT count(*) FROM journal.outbox") == [(1,)]


async def test_04_invalid_narrative_ships_report_anyway(env):
    # wipe today's report so a fresh one can generate
    async with env["pool"].connection() as c:
        await c.execute("""DELETE FROM journal.outbox;
                           DELETE FROM journal.decisions
                           WHERE stage='SYSTEM' AND agent='A7'""")
    outbox_id = await a7.run_report(
        CFG, backend_override=StubBackend(["garbage", '{"still": "bad"}']))
    assert outbox_id is not None
    rows = await q(env, "SELECT body FROM journal.outbox WHERE message_id=%s",
                   outbox_id)
    assert "narrative unavailable" in rows[0][0]
    dec = await q(env, """SELECT payload FROM journal.decisions
                          WHERE stage='SYSTEM' AND agent='A7'
                            AND action='REPORT'""")
    assert dec[0][0]["narrative"] is None


async def test_05_mailer_sends_pending(env):
    t = FakeTransport()
    stats = await process_outbox(transport=t)
    assert stats["sent"] == 1 and len(t.sent) == 1
    assert "EOD Report" in t.sent[0][0]
    rows = await q(env, "SELECT status, sent_ts FROM journal.outbox")
    assert rows[0][0] == "SENT" and rows[0][1] is not None


async def test_06_mailer_retries_then_errors_out(env):
    async with env["pool"].connection() as c:
        await c.execute("""INSERT INTO journal.outbox (kind, subject, body)
                           VALUES ('EOD_REPORT','doomed','body')""")
    t = FakeTransport(fail_times=10 ** 6)
    for i in range(MAX_ATTEMPTS):
        stats = await process_outbox(transport=t)
        assert stats["failed"] == 1
    rows = await q(env, """SELECT status, attempts, last_error
                           FROM journal.outbox WHERE subject='doomed'""")
    status, attempts, err = rows[0]
    assert status == "FAILED" and attempts == MAX_ATTEMPTS
    assert "smtp boom" in err
    # errored-out rows are never retried again
    stats = await process_outbox(transport=FakeTransport())
    assert stats["sent"] == 0 and stats["failed"] == 0


async def test_07_unconfigured_mailer_leaves_outbox_alone(env):
    async with env["pool"].connection() as c:
        await c.execute("""INSERT INTO journal.outbox (kind, subject, body)
                           VALUES ('EOD_REPORT','waiting','body')""")

    class Unconfigured:
        def configured(self):
            return False

    stats = await process_outbox(transport=Unconfigured())
    assert stats["skipped"] == 1
    rows = await q(env, """SELECT status, attempts FROM journal.outbox
                           WHERE subject='waiting'""")
    assert rows[0] == ("QUEUED", 0)
    health = await q(env, """SELECT status FROM journal.health
                             WHERE component='mailer'""")
    assert health == [("DEGRADED",)]
