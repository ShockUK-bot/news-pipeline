"""Story-level repeat suppression (v0.4.7).

The 2026-07-15 saturation diagnosis: one story ("Apple Stock Hits 52-Week
High") was re-analyzed ~14 times over three hours — re-syndicated wordings in
the 0.80-0.90 RELATED band and article-update revisions all re-entered A1,
and every ESCALATE re-entered A2 (the saturated 32B slot). Repeat suppression
is a code-level (deterministic) pre-model check: one article and its
revisions/re-syndications cost at most ONE analysis unless materially new
information arrives.

Mechanism — before calling the model, A1 looks up the most recent A1 TRIAGE
verdict (ESCALATE|DISCARD; REJECT and SUPPRESS rows don't count) for the same
story cluster inside the cooldown window. If one exists, the item is
journaled as action='SUPPRESS' (model_id NULL — pure code, no tokens burned)
referencing the prior decision, and nothing is enqueued.

Bypasses — "materially new information" and the A12 mandate:
  * is_correction items are never suppressed (every stored revision carries
    is_correction=true per v0.4 store semantics; C2's revision policy drops
    the cosmetic ones, so a revision that reaches A1 has semantically changed
    text and deserves a fresh verdict);
  * position-touching items are never suppressed: the union of the incoming
    feed-tagged symbols and the prior verdict's tickers is intersected with
    open positions — corrections/updates on held names must reach A12
    (baseline §4, v0.2 guard path);
  * corroboration crossing the re-escalate threshold: a story first seen from
    one outlet that is now independently corroborated by >= N outlets is new
    information (C3's credibility input changed regime) — re-triage it;
  * is_new_story clusters are by definition not repeats (no lookup runs).

The lookup keys on the story CLUSTER, not the item: journal.decisions has no
cluster column (by design — payload carries full structure), so it joins
news.cluster_members on item_id. Synthetic (sympathy-lane) decisions are
excluded — they are verdicts about a DIFFERENT ticker's exposure, not about
the story itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from common.db import get_pool


DEFAULTS = {
    "enabled": True,
    "window_hours": 24,               # ESCALATE priors: one analysis per story
    "discard_window_hours": 2,        # v0.12.18: DISCARD priors cool off fast
    "corroboration_reescalate_threshold": 3,
}


@dataclass(frozen=True)
class PriorVerdict:
    decision_id: int
    action: str                        # ESCALATE | DISCARD
    tickers: tuple[str, ...]           # prior triage tickers (guard-union input)
    independent_outlets: int           # outlets at prior decision time
    age_hours: float                   # v0.12.18: how old the prior verdict is


def discard_cooldown_expired(action: str, age_hours: Optional[float],
                             discard_window_hours: float) -> bool:
    """v0.12.18 — the SPCX dedup blackhole (2026-08-06/07): once a story
    cluster was DISCARDed, every follow-up was suppressed into it for 24
    hours — including next-day items carrying a NEW catalyst ("shares
    trading higher after Argus upgrade" suppressed into a cluster discarded
    long before). Suppression into a DISCARD exists to flood-control wire
    reprints, which arrive minutes apart — not to freeze a verdict on the
    story for a full session. True -> the DISCARD prior is stale and the
    item deserves a fresh triage. ESCALATE priors keep the long window: an
    analyst already saw that story, so repeats genuinely add nothing."""
    return action == "DISCARD" and age_hours is not None \
        and float(age_hours) > float(discard_window_hours)


async def find_prior_verdict(cluster_id: int,
                             window_hours: float) -> Optional[PriorVerdict]:
    """Most recent non-synthetic A1 TRIAGE verdict for this story cluster
    inside the window. None -> no suppression candidate."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT d.decision_id, d.action,
                      COALESCE(d.payload->'triage'->'tickers', '[]'::jsonb),
                      COALESCE((d.payload->'cluster'->>'independent_outlets')::int, 1),
                      EXTRACT(EPOCH FROM (now() - d.ts)) / 3600.0
               FROM journal.decisions d
               JOIN (SELECT DISTINCT item_id FROM news.cluster_members
                     WHERE cluster_id = %s) cm ON cm.item_id = d.item_id
               WHERE d.stage = 'TRIAGE' AND d.agent = 'A1'
                 AND d.action IN ('ESCALATE', 'DISCARD')
                 AND (d.payload->>'synthetic') IS DISTINCT FROM 'true'
                 AND d.ts > now() - (interval '1 hour' * %s)
               ORDER BY d.ts DESC LIMIT 1""",
            (cluster_id, window_hours))
        row = await cur.fetchone()
    if row is None:
        return None
    decision_id, action, tickers, outlets, age_hours = row
    return PriorVerdict(decision_id=decision_id, action=action,
                        tickers=tuple(t for t in (tickers or []) if isinstance(t, str)),
                        independent_outlets=int(outlets),
                        age_hours=float(age_hours))


def corroboration_bypass(outlets_now: int, outlets_prior: int,
                         threshold: int) -> bool:
    """New information via corroboration: the story crossed the independent-
    outlet threshold since the prior verdict."""
    return outlets_now >= threshold > outlets_prior
