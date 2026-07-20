"""C6 dashboard integration tests (spec v1.2) — live Postgres, ASGI transport."""
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dashboard"))

os.environ.setdefault("DASH_USER", "op")
os.environ.setdefault("DASH_PASS", "op_pw")
os.environ.setdefault("DASH_KILL_TOKEN", "kill_tok")

from app import app, _WS_TOKENS  # noqa: E402

AUTH = ("op", "op_pw")


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://dash") as c:
        yield c


@pytest.fixture
def db():
    import psycopg
    with psycopg.connect(os.environ["PIPELINE_DSN"], autocommit=True) as conn:
        yield conn


async def test_auth_required_everywhere(client):
    for path in ("/", "/api/state", "/api/history", "/api/ws-token"):
        assert (await client.get(path)).status_code == 401
    assert (await client.get("/api/state", auth=("op", "wrong"))).status_code == 401


async def test_index_served(client):
    r = await client.get("/", auth=AUTH)
    assert r.status_code == 200 and "C6" in r.text and "KILL SWITCH" in r.text


async def test_state_shape(client):
    r = await client.get("/api/state", auth=AUTH)
    assert r.status_code == 200
    s = r.json()
    for key in ("ts", "positions", "decisions", "vetoes", "health", "control", "stats"):
        assert key in s
    for key in ("trading_capital", "open_positions", "unrealized_pnl",
                "realized_today", "fills_today", "vetoes_today"):
        assert key in s["stats"]


async def test_state_load_panel(client, db):
    """Pipeline-load panel: per-queue depth + repeat-analysis watch."""
    # a ready message and an in-flight (claimed) one on distinct queues
    db.execute("INSERT INTO queue.messages (queue_name, dedup_key, payload) "
               "VALUES ('signal.analyst','load-test:1','{}') "
               "ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO queue.messages (queue_name, dedup_key, payload, claimed_by, claimed_ts) "
               "VALUES ('signal.gate','load-test:2','{}','x',now()) "
               "ON CONFLICT DO NOTHING")
    r = await client.get("/api/state", auth=AUTH)
    s = r.json()
    assert "load" in s and "queues" in s["load"] and "hot_tickers" in s["load"]
    byq = {q["queue_name"]: q for q in s["load"]["queues"]}
    for f in ("ready", "in_flight", "oldest_age_s"):
        assert f in byq["signal.analyst"]
    assert byq["signal.analyst"]["ready"] >= 1
    assert byq["signal.gate"]["in_flight"] >= 1
    # cleanup
    db.execute("DELETE FROM queue.messages WHERE dedup_key LIKE 'load-test:%'")


async def test_history_granularities_and_validation(client):
    for g in ("day", "week", "month", "year"):
        r = await client.get(f"/api/history?granularity={g}", auth=AUTH)
        assert r.status_code == 200 and r.json()["granularity"] == g
    assert (await client.get("/api/history?granularity=hour", auth=AUTH)).status_code == 400


async def test_kill_flow_writes_control_and_audit(client, db):
    before = db.execute("SELECT count(*) FROM journal.audit").fetchone()[0]
    assert (await client.post("/api/kill", json={"token": "wrong"}, auth=AUTH)).status_code == 403
    r = await client.post("/api/kill", json={"token": "kill_tok"}, auth=AUTH)
    assert r.status_code == 200 and r.json()["new"] == "1"
    assert db.execute("SELECT value FROM journal.control WHERE key='kill_switch'").fetchone()[0] == "1"
    r = await client.post("/api/resume", json={"token": "kill_tok"}, auth=AUTH)
    assert r.status_code == 200 and r.json()["new"] == "0"
    assert db.execute("SELECT value FROM journal.control WHERE key='kill_switch'").fetchone()[0] == "0"
    rows = db.execute(
        "SELECT actor, action FROM journal.audit ORDER BY audit_id DESC LIMIT 2").fetchall()
    assert {a for _, a in rows} == {"KILL_SWITCH_ON", "KILL_SWITCH_OFF"}
    assert all(actor == "op" for actor, _ in rows)
    assert db.execute("SELECT count(*) FROM journal.audit").fetchone()[0] == before + 2


async def test_capital_validation_and_set(client, db):
    old = db.execute("SELECT value FROM journal.control WHERE key='trading_capital'").fetchone()[0]
    assert (await client.post("/api/capital", json={"token": "kill_tok", "amount": "nope"},
                              auth=AUTH)).status_code == 400
    assert (await client.post("/api/capital", json={"token": "kill_tok", "amount": -5},
                              auth=AUTH)).status_code == 400
    assert (await client.post("/api/capital", json={"token": "wrong", "amount": 60000},
                              auth=AUTH)).status_code == 403
    r = await client.post("/api/capital", json={"token": "kill_tok", "amount": "$60,000"}, auth=AUTH)
    assert r.status_code == 200 and r.json()["new"] == "60000"
    assert db.execute(
        "SELECT value FROM journal.control WHERE key='trading_capital'").fetchone()[0] == "60000"
    a = db.execute("SELECT action, old_value, new_value FROM journal.audit "
                   "ORDER BY audit_id DESC LIMIT 1").fetchone()
    assert a == ("CAPITAL_SET", old, "60000")
    # restore
    await client.post("/api/capital", json={"token": "kill_tok", "amount": old}, auth=AUTH)


async def test_ws_token_minted_single_use(client):
    r = await client.get("/api/ws-token", auth=AUTH)
    assert r.status_code == 200
    token = r.json()["token"]
    assert _WS_TOKENS.get(token, 0) > time.time()
    # single-use semantics: pop() consumes — simulate the ws handshake's check
    assert _WS_TOKENS.pop(token, 0) > time.time()
    assert token not in _WS_TOKENS


async def test_stats_consistent_with_tables(client, db):
    open_n = db.execute("SELECT count(*) FROM journal.positions WHERE status='OPEN'").fetchone()[0]
    r = await client.get("/api/state", auth=AUTH)
    assert r.json()["stats"]["open_positions"] == open_n
