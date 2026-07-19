"""A5 Macro/Thematic (Phase 8) — oneshot, fired by a5-thematic.timer nightly
at 21:30 ET (every day; the Sunday firing is the baseline §6 "deep pass").
The consumer of `signal.thesis` (fed by router rule 3 and A4's thesis lane —
accumulating unread since Phase 2; this agent finally reads it).

Flow (models propose, code disposes — the store is written by code only):
  1. BULK EXPIRY (one SQL pass): thesis-lane messages older than
     lane.max_age_hours are marked done and journaled as ONE
     THEMATIC/EXPIRED_BULK summary row — the multi-week backlog drains on
     first start without journal spam or tokens.
  2. STALENESS EXPIRY (code rule, zero tokens): ACTIVE theses with no
     evidence in store.stale_weeks -> status EXPIRED, one
     THEMATIC/THESIS_EXPIRED row each (baseline L3, long lane: "no
     confirming evidence added in N weeks"). The model cannot veto this.
  3. Acquire a model slot FIRST (heavy via the shared SlotManager, analyst
     fallback). No slot -> journal SKIPPED_NO_MODEL and exit WITHOUT
     claiming — the lane simply waits for tomorrow night. (Unlike A4 there
     is no deterministic fallback: an unread thesis item tonight is worth
     more than a blind guess.)
  4. Claim up to top_k messages (deep_top_k on Sundays), fetch headlines,
     and make ONE grammar-constrained call over (active theses + fresh
     items) -> ThematicUpdate.
  5. Apply with validation, one transaction per item (decision row + store
     write commit together, then ack):
       * evidence -> thesis_evidence insert (redelivery no-ops via the
         UNIQUE constraint) + evidence-clock update;
       * new thesis -> code-minted thesis_id (th-<year>-<seq>) + anchor
         evidence row;
       * unknown thesis_id or unaddressed item -> IGNORE (journaled).
     Then reviews: confidence moves and invalidate/realized status changes.
  6. One THEMATIC/DIGEST decision (the idempotency anchor) + an optional
     digest email via journal.outbox (kind ALERT — C5 mails it).

Invalid model output after retry -> claimed messages are fail()ed back to
the queue (linear backoff; they return tomorrow), THEMATIC/REJECT row, no
DIGEST — a manual same-night rerun is allowed to try again. Idempotent per
ET date via the DIGEST decision. The run is session-independent: news
accrues on weekends too, so there is no SKIPPED_NO_SESSION here.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from common.config import config_path, load_yaml
from common.clock import utcnow
from common.db import get_pool, jb
from common.journal import (active_config_version, register_config_version,
                            write_decision)
from common.log import get_logger, kv
from common.queue import ack, claim, fail
from a7_report.service import SlotManager

from . import store
from .prompt import build_messages
from .render import render_digest, subject_line
from .schema import (ThematicUpdate, ThematicValidationError,
                     thematic_json_schema, validate_thematic)

log = get_logger("a5.thematic")

IN_QUEUE = "signal.thesis"
CONSUMER = "a5-thematic"
ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------
# queue / journal helpers
# --------------------------------------------------------------------------

async def bulk_expire(cutoff: datetime) -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """UPDATE queue.messages SET done_ts = now()
               WHERE queue_name = %s AND done_ts IS NULL
                 AND claimed_ts IS NULL AND enqueued_ts < %s""",
            (IN_QUEUE, cutoff))
        return cur.rowcount or 0


async def queue_depth() -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT count(*) FROM queue.messages
               WHERE queue_name = %s AND done_ts IS NULL""", (IN_QUEUE,))
        return (await cur.fetchone())[0]


async def fetch_item(item_id: str, revision: int) -> dict:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT headline, summary, source_tier, received_ts
               FROM news.news_items WHERE item_id=%s AND revision=%s""",
            (item_id, revision))
        row = await cur.fetchone()
        if row is None:
            return {}
        return {"headline": row[0], "summary": row[1], "source_tier": row[2],
                "received_ts": row[3]}


async def digest_already_done(run_date: str) -> bool:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT 1 FROM journal.decisions
               WHERE stage='THEMATIC' AND agent='A5' AND action='DIGEST'
                 AND payload->>'run_date' = %s LIMIT 1""", (run_date,))
        return (await cur.fetchone()) is not None


# --------------------------------------------------------------------------
# model call
# --------------------------------------------------------------------------

async def update_with_model(backend, theses: list[dict], items: list[dict],
                            deep: bool, retries: int):
    """Returns (ThematicUpdate | None, model_id | None, latency_ms)."""
    if backend is None:
        return None, None, 0
    error: ThematicValidationError | None = None
    total = 0
    for _ in range(1 + retries):
        messages = build_messages(theses, items, deep,
                                  retry_error=error.detail if error else None)
        try:
            reply = await backend.complete(messages, thematic_json_schema())
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            log.warning("thematic model call failed",
                        extra=kv(error=repr(e)[:200]))
            return None, None, total
        total += reply.latency_ms
        try:
            return validate_thematic(reply.text), reply.model_id, total
        except ThematicValidationError as e:
            error = e
            log.warning("invalid thematic output",
                        extra=kv(detail=e.detail[:150]))
    return None, backend.model_id, total


def resolve_ops(update: ThematicUpdate, known_ids: set[str],
                claimed_item_ids: list[str]) -> tuple[dict, list, list, int]:
    """Pure: reconcile the model's lists against reality. Returns
    (item_id -> op dict, valid new_theses, valid reviews, downgraded count).
    Precedence: a new_theses anchor wins over any ItemOp for the same item;
    unknown thesis_ids and unlisted item_ids are downgraded to ignore."""
    downgraded = 0
    anchors = {t.anchor_item_id for t in update.new_theses
               if t.anchor_item_id in set(claimed_item_ids)}
    new_theses = [t for t in update.new_theses
                  if t.anchor_item_id in anchors]

    ops: dict[str, dict] = {}
    for op in update.items:
        if op.item_id not in claimed_item_ids or op.item_id in anchors:
            continue
        if op.op == "evidence" and op.thesis_id not in known_ids:
            downgraded += 1
            ops[op.item_id] = {"op": "ignore",
                               "note": "model referenced unknown thesis "
                                       f"{op.thesis_id!r} — downgraded"}
            continue
        ops[op.item_id] = op.model_dump()

    reviews = [r for r in update.reviews if r.thesis_id in known_ids]
    return ops, new_theses, reviews, downgraded


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

async def run_thematic(cfg: dict, backend_override=None,
                       now: datetime | None = None) -> int | None:
    """Returns the digest's outbox message_id (None when skipped or when the
    digest email is disabled)."""
    now = now or utcnow()
    run_date = now.astimezone(ET).strftime("%Y-%m-%d")
    deep = (now.astimezone(ET).weekday() == 6
            or bool((cfg.get("lane") or {}).get("force_deep", False)))
    lcfg = cfg.get("lane") or {}
    scfg = cfg.get("store") or {}
    max_age_h = float(lcfg.get("max_age_hours", 168))
    top_k = int(lcfg.get("deep_top_k", 60) if deep else lcfg.get("top_k", 25))
    stale_weeks = float(scfg.get("stale_weeks", 6))
    signal_anchor = f"thematic-{run_date}"

    if await digest_already_done(run_date):
        log.info("digest already exists — skipping", extra=kv(date=run_date))
        return None

    pool = await get_pool()

    # 1. bulk expiry ---------------------------------------------------------
    expired_bulk = await bulk_expire(now - timedelta(hours=max_age_h))
    if expired_bulk:
        await write_decision(
            signal_id=signal_anchor, stage="THEMATIC", agent="A5",
            action="EXPIRED_BULK",
            payload={"run_date": run_date, "expired_count": expired_bulk,
                     "max_age_hours": max_age_h},
            reason=f"bulk-expired {expired_bulk} thesis-lane messages older "
                   f"than {max_age_h:.0f}h")
        log.info("bulk expired", extra=kv(count=expired_bulk))

    # 2. deterministic staleness expiry --------------------------------------
    expired_stale: list[dict] = []
    for t in await store.stale_active(store.stale_cutoff(now, stale_weeks)):
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=signal_anchor, stage="THEMATIC", agent="A5",
                    action="THESIS_EXPIRED",
                    payload={"run_date": run_date, "thesis_id": t["thesis_id"],
                             "title": t["title"],
                             "last_evidence_ts": t["last_evidence_ts"].isoformat(),
                             "stale_weeks": stale_weeks},
                    reason=f"no evidence in {stale_weeks:.0f} weeks — "
                           "staleness expiry (code rule)", conn=conn)
                await store.set_status(conn, t["thesis_id"], "EXPIRED",
                                       decision_id)
        expired_stale.append(t)

    # 3. slot first — no slot, no claims. (But no WORK, no slot either: a
    #    quiet non-deep night must not start the 77GB heavy model for an
    #    empty lane — it journals a quiet digest and exits.)
    pending = await queue_depth()
    slots = SlotManager(cfg)
    outbox_id: int | None = None
    try:
        if pending == 0 and not deep:
            backend, slot_name = None, "none"
        elif backend_override is not None:
            backend, slot_name = backend_override, "stub"
        else:
            backend, slot_name = await slots.acquire()
        if backend is None and (pending > 0 or deep):
            await write_decision(
                signal_id=signal_anchor, stage="THEMATIC", agent="A5",
                action="SKIPPED_NO_MODEL",
                payload={"run_date": run_date, "queue_depth": pending},
                reason="no model slot available — thesis lane left queued "
                       "for the next nightly run")
            log.warning("no model slot — skipped")
            return None

        # 4. claim + model ----------------------------------------------------
        claimed: list = []
        items: list[dict] = []
        by_item: dict[str, object] = {}
        while len(claimed) < top_k:
            msg = await claim(IN_QUEUE, CONSUMER)
            if msg is None:
                break
            body = msg.payload.get("body") or {}
            item_ref = body.get("item_ref") or {}
            triage = body.get("triage") or {}
            item_id = item_ref.get("item_id")
            revision = int(item_ref.get("revision") or 1)
            if not item_id or item_id in by_item:
                await fail(msg.msg_id, "malformed or duplicate thesis signal")
                continue
            meta = await fetch_item(item_id, revision)
            received = meta.get("received_ts")
            claimed.append(msg)
            by_item[item_id] = msg
            items.append({
                "item_id": item_id, "revision": revision,
                "headline": meta.get("headline"),
                "summary": (meta.get("summary") or "")[:300],
                "source_tier": meta.get("source_tier"),
                "tickers": triage.get("tickers") or [],
                "triage_reason": (triage.get("reason") or "")[:200],
                "age_hours": (round((now - received).total_seconds() / 3600, 1)
                              if received else None),
            })

        theses = await store.load_active()
        update = model_id = None
        latency = 0
        if items or (deep and theses):
            retries = int((cfg.get("narrative") or {})
                          .get("retries_on_invalid", 1))
            update, model_id, latency = await update_with_model(
                backend, theses, items, deep, retries)
            if update is None:
                for msg in claimed:
                    await fail(msg.msg_id, "thematic model unavailable/invalid")
                await write_decision(
                    signal_id=signal_anchor, stage="THEMATIC", agent="A5",
                    action="REJECT",
                    payload={"run_date": run_date, "claimed": len(claimed),
                             "slot": slot_name},
                    reason="model output invalid after retry — items "
                           "released back to the lane", model_id=model_id,
                    latency_ms=latency)
                return None
        else:
            update = ThematicUpdate(items=[], new_theses=[], reviews=[],
                                    summary="Quiet night — thesis lane empty.")

        # 5. apply ------------------------------------------------------------
        known_ids = {t["thesis_id"] for t in theses}
        ops, new_specs, reviews, downgraded = resolve_ops(
            update, known_ids, [c["item_id"] for c in items])

        created: list[dict] = []
        anchor_ev: dict[str, dict] = {}
        for spec in new_specs:
            async with pool.connection() as conn:
                async with conn.transaction():
                    thesis_id = await store.next_thesis_id(conn, now)
                    decision_id = await write_decision(
                        signal_id=spec.anchor_item_id,
                        item_id=spec.anchor_item_id,
                        stage="THEMATIC", agent="A5", action="NEW_THESIS",
                        payload={"run_date": run_date, "thesis_id": thesis_id,
                                 **spec.model_dump()},
                        reason=f"{thesis_id}: {spec.title}",
                        confidence=spec.confidence, model_id=model_id,
                        conn=conn)
                    await store.create_thesis(conn, thesis_id, spec,
                                              active_config_version(),
                                              decision_id)
                    await store.add_evidence(
                        conn, thesis_id, spec.anchor_item_id,
                        next((c["revision"] for c in items
                              if c["item_id"] == spec.anchor_item_id), 1),
                        "SUPPORTS", "anchor item", decision_id, now)
            msg = by_item.get(spec.anchor_item_id)
            if msg is not None:
                await ack(msg.msg_id)
            created.append({"thesis_id": thesis_id, **spec.model_dump()})
            anchor_ev[spec.anchor_item_id] = {"thesis_id": thesis_id}

        evidence_rows: list[dict] = []
        ignored = 0
        for c in items:
            if c["item_id"] in anchor_ev:
                continue
            op = ops.get(c["item_id"]) or {
                "op": "ignore", "note": "not addressed by model"}
            msg = by_item[c["item_id"]]
            async with pool.connection() as conn:
                async with conn.transaction():
                    if op["op"] == "evidence":
                        decision_id = await write_decision(
                            signal_id=c["item_id"], item_id=c["item_id"],
                            item_revision=c["revision"], stage="THEMATIC",
                            agent="A5", action="EVIDENCE",
                            payload={"run_date": run_date,
                                     "thesis_id": op["thesis_id"],
                                     "polarity": op["polarity"].upper(),
                                     "note": op.get("note", ""),
                                     "headline": c["headline"]},
                            reason=op.get("note") or "evidence attached",
                            model_id=model_id, conn=conn)
                        await store.add_evidence(
                            conn, op["thesis_id"], c["item_id"],
                            c["revision"], op["polarity"].upper(),
                            op.get("note", ""), decision_id, now)
                        evidence_rows.append({**op, "item_id": c["item_id"],
                                              "headline": c["headline"]})
                    else:
                        await write_decision(
                            signal_id=c["item_id"], item_id=c["item_id"],
                            item_revision=c["revision"], stage="THEMATIC",
                            agent="A5", action="IGNORE",
                            payload={"run_date": run_date,
                                     "note": op.get("note", ""),
                                     "headline": c["headline"]},
                            reason=op.get("note") or "thesis-lane noise",
                            model_id=model_id, conn=conn)
                        ignored += 1
            await ack(msg.msg_id)

        status_changes: list[dict] = []
        for r in reviews:
            async with pool.connection() as conn:
                async with conn.transaction():
                    if r.op == "keep":
                        await write_decision(
                            signal_id=signal_anchor, stage="THEMATIC",
                            agent="A5", action="THESIS_UPDATE",
                            payload={"run_date": run_date,
                                     "thesis_id": r.thesis_id,
                                     "confidence": r.confidence,
                                     "note": r.note},
                            reason=r.note or "confidence updated",
                            confidence=r.confidence, model_id=model_id,
                            conn=conn)
                        await store.set_confidence(conn, r.thesis_id,
                                                   r.confidence)
                    else:
                        status = ("INVALIDATED" if r.op == "invalidate"
                                  else "REALIZED")
                        decision_id = await write_decision(
                            signal_id=signal_anchor, stage="THEMATIC",
                            agent="A5",
                            action=f"THESIS_{status}",
                            payload={"run_date": run_date,
                                     "thesis_id": r.thesis_id,
                                     "note": r.note},
                            reason=r.note or f"model review: {r.op}",
                            confidence=r.confidence, model_id=model_id,
                            conn=conn)
                        await store.set_status(conn, r.thesis_id, status,
                                               decision_id)
                        status_changes.append({"thesis_id": r.thesis_id,
                                               "status": status,
                                               "note": r.note})

        # 6. digest anchor + email --------------------------------------------
        active_after = len(await store.load_active())
        stats = {"run_date": run_date, "deep": deep, "slot": slot_name,
                 "processed": len(items), "new_theses": len(created),
                 "evidence_attached": len(evidence_rows), "ignored": ignored,
                 "downgraded_ops": downgraded,
                 "status_changes": len(status_changes),
                 "stale_expired": len(expired_stale),
                 "expired_bulk": expired_bulk,
                 "left_queued": await queue_depth(),
                 "active_after": active_after}
        subject = subject_line(run_date, stats)
        body_text = render_digest(
            run_date, update.summary, created, evidence_rows, status_changes,
            expired_stale, stats, slot_name)
        email = bool((cfg.get("digest") or {}).get("email", True))
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=signal_anchor, stage="THEMATIC", agent="A5",
                    action="DIGEST",
                    payload={**stats, "summary": update.summary},
                    reason=update.summary, model_id=model_id,
                    latency_ms=latency, conn=conn)
                if email:
                    cur = await conn.execute(
                        """INSERT INTO journal.outbox
                             (kind, subject, body, fact_sheet, decision_id)
                           VALUES ('ALERT',%s,%s,%s,%s)
                           RETURNING message_id""",
                        (subject, body_text, jb(stats), decision_id))
                    outbox_id = (await cur.fetchone())[0]

        log.info("thematic digest done",
                 extra=kv(**{**stats, "outbox_id": outbox_id}))
        return outbox_id
    finally:
        if backend_override is None:
            await slots.release()


async def main() -> None:
    from common.db import close_pool
    cfg = load_yaml(config_path("a5.yaml"))
    await register_config_version("a5 thematic run")
    try:
        await run_thematic(cfg)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
