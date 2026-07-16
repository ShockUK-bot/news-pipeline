"""File-for-evaluation — A13's single pipeline-affecting act.

The model PROPOSES (AnswerOutput.filing_proposal), the operator CONFIRMS on
the dashboard (kill-token gated there), and this module — code — DISPOSES.
A filed ticker enters the existing synthetic lane (queue-contracts-spec §10):
A1 re-triages the anchor news item FOR the requested ticker, then A2 → C3 →
A3 apply every normal gate. Full thesis lineage is preserved, so a resulting
position satisfies A3's NO_THESIS_LINEAGE invariant like any other.

Code-side gates (in order):
  FILING_DISABLED   config filing.enabled is false
  KILL_SWITCH       control.kill_switch = '1' — no new evaluations under kill
  BREAKER           control.drawdown_breaker = '1'
  NO_ANCHOR         anchor_item_id not found in the news store
  STALE_ANCHOR      anchor item older than filing.anchor_max_age_hours
                    (the system trades news; a stale anchor is a hunch)

On success, ONE TRANSACTION writes: the CHAT/FILED decision row, the audit
row (operator attribution), and the signal.synthetic enqueue — a filing can't
exist without its journal trail, and vice versa.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from common.clock import utcnow
from common.contracts import envelope
from common.db import get_pool
from common.journal import write_decision
from common.log import get_logger, kv
from common.queue import enqueue

from .schema import FilingProposal

log = get_logger("a13.filing")

SYNTHETIC_QUEUE = "signal.synthetic"
CONTRACT_SYNTHETIC = "signal.synthetic/1"


class FilingRejected(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def anchor_is_fresh(received_ts: datetime, now: datetime,
                    max_age_hours: float) -> bool:
    """Pure freshness rule (unit-tested without a DB)."""
    return received_ts >= now - timedelta(hours=max_age_hours)


async def _control(key: str) -> str | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT value FROM journal.control WHERE key = %s", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def file_for_evaluation(proposal: FilingProposal, operator: str,
                              chat_message_id: int, cfg: dict) -> tuple[int, str]:
    """Returns (decision_id, synthetic_id). Raises FilingRejected."""
    fcfg = cfg.get("filing") or {}
    if not fcfg.get("enabled", True):
        raise FilingRejected("FILING_DISABLED", "filing is disabled in config/a13.yaml")
    if await _control("kill_switch") == "1":
        raise FilingRejected("KILL_SWITCH", "kill switch is armed — not filing")
    if await _control("drawdown_breaker") == "1":
        raise FilingRejected("BREAKER", "drawdown breaker is tripped — not filing")

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT item_id, revision, received_ts, headline
               FROM news.news_items WHERE item_id = %s
               ORDER BY revision DESC LIMIT 1""",
            (proposal.anchor_item_id,))
        row = await cur.fetchone()
    if row is None:
        raise FilingRejected("NO_ANCHOR",
                             f"anchor item {proposal.anchor_item_id!r} not in news store")
    item_id, revision, received_ts, headline = row
    max_age = float(fcfg.get("anchor_max_age_hours", 72))
    if not anchor_is_fresh(received_ts, utcnow(), max_age):
        raise FilingRejected(
            "STALE_ANCHOR",
            f"anchor item is older than {max_age:g}h — nothing fresh to evaluate")

    signal_id = f"chat-{chat_message_id}"
    async with pool.connection() as conn:
        async with conn.transaction():
            decision_id = await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                ticker=proposal.ticker, stage="CHAT", agent="A13",
                action="FILED",
                payload={"proposal": proposal.model_dump(),
                         "operator": operator,
                         "chat_message_id": chat_message_id,
                         "anchor_headline": headline},
                reason=f"operator-filed evaluation: {proposal.rationale}"[:400],
                conn=conn)
            syn_id = f"op-{decision_id}-{proposal.ticker}"
            body = {
                "synthetic_id": syn_id,
                "derived_from_decision": decision_id,
                "derived_from_item": {"item_id": item_id, "revision": revision},
                "ticker": proposal.ticker,
                "relation": "operator_inquiry",
                "rationale": proposal.rationale,
            }
            out = envelope(CONTRACT_SYNTHETIC, "A13", syn_id, item_id,
                           revision, body)
            await enqueue(SYNTHETIC_QUEUE, syn_id, out, conn=conn)
            await conn.execute(
                """INSERT INTO journal.audit (actor, action, old_value, new_value, detail)
                   VALUES (%s, 'CHAT_SIGNAL_FILED', NULL, %s, %s)""",
                (operator, proposal.ticker,
                 f"decision {decision_id}, anchor {item_id} rev {revision}: "
                 f"{proposal.rationale}"[:400]))

    log.info("operator inquiry filed", extra=kv(
        ticker=proposal.ticker, decision_id=decision_id, synthetic_id=syn_id,
        anchor=item_id, operator=operator))
    return decision_id, syn_id
