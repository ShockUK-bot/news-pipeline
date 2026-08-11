"""C11 Thesis-Entry lane (v0.12.26) — the long lane's originator.

Oneshot, fired by thesis-entry.timer nightly at 22:15 ET (45 min after
A5's thematic pass has updated the store). Pure CODE: no model call
anywhere in this service — A5's store proposes (theses, confidences,
beneficiaries), these deterministic rules dispose.

Why this lane exists (operator decision 2026-08-11, the RIOT night): the
news lane's C3 gate correctly refuses to chase a big gap on news — so a
beneficiary that pops +26% overnight on a thesis-confirming catalyst can
only be caught by ALREADY OWNING IT. This lane buys quiet beneficiaries of
ACTIVE up-theses, small and wide-stopped, and holds while the thesis lives.

Nightly flow:
  1. MANAGEMENT pass over open origin='thesis' positions:
     a. DEAD THESIS (status no longer ACTIVE — invalidated / realized /
        staleness-expired): tighten the position's live stop to just under
        the last mark. The existing C4 L1 stop machinery exits it next
        session — no new execution code touches the money path. Tighten-
        only: the stop never moves DOWN.
     b. EARNINGS TRIM (operator policy): reports within <=1 session AND
        unrealized >= trim_min_gain_R -> journal THESIS_TRIM_RECO + email.
        v1 recommends; partial-exit automation is a later release.
  2. ENTRY planning: ACTIVE * direction='up' * confidence>=min theses,
     beneficiaries filtered by liquidity (price, 20d dollar volume) and the
     DON'T-CHASE gate (extended vs 20-session mean, or 5-session run-up),
     capped (per day / per thesis / total open), sized at risk_pct of
     effective capital over a k*ATR(14) stop (thesis_v1 profile), and
     delivered as exec.intent messages with available_ts = next session
     open + blackout (+stagger). C4 runs its normal preflight / limit /
     fill / stop path — this service never talks to the broker.
  3. One RISK/C11/THESIS_PLAN anchor per ET date (rerun = no-op) + a plan
     digest email via journal.outbox.

Idempotency: the plan anchor gates the whole run per ET date; intent ids
are deterministic (thesis-<id>-<ticker>-<session date>) so even a forced
re-run cannot double-enter (intents PK + ON CONFLICT DO NOTHING + C4's
already-processed check).

Failure posture: every per-candidate step is isolated (one bad symbol
never kills the plan); marketdata errors -> that candidate is skipped with
a journaled DATA_UNAVAILABLE. The service holds NO state of its own —
everything lives in journal.* tables.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common.config import config_path, load_yaml
from common.clock import utcnow
from common.contracts import envelope
from common.db import get_pool, jb
from common.journal import (active_config_version, register_config_version,
                            write_decision)
from common.log import get_logger, kv
from common.marketdata import adv20, atr14, get_marketdata, sma
from a4_premarket.service import next_entry_ts
from c4_exec.flags import get_flag

log = get_logger("c11.thesis")

ET = ZoneInfo("America/New_York")
EXEC_QUEUE = "exec.intent"
CONTRACT_INTENT = "exec.intent/1"


# --------------------------------------------------------------------------
# pure rules (unit-tested directly)
# --------------------------------------------------------------------------

def extended_verdict(daily: list[dict], cfg: dict) -> tuple[bool, dict]:
    """The don't-chase gate. daily: ascending bars with 'close'.
    Returns (skip, numbers). Insufficient history -> skip (we don't buy
    what we can't measure)."""
    closes = [float(b["close"]) for b in daily]
    if len(closes) < 21:
        return True, {"reason": "insufficient history", "bars": len(closes)}
    last = closes[-1]
    mean20 = sma(closes, 20)
    above = (last - mean20) / mean20 if mean20 else 0.0
    gain5 = (last - closes[-6]) / closes[-6] if closes[-6] else 0.0
    numbers = {"last_close": round(last, 4),
               "sma20": round(mean20, 4) if mean20 else None,
               "above_sma20_pct": round(above, 4),
               "gain_5session_pct": round(gain5, 4)}
    ecfg = cfg.get("extended") or {}
    if above > float(ecfg.get("max_above_sma20_pct", 0.10)):
        return True, {**numbers, "reason": "above sma20 limit"}
    if gain5 > float(ecfg.get("max_5session_gain_pct", 0.15)):
        return True, {**numbers, "reason": "5-session run-up limit"}
    return False, numbers


def liquidity_verdict(daily: list[dict], cfg: dict) -> tuple[bool, dict]:
    """Returns (skip, numbers)."""
    lcfg = cfg.get("liquidity") or {}
    closes = [float(b["close"]) for b in daily]
    last = closes[-1] if closes else 0.0
    adv = adv20(daily)
    adv_dollars = (adv * last) if adv else 0.0
    numbers = {"last_close": round(last, 4),
               "adv_dollars": round(adv_dollars, 0)}
    if last < float(lcfg.get("min_price", 3.0)):
        return True, {**numbers, "reason": "price floor"}
    if adv_dollars < float(lcfg.get("min_adv_dollars", 20_000_000)):
        return True, {**numbers, "reason": "dollar-volume floor"}
    return False, numbers


def size_position(effective_capital: float, risk_pct: float, atr: float,
                  k: float, min_viable_risk: float) -> tuple[int, dict]:
    """qty from a fixed-fraction risk budget over a k*ATR stop. qty 0 with
    reason when the budget can't buy a viable stop."""
    risk_budget = effective_capital * risk_pct
    stop_dist = k * atr
    if stop_dist <= 0:
        return 0, {"reason": "no ATR"}
    qty = int(risk_budget // stop_dist)
    actual_risk = qty * stop_dist
    numbers = {"risk_budget": round(risk_budget, 2),
               "stop_distance": round(stop_dist, 4), "qty": qty,
               "actual_risk": round(actual_risk, 2)}
    if qty < 1:
        return 0, {**numbers, "reason": "stop wider than budget"}
    if actual_risk < min_viable_risk:
        return 0, {**numbers, "reason": "below min viable risk"}
    return qty, numbers


def intent_id_for(thesis_id: str, ticker: str, session_date: str) -> str:
    return f"thesis-{thesis_id}-{ticker}-{session_date}"


def materialize_thesis_policy(profile: dict, limit_price: float, atr: float,
                              invalidation: list, expected_move: float
                              ) -> dict:
    """thesis_v1 exit policy in the EXACT shape C4's engine consumes (the
    unit tests pin every key _open_position and evaluate_on_bar read).
    machine_invalidations is [] by design: a thesis dies in the STORE
    (A5 invalidate / staleness), and the nightly management pass converts
    that into an exit — not a price predicate."""
    k = float(profile["initial_stop"]["k"])
    cat_k = float(profile["catastrophe"]["k"])
    return {
        "profile": "thesis_v1",
        "origin": "thesis",
        "initial_stop": {"method": "atr", "k": k,
                         "price": round(limit_price - k * atr, 2)},
        "catastrophe_stop_broker": {"k": cat_k,
                                    "price": round(limit_price - cat_k * atr, 2)},
        "breakeven_at_R": profile["breakeven_at_R"],
        "trail": dict(profile["trail"]),
        "time_stop": None,           # the thesis store IS the time stop
        "realization": {"target_fraction":
                        float(profile["realization"]["target_fraction"]),
                        "action": profile["realization"]["action"]},
        "machine_invalidations": [],
        "news_invalidations": [str(x)[:200] for x in (invalidation or [])][:4],
        "earnings_blackout_exit": bool(profile["earnings_blackout_exit"]),
        "overnight_hold": profile["overnight_hold"],
        "magnitude_est": float(expected_move),
        "atr_14": atr,
        "atr_value": atr,
        "atr_method": "atr",
    }


def tighten_stop(exit_policy: dict, last_price: float) -> tuple[dict, float] | None:
    """Dead-thesis exit arm: raise current_stop to just under the last
    mark so the existing L1 machinery exits next session. Tighten-only —
    returns None when the live stop is already at/above the target."""
    current = float(exit_policy.get("current_stop")
                    or exit_policy["initial_stop"]["price"])
    target = round(last_price * 0.995, 2)
    if target <= current:
        return None
    policy = dict(exit_policy)
    policy["current_stop"] = target
    return policy, target


# --------------------------------------------------------------------------
# store reads
# --------------------------------------------------------------------------

async def load_up_theses(min_conf: float, min_evidence: int) -> list[dict]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT thesis_id, title, confidence, beneficiaries,
                      invalidation, evidence_count, created_decision_id
               FROM journal.theses
               WHERE status='ACTIVE' AND direction='up'
                 AND confidence >= %s AND evidence_count >= %s
               ORDER BY confidence DESC, created_ts""",
            (min_conf, min_evidence))
        rows = await cur.fetchall()
    return [{"thesis_id": r[0], "title": r[1], "confidence": float(r[2]),
             "beneficiaries": r[3] or [], "invalidation": r[4] or [],
             "evidence_count": int(r[5]), "created_decision_id": r[6]}
            for r in rows]


async def open_positions_all() -> list[dict]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT position_id, ticker, origin, qty_open, avg_entry,
                      last_price, r_unit, exit_policy, thesis_decision_id
               FROM journal.positions WHERE status='OPEN'""")
        rows = await cur.fetchall()
    cols = ("position_id", "ticker", "origin", "qty_open", "avg_entry",
            "last_price", "r_unit", "exit_policy", "thesis_decision_id")
    return [dict(zip(cols, r)) for r in rows]


async def pending_thesis_intents() -> set[str]:
    """Tickers with a live not-yet-resolved thesis intent."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT ticker FROM journal.intents
               WHERE intent_id LIKE 'thesis-%%'
                 AND status IN ('PENDING','SUBMITTED')""")
        return {r[0] for r in await cur.fetchall()}


async def thesis_status_by_decision(decision_ids: list[int]) -> dict[int, str]:
    if not decision_ids:
        return {}
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT created_decision_id, status FROM journal.theses
               WHERE created_decision_id = ANY(%s)""", (decision_ids,))
        return {int(r[0]): r[1] for r in await cur.fetchall()}


async def plan_already_done(run_date: str) -> bool:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT 1 FROM journal.decisions
               WHERE stage='RISK' AND agent='C11' AND action='THESIS_PLAN'
                 AND payload->>'run_date' = %s LIMIT 1""", (run_date,))
        return (await cur.fetchone()) is not None


async def enqueue_delayed(queue: str, dedup_key: str, payload: dict,
                          available_ts: datetime, conn) -> None:
    """Queue-native delayed delivery (the A4 open-handoff pattern)."""
    await conn.execute(
        """INSERT INTO queue.messages
             (queue_name, dedup_key, priority, payload, available_ts)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (queue_name, dedup_key) DO NOTHING""",
        (queue, dedup_key, 50, jb(payload), available_ts))


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

async def run_thesis_entries(cfg: dict, profiles: dict,
                             now: datetime | None = None,
                             md=None) -> dict | None:
    """md: tests inject a seeded FakeData (get_marketdata() constructs a
    fresh instance per call, so injection is the only way to script it)."""
    now = now or utcnow()
    run_date = now.astimezone(ET).strftime("%Y-%m-%d")
    ecfg = cfg.get("entry") or {}
    mcfg = cfg.get("management") or {}
    profile = profiles["profiles"]["thesis_v1"]

    if await plan_already_done(run_date):
        log.info("plan already exists — skipping", extra=kv(date=run_date))
        return None

    pool = await get_pool()
    md = md or get_marketdata()
    entry_ts = next_entry_ts(now, int(ecfg.get("entry_blackout_min", 15)))
    session_date = entry_ts.astimezone(ET).strftime("%Y-%m-%d")

    # ---- 1. management pass ------------------------------------------------
    positions = await open_positions_all()
    thesis_pos = [p for p in positions if p["origin"] == "thesis"]
    dead_armed: list[dict] = []
    trim_recos: list[dict] = []
    if thesis_pos:
        status_by_dec = await thesis_status_by_decision(
            [int(p["thesis_decision_id"]) for p in thesis_pos])
        for p in thesis_pos:
            status = status_by_dec.get(int(p["thesis_decision_id"]), "MISSING")
            mark = float(p["last_price"] or p["avg_entry"])
            if status != "ACTIVE" and bool(mcfg.get("exit_dead_theses", True)):
                tightened = tighten_stop(p["exit_policy"], mark)
                if tightened is None:
                    continue
                policy, new_stop = tightened
                async with pool.connection() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            """UPDATE journal.positions SET exit_policy=%s
                               WHERE position_id=%s AND status='OPEN'""",
                            (jb(policy), p["position_id"]))
                        await conn.execute(
                            """INSERT INTO journal.position_events
                                 (position_id, event_type, actor, new_value)
                               VALUES (%s,'STOP_TIGHTENED','C11',%s)""",
                            (p["position_id"],
                             jb({"reason": f"thesis {status} — exit armed",
                                 "new_stop": new_stop, "mark": mark})))
                        await write_decision(
                            signal_id=f"thesis-mgmt-{run_date}",
                            ticker=p["ticker"], stage="RISK", agent="C11",
                            action="THESIS_DEAD_EXIT",
                            payload={"run_date": run_date,
                                     "position_id": p["position_id"],
                                     "thesis_status": status,
                                     "new_stop": new_stop, "mark": mark},
                            reason=f"thesis no longer ACTIVE ({status}) — "
                                   f"stop tightened to {new_stop}", conn=conn)
                dead_armed.append({"ticker": p["ticker"], "stop": new_stop,
                                   "status": status})
                continue
            if bool(mcfg.get("trim_before_earnings", True)):
                try:
                    from c1_ingestion.earnings import earnings_next_sessions
                    sessions = await earnings_next_sessions(p["ticker"])
                except Exception:                     # noqa: BLE001
                    sessions = None
                r_unit = float(p["r_unit"] or 0)
                unreal_r = ((mark - float(p["avg_entry"])) / r_unit
                            if r_unit else 0.0)
                if (sessions is not None and sessions <= 1
                        and unreal_r >= float(mcfg.get("trim_min_gain_R", 0.5))):
                    await write_decision(
                        signal_id=f"thesis-mgmt-{run_date}",
                        ticker=p["ticker"], stage="RISK", agent="C11",
                        action="THESIS_TRIM_RECO",
                        payload={"run_date": run_date,
                                 "position_id": p["position_id"],
                                 "unrealized_R": round(unreal_r, 3),
                                 "earnings_sessions": sessions,
                                 "trim_fraction": mcfg.get("trim_fraction", 0.5)},
                        reason=f"+{unreal_r:.1f}R into earnings in "
                               f"{sessions} session(s) — trim "
                               f"{mcfg.get('trim_fraction', 0.5):.0%} "
                               "(operator policy; v1 recommendation-only)")
                    trim_recos.append({"ticker": p["ticker"],
                                       "unrealized_R": round(unreal_r, 2),
                                       "sessions": sessions})

    # ---- 2. entry planning -------------------------------------------------
    theses = await load_up_theses(float(ecfg.get("min_confidence", 0.5)),
                                  int(ecfg.get("min_evidence", 0)))
    held = {p["ticker"] for p in positions}
    pending = await pending_thesis_intents()
    open_thesis_count = len(thesis_pos)
    per_thesis: dict[str, int] = {}
    for p in thesis_pos:
        for t in theses:
            if t["created_decision_id"] == p["thesis_decision_id"]:
                per_thesis[t["thesis_id"]] = per_thesis.get(t["thesis_id"], 0) + 1

    equity = float(await get_flag("broker_equity", "0") or 0)
    capital = float(await get_flag("trading_capital", "0") or 0)
    effective = min(equity, capital)

    planned: list[dict] = []
    skips: list[dict] = []
    room_total = max(0, int(ecfg.get("max_open_positions", 4))
                     - open_thesis_count)
    budget_today = min(int(ecfg.get("max_new_per_day", 2)), room_total)

    for t in theses:
        if len(planned) >= budget_today:
            break
        for b in t["beneficiaries"]:
            if len(planned) >= budget_today:
                break
            ticker = str(b.get("ticker", "")).upper()
            if (not ticker or ticker in held or ticker in pending
                    or any(pl["ticker"] == ticker for pl in planned)):
                continue
            if per_thesis.get(t["thesis_id"], 0) >= int(
                    ecfg.get("max_per_thesis", 2)):
                break
            skip_reason = None
            numbers: dict = {}
            try:
                daily = await md.daily_bars(ticker, 30)
                atr = atr14(daily)
                liq_skip, liq_n = liquidity_verdict(daily, cfg)
                ext_skip, ext_n = extended_verdict(daily, cfg)
                numbers = {**liq_n, **ext_n}
                if not daily or not atr:
                    skip_reason = "DATA_UNAVAILABLE"
                elif liq_skip:
                    skip_reason = "ILLIQUID"
                elif ext_skip:
                    skip_reason = "EXTENDED"
            except Exception as e:                    # noqa: BLE001 — isolate per symbol
                skip_reason, numbers = "DATA_UNAVAILABLE", {"error": repr(e)[:120]}

            if skip_reason is None:
                prev_close = float(daily[-1]["close"])
                qty, size_n = size_position(
                    effective, float(ecfg.get("risk_pct", 0.0025)), atr,
                    float(profile["initial_stop"]["k"]),
                    float(ecfg.get("min_viable_risk", 200)))
                numbers.update(size_n)
                if qty < 1:
                    skip_reason = "MIN_RISK"

            if skip_reason:
                await write_decision(
                    signal_id=f"thesis-plan-{run_date}", ticker=ticker,
                    stage="RISK", agent="C11", action="THESIS_SKIP",
                    payload={"run_date": run_date, "thesis_id": t["thesis_id"],
                             "skip": skip_reason, **numbers},
                    reason=f"{t['thesis_id']}: {skip_reason.lower()} — "
                           + str(numbers.get("reason", ""))[:120])
                skips.append({"ticker": ticker, "thesis_id": t["thesis_id"],
                              "skip": skip_reason})
                continue

            limit_price = round(prev_close
                                * (1 + float(ecfg.get("chase_buffer_pct", 0.02))), 2)
            policy = materialize_thesis_policy(
                profile, limit_price, atr, t["invalidation"],
                float(ecfg.get("expected_move", 0.20)))
            intent_id = intent_id_for(t["thesis_id"], ticker, session_date)
            stagger = timedelta(minutes=int(ecfg.get("stagger_min", 5))
                                * len(planned))
            body = {"intent_id": intent_id, "ticker": ticker, "qty": qty,
                    "limit_price": limit_price, "exit_policy": policy,
                    "horizon": "LONG",
                    "thesis_decision_id": int(t["created_decision_id"]),
                    "origin": "thesis"}
            async with pool.connection() as conn:
                async with conn.transaction():
                    decision_id = await write_decision(
                        signal_id=intent_id, ticker=ticker, stage="RISK",
                        agent="C11", action="THESIS_ENTRY",
                        payload={"run_date": run_date,
                                 "thesis_id": t["thesis_id"],
                                 "thesis_title": t["title"],
                                 "confidence": t["confidence"],
                                 "session_date": session_date,
                                 "limit_price": limit_price,
                                 "entry_after": entry_ts.isoformat(),
                                 **numbers},
                        reason=f"{t['thesis_id']} ({t['confidence']:.2f}): "
                               f"{qty} {ticker} @ lim {limit_price} — "
                               f"{str(b.get('rationale', ''))[:100]}",
                        confidence=t["confidence"], conn=conn)
                    await conn.execute(
                        """INSERT INTO journal.intents
                             (intent_id, decision_id, ticker, side, qty,
                              limit_price, gate_snapshot, exit_policy,
                              horizon, effective_capital, risk_budget,
                              status, config_version)
                           VALUES (%s,%s,%s,'BUY',%s,%s,%s,%s,'LONG',%s,%s,
                                   'PENDING',%s)
                           ON CONFLICT (intent_id) DO NOTHING""",
                        (intent_id, decision_id, ticker, qty, limit_price,
                         json.dumps({"origin": "thesis", **numbers}),
                         json.dumps(policy), effective,
                         numbers.get("actual_risk"),
                         active_config_version()))
                    out = envelope(CONTRACT_INTENT, "C11", intent_id, None, 1,
                                   body)
                    out["envelope"]["trace"]["decision_id"] = decision_id
                    await enqueue_delayed(EXEC_QUEUE, intent_id, out,
                                          entry_ts + stagger, conn)
            per_thesis[t["thesis_id"]] = per_thesis.get(t["thesis_id"], 0) + 1
            planned.append({"ticker": ticker, "qty": qty,
                            "limit": limit_price,
                            "thesis_id": t["thesis_id"],
                            "entry_after": (entry_ts + stagger).isoformat()})

    # ---- 3. anchor + digest ------------------------------------------------
    stats = {"run_date": run_date, "session_date": session_date,
             "theses_eligible": len(theses), "planned": len(planned),
             "skips": len(skips), "dead_exits_armed": len(dead_armed),
             "trim_recos": len(trim_recos),
             "open_thesis_positions": open_thesis_count,
             "effective_capital": effective}
    lines = [f"THESIS ENTRY PLAN — {run_date} (for session {session_date})",
             "-" * 62]
    if planned:
        for pl in planned:
            lines.append(f"  ENTER {pl['qty']} {pl['ticker']} @ lim "
                         f"{pl['limit']}  [{pl['thesis_id']}] after "
                         f"{pl['entry_after'][11:16]} UTC")
    else:
        lines.append("  No new entries.")
    if dead_armed:
        lines.append("DEAD-THESIS EXITS ARMED")
        for d in dead_armed:
            lines.append(f"  {d['ticker']} stop -> {d['stop']} "
                         f"(thesis {d['status']})")
    if trim_recos:
        lines.append("EARNINGS TRIM RECOMMENDATIONS (not auto-executed)")
        for tr in trim_recos:
            lines.append(f"  {tr['ticker']} +{tr['unrealized_R']}R, reports "
                         f"in {tr['sessions']} session(s) — consider trim")
    if skips:
        lines.append(f"Skipped {len(skips)}: " + ", ".join(
            f"{s['ticker']}({s['skip'].lower()})" for s in skips[:8]))
    lines.append(f"Caps: {open_thesis_count} open, "
                 f"{len(planned)}/{budget_today} today. Sized at "
                 f"{float(ecfg.get('risk_pct', 0.0025)) * 100:.2f}% risk on "
                 f"${effective:,.0f} effective.")
    body_text = "\n".join(lines)

    async with pool.connection() as conn:
        async with conn.transaction():
            decision_id = await write_decision(
                signal_id=f"thesis-plan-{run_date}", stage="RISK",
                agent="C11", action="THESIS_PLAN",
                payload={**stats, "planned_detail": planned,
                         "dead_armed": dead_armed, "trim_recos": trim_recos},
                reason=f"thesis entry plan: {len(planned)} entries, "
                       f"{len(dead_armed)} exits armed, "
                       f"{len(trim_recos)} trim recos", conn=conn)
            if bool((cfg.get("digest") or {}).get("email", True)) and (
                    planned or dead_armed or trim_recos):
                await conn.execute(
                    """INSERT INTO journal.outbox
                         (kind, subject, body, fact_sheet, decision_id)
                       VALUES ('ALERT',%s,%s,%s,%s)""",
                    (f"Thesis entries {run_date} — {len(planned)} planned"
                     + (f", {len(dead_armed)} exits" if dead_armed else "")
                     + (f", {len(trim_recos)} trims" if trim_recos else ""),
                     body_text, jb(stats), decision_id))

    log.info("thesis entry plan done", extra=kv(**stats))
    return stats


async def main() -> None:
    from common.db import close_pool
    cfg = load_yaml(config_path("thesis_entry.yaml"))
    profiles = load_yaml(config_path("exit_profiles.yaml"))
    await register_config_version("c11 thesis entry run")
    try:
        await run_thesis_entries(cfg, profiles)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
