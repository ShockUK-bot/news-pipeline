"""C6 Dashboard backend (spec v1.3, final-build variant: Postgres).

Single-operator, read-only console over the journal DB with three
token-gated write actions: kill switch, trading capital, and max trades/day.
The dashboard never commands the pipeline; only code enforces flags
(baseline C6).

Run: PYTHONPATH=src:dashboard uvicorn app:app --host 127.0.0.1 --port 8000
Env: PIPELINE_DSN, DASH_USER, DASH_PASS, DASH_KILL_TOKEN,
     DASH_PUSH_INTERVAL (default 1.5s). Binds 127.0.0.1; expose via
     `tailscale serve --bg 8000` only (spec section 7).

Deltas from the SQLite reference implementation (spec section 10):
- Postgres via the dash_* views shipped in journal-schema.sql since Phase 1.
- DASH_DB replaced by PIPELINE_DSN (one DSN convention everywhere).

v1.3 additions:
- POST /api/max-trades  — third operational control (max_trades_per_day).
- GET  /api/performance — portfolio vs SPY/QQQ total % change series, read
  from journal.portfolio_nav_daily (fed nightly by ops/snapshot_nav.py).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from datetime import date
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from common.marketdata import get_marketdata

app = FastAPI(title="C6 Dashboard", docs_url=None, redoc_url=None)
basic = HTTPBasic()

_WS_TOKENS: dict[str, float] = {}       # token -> expiry epoch (single-use)
_WS_TOKEN_TTL = 60.0

GRANULARITY = {"day": "day", "week": "week", "month": "month", "year": "year"}


def _dsn() -> str:
    return os.environ["PIPELINE_DSN"]


def _require_user(credentials: HTTPBasicCredentials = Depends(basic)) -> str:
    user_ok = secrets.compare_digest(credentials.username, os.environ.get("DASH_USER", ""))
    pass_ok = secrets.compare_digest(credentials.password, os.environ.get("DASH_PASS", ""))
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="bad credentials",
                            headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def _check_kill_token(token: str | None) -> None:
    expected = os.environ.get("DASH_KILL_TOKEN", "")
    if not (expected and token and secrets.compare_digest(token, expected)):
        raise HTTPException(status_code=403, detail="bad kill token")


async def _connect() -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(_dsn(), row_factory=dict_row)


async def _state() -> dict:
    async with await _connect() as conn:
        positions = [dict(r) for r in await (await conn.execute(
            "SELECT * FROM journal.dash_positions WHERE status='OPEN' ORDER BY opened_ts")).fetchall()]
        decisions = [dict(r) for r in await (await conn.execute(
            "SELECT * FROM journal.dash_decisions LIMIT 100")).fetchall()]
        vetoes = [dict(r) for r in await (await conn.execute(
            "SELECT * FROM journal.dash_decisions WHERE action='VETO' LIMIT 50")).fetchall()]
        health = [dict(r) for r in await (await conn.execute(
            "SELECT * FROM journal.dash_health ORDER BY component")).fetchall()]
        control = {r["key"]: r["value"] for r in await (await conn.execute(
            "SELECT key, value FROM journal.dash_control")).fetchall()}
        # Pipeline load — per-queue pending depth (the direct agent-overload
        # signal: signal.analyst backing up = A2 overloaded, etc.). Only scans
        # not-done rows (small, index-backed), so it's cheap on every push.
        load_queues = [dict(r) for r in await (await conn.execute("""
            SELECT queue_name,
                   count(*) FILTER (WHERE claimed_ts IS NULL AND available_ts <= now()) AS ready,
                   count(*) FILTER (WHERE claimed_ts IS NOT NULL)                        AS in_flight,
                   COALESCE(EXTRACT(EPOCH FROM now() - min(enqueued_ts)
                     FILTER (WHERE claimed_ts IS NULL AND available_ts <= now())), 0)::int AS oldest_age_s
            FROM queue.messages
            WHERE done_ts IS NULL
            GROUP BY queue_name""")).fetchall()]
        # Repeat-analysis watch: any ticker analyzed unusually often recently
        # is the fingerprint of a loop (e.g. the sympathy-lane cascade).
        hot_tickers = [dict(r) for r in await (await conn.execute("""
            SELECT ticker, count(*) AS analyses
            FROM journal.decisions
            WHERE stage='ANALYST' AND ticker IS NOT NULL
              AND ts > now() - interval '30 minutes'
            GROUP BY ticker HAVING count(*) > 3
            ORDER BY analyses DESC LIMIT 8""")).fetchall()]
        stats = dict((await (await conn.execute("""
            SELECT
              (SELECT value::numeric FROM journal.control WHERE key='trading_capital') AS trading_capital,
              (SELECT count(*) FROM journal.positions WHERE status='OPEN')             AS open_positions,
              (SELECT COALESCE(sum((COALESCE(last_price, avg_entry) - avg_entry) * qty_open), 0)
                 FROM journal.positions WHERE status='OPEN')                           AS unrealized_pnl,
              (SELECT COALESCE(sum(realized_pnl), 0) FROM journal.exits
                 WHERE ts::date = current_date)                                        AS realized_today,
              (SELECT count(*) FROM journal.fills WHERE ts::date = current_date)       AS fills_today,
              (SELECT count(*) FROM journal.decisions
                 WHERE action='VETO' AND ts::date = current_date)                      AS vetoes_today
        """)).fetchone()))
    return {"ts": time.time(), "positions": positions, "decisions": decisions,
            "vetoes": vetoes, "health": health, "control": control,
            "load": {"queues": load_queues, "hot_tickers": hot_tickers},
            "stats": {k: (float(v) if v is not None and k != "open_positions" else
                          int(v) if v is not None else 0) for k, v in stats.items()}}


def _json(payload: dict) -> JSONResponse:
    return JSONResponse(json.loads(json.dumps(payload, default=float)))


@app.get("/", response_class=HTMLResponse)
async def index(user: str = Depends(_require_user)) -> str:
    return (Path(__file__).parent / "index.html").read_text()


@app.get("/api/state")
async def api_state(user: str = Depends(_require_user)):
    return _json(await _state())


@app.get("/api/history")
async def api_history(granularity: str = "day", user: str = Depends(_require_user)):
    if granularity not in GRANULARITY:
        raise HTTPException(status_code=400, detail="granularity must be day|week|month|year")
    trunc = GRANULARITY[granularity]
    async with await _connect() as conn:
        periods = [dict(r) for r in await (await conn.execute(f"""
            SELECT to_char(date_trunc('{trunc}', closed_ts), 'YYYY-MM-DD') AS period,
                   count(*)                                   AS trades,
                   count(*) FILTER (WHERE realized_pnl > 0)   AS wins,
                   round(100.0 * count(*) FILTER (WHERE realized_pnl > 0) / count(*), 1) AS win_rate,
                   sum(realized_pnl)                          AS realized,
                   max(realized_pnl)                          AS best,
                   min(realized_pnl)                          AS worst
            FROM journal.positions WHERE status='CLOSED'
            GROUP BY 1 ORDER BY 1 DESC LIMIT 60""")).fetchall()]
        closed = [dict(r) for r in await (await conn.execute("""
            SELECT EXTRACT(EPOCH FROM p.closed_ts) AS closed_ts, p.ticker,
                   p.origin, p.qty_initial AS qty, p.avg_entry,
                   round(p.avg_entry + p.realized_pnl / NULLIF(p.qty_initial,0), 4) AS avg_exit,
                   (SELECT e.exit_layer FROM journal.exits e
                     WHERE e.position_id = p.position_id ORDER BY e.ts DESC LIMIT 1) AS exit_layer,
                   p.realized_pnl
            FROM journal.positions p WHERE p.status='CLOSED'
            ORDER BY p.closed_ts DESC LIMIT 100""")).fetchall()]
    return _json({"granularity": granularity, "periods": periods, "closed": closed})


@app.get("/api/performance")
async def api_performance(user: str = Depends(_require_user)):
    """Portfolio vs SPY/QQQ, total % change since first trade.

    Portfolio % = cumulative (realized + unrealized) P&L / a frozen baseline
    capital (the trading_capital value in effect on the day of the first
    trade, stored once in journal.control['performance_baseline_capital']).
    Freezing the denominator means later CAPITAL top-ups grow the numerator
    going forward without reshaping the historical curve.
    """
    async with await _connect() as conn:
        rows = [dict(r) for r in await (await conn.execute(
            "SELECT nav_date, total_pnl FROM journal.portfolio_nav_daily "
            "ORDER BY nav_date")).fetchall()]
        baseline = await (await conn.execute(
            "SELECT value FROM journal.control "
            "WHERE key='performance_baseline_capital'")).fetchone()
    if not rows or not baseline:
        return _json({"first_trade_date": None, "series": {}})
    base_cap = float(baseline["value"])
    first_date = rows[0]["nav_date"]
    portfolio = [{"date": r["nav_date"].isoformat(),
                  "pct": round(float(r["total_pnl"]) / base_cap * 100, 3)} for r in rows]
    md = get_marketdata()
    n_days = (date.today() - first_date).days + 5
    series = {"portfolio": portfolio}
    for sym in ("SPY", "QQQ"):
        bars = [b for b in await md.daily_bars(sym, n_days) if b["ts"].date() >= first_date]
        if not bars:
            continue
        base_close = bars[0]["close"]
        series[sym] = [{"date": b["ts"].date().isoformat(),
                         "pct": round((b["close"] / base_close - 1) * 100, 3)} for b in bars]
    return _json({"first_trade_date": first_date.isoformat(), "series": series})


@app.get("/api/ws-token")
async def api_ws_token(user: str = Depends(_require_user)):
    token = secrets.token_urlsafe(24)
    now = time.time()
    for t, exp in list(_WS_TOKENS.items()):    # opportunistic expiry sweep
        if exp < now:
            _WS_TOKENS.pop(t, None)
    _WS_TOKENS[token] = now + _WS_TOKEN_TTL
    return {"token": token, "ttl": _WS_TOKEN_TTL}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    if _WS_TOKENS.pop(token, 0) < time.time():   # single-use + unexpired
        await websocket.close(code=4403)
        return
    await websocket.accept()
    interval = float(os.environ.get("DASH_PUSH_INTERVAL", "1.5"))
    try:
        while True:
            await websocket.send_text(json.dumps(await _state(), default=float))
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        pass


async def _set_control(key: str, value: str, actor: str, action: str) -> dict:
    async with await _connect() as conn:
        old = (await (await conn.execute(
            "SELECT value FROM journal.control WHERE key=%s", (key,))).fetchone() or {}).get("value")
        await conn.execute(
            "INSERT INTO journal.control (key, value, updated_ts) VALUES (%s,%s,now()) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_ts=now()", (key, value))
        await conn.execute(
            "INSERT INTO journal.audit (actor, action, old_value, new_value) VALUES (%s,%s,%s,%s)",
            (actor, action, old, value))
        await conn.commit()
    return {"ok": True, "key": key, "old": old, "new": value}


@app.post("/api/kill")
async def api_kill(body: dict, user: str = Depends(_require_user)):
    _check_kill_token(body.get("token"))
    return await _set_control("kill_switch", "1", user, "KILL_SWITCH_ON")


@app.post("/api/resume")
async def api_resume(body: dict, user: str = Depends(_require_user)):
    _check_kill_token(body.get("token"))
    return await _set_control("kill_switch", "0", user, "KILL_SWITCH_OFF")


@app.post("/api/capital")
async def api_capital(body: dict, user: str = Depends(_require_user)):
    _check_kill_token(body.get("token"))
    raw = str(body.get("amount", "")).replace("$", "").replace(",", "").strip()
    try:
        amount = float(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="amount must be a number")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    return await _set_control("trading_capital", f"{amount:.0f}", user, "CAPITAL_SET")


@app.post("/api/max-trades")
async def api_max_trades(body: dict, user: str = Depends(_require_user)):
    _check_kill_token(body.get("token"))
    raw = str(body.get("amount", "")).strip()
    try:
        n = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="amount must be an integer")
    if n <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    return await _set_control("max_trades_per_day", str(n), user, "MAX_TRADES_SET")
