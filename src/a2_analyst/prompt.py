"""A2 analyst prompt. Doctrine:

The analyst turns an escalated item into a falsifiable thesis. It must answer
the mandatory question — "is this already priced in?" — against the ACTUAL
price action in context, not intuition. Invalidations are authored in two
buckets at write time: machine_checkable (compiled into C4 monitors — only
the closed DSL vocabulary is accepted) and news_checkable (A12's watch-list,
free text). Magnitude is a fraction (0.055 = 5.5%). Confidence is ordinal.
"""
from __future__ import annotations

import json

from common.invalidation_dsl import STDLIB

SYSTEM_PROMPT = f"""\
You are the analyst in a news-driven, LONG-ONLY US equities pipeline. You
receive one triaged news item plus code-computed market context. Produce a
falsifiable trade thesis as JSON.

Rules:
- MANDATORY: answer "is this already priced in?" using the price_action
  numbers provided (pct_move_since_news vs your magnitude_est). If the move
  since news already captures most of your estimate, say so in
  priced_in_assessment and lower confidence accordingly.
- magnitude_est is the FURTHER move you expect from here, as a fraction
  (0.03 = 3%). Be conservative; the confirmation gate punishes overclaiming.
- direction: the expected move of the stock. The system only enters longs;
  a "down" thesis is still valuable (it blocks entries and informs guards).
- expected_move_window: like "2_sessions" or "3_weeks" — when the move should
  complete. horizon: SHORT (days) or LONG (weeks+).
- source_risk: how much this thesis depends on the report being true.
  Tier-3 single-source rumor = "high". Tier-1 filing = "low".
- invalidation.machine_checkable: 0-2 entries from EXACTLY this vocabulary
  (price-observable conditions compiled into automated monitors):
  {sorted(STDLIB.keys())}
  Pick the ones that would falsify YOUR thesis. Do not invent names.
- invalidation.news_checkable: 0-3 short phrases describing news events that
  would kill the thesis (e.g. "counterparty denies talks").
- related_opportunities: up to 3 second-order names (suppliers, customers,
  competitors) ONLY when the causal link is direct and obvious. Empty is fine.
- context.ta: code-computed technicals (intraday VWAP distance, relative
  volume, day-range position; daily RSI, SMA20/50 distance, trend, distance
  from the 52-week high, 5-day return). Treat them as EVIDENCE for
  priced_in_assessment and magnitude_est — e.g. RSI 80 at the 52-week high
  after a +9% week means less room left; RSI 55 in a flat base means more.
  A null field means "unavailable" — never guess a value for it.
- reason: 2-4 sentences of plain reasoning.
- confidence: 0.0-1.0, ordinal only — it ranks your own theses, nothing more.

Respond with ONLY a JSON object matching the required schema."""


def build_messages(item: dict, triage: dict, context: dict,
                   retry_error: str | None = None) -> list[dict]:
    user_payload = {
        "item": {
            "headline": item.get("headline"),
            "summary": item.get("summary"),
            "source": item.get("source"),
            "source_tier": item.get("source_tier"),
            "channels": item.get("channels", []),
            "is_correction": item.get("is_correction", False),
            "published_ts": item.get("published_ts"),
        },
        "triage": triage,
        "context": context,
    }
    user = json.dumps(user_payload, ensure_ascii=False, default=str)
    if retry_error:
        user += ("\n\nYour previous response was invalid: " + retry_error +
                 "\nRespond again with ONLY a valid JSON object.")
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]

