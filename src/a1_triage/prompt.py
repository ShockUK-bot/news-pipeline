"""A1 triage prompt. The doctrine (Phase 2 design, tightened v0.4.7 after the
2026-07-15 pass-through incident — 79.6% ESCALATE vs a 15-25% target):

Material = plausibly moves a specific US-listed equity >=2% within days,
BECAUSE OF A CATALYST IN THE NEWS ITSELF. A1 is a filter, not an analyst: it
does NOT estimate magnitude, does NOT assess credibility (C3's job), does NOT
decide horizon (A2's job). It answers: is this a catalyst class we trade,
which tickers, which direction on its face, how urgent, how novel — and how
confident it is in the material verdict (v0.4.7: `confidence`, journaled).

Recall discipline, reconciled with baseline §4 ("bias: high recall"): high
recall applies to the CATALYST TAXONOMY — never discard a taxonomy match
because details are thin. It does not apply to the NEGATIVE CATEGORIES: those
are not marginal cases, they are known non-events, and "can influence investor
sentiment" is never a justification (anything can).
"""
from __future__ import annotations

import json

SYSTEM_PROMPT = """\
You are the triage filter in a news-driven US equities trading pipeline. For
each news item, decide whether it is MATERIAL: a catalyst plausibly capable of
moving a specific US-listed stock at least 2% within days.

MATERIAL — the item must fit one of these catalyst classes:
1. Earnings/guidance: results released now; guidance raised, lowered, or
   withdrawn; pre-announcements and warnings.
2. M&A / strategic: takeover offers or merger agreements; credible
   strategic-alternatives or sale-process reports; activist stakes.
3. Regulatory/clinical: FDA approvals, CRLs, clinical holds; major trial
   results; government investigations, enforcement actions, or settlements
   naming the company.
4. Operations: major contract wins or losses; product recalls;
   supply-chain disruptions naming the company; plant shutdowns; strikes.
5. Capital structure & credit: dividend initiated, cut, or suspended;
   buyback materially changed; credit-rating CHANGE; large equity/debt
   raises; bankruptcy or going-concern language.
6. Leadership: unexpected CEO/CFO departure, effective now or imminent.
7. 8-K or other filings with substantive items of the classes above.

NOT MATERIAL — these categories are never material, regardless of wording:
1. Analyst actions without a rating change: "Maintains"/"Reiterates" with a
   price-target raise or cut; coverage initiations; price-target-only moves.
   (An actual upgrade/downgrade of the rating MAY be material.)
2. Price-action commentary: 52-week highs/lows, "shares rise/fall X%",
   unusual-volume movers, technical levels. News ABOUT the price is not a
   catalyst — that move has already happened.
3. Distant-future scheduled events: executive transitions effective a year or
   more out; conference presentations; announcements of future earnings dates.
4. Sub-materiality transactions: acquisitions, contracts, or investments
   trivially small relative to the company (single-digit $M for a mid/large
   cap; roughly <1% of market cap).
5. Political/macro commentary without a company-specific catalyst.
6. Routine PR: awards, conference attendance, minor product updates,
   listicles, market-recap roundups, analyst-day recaps without new guidance.
7. Crypto/forex-only news; non-US-listed companies with no US-listed affiliate.

Discipline: if the item fits a NOT MATERIAL category, material=false with high
confidence — do not rescue it with speculation about sentiment. If it fits a
MATERIAL catalyst class, escalate even if details are thin (recall applies to
catalysts, not to non-events). If it fits neither list, ask: what specific
repricing would this cause, in whom, why now? No specific answer -> false.

Fields:
- material: boolean per the taxonomy above.
- tickers: US-listed symbols this DIRECTLY concerns. Include feed-tagged
  symbols you agree with, add obvious ones the text names (e.g. "Apple" ->
  AAPL). Leave empty if none is clearly identifiable — do NOT guess.
- direction_hint: "up", "down", or "unclear" — the face-value read for the
  primary ticker. Not a prediction; a reading of the text.
- urgency: "high" = market reaction likely within hours (M&A, FDA, earnings
  out now); "medium" = within days; "low" = slow-burn or uncertain timing.
- novelty_score: 0.0-1.0. 1.0 = first report of a new event; 0.5 = meaningful
  development of a known story; 0.1 = rehash of widely known information.
- confidence: 0.0-1.0 — your confidence in the material verdict itself.
  Clear taxonomy match (either list) -> 0.8+; genuine judgment call -> 0.4-0.7.
- reason: one or two sentences naming WHICH catalyst class (material) or WHICH
  negative category (not material) applies. "Could influence investor
  sentiment" is not a reason and must never appear.

Respond with ONLY a JSON object matching the required schema."""


FEW_SHOT: list[tuple[dict, dict]] = [
    (
        {"headline": "Acme Corp receives unsolicited acquisition proposal at $45/share",
         "summary": "Board confirms receipt; no decision made.",
         "source": "alpaca_benzinga", "source_tier": 2, "symbols": ["ACME"],
         "channels": [], "is_new_story": True, "independent_outlets": 1},
        {"material": True, "tickers": ["ACME"], "direction_hint": "up",
         "urgency": "high", "novelty_score": 1.0, "confidence": 0.95,
         "reason": "M&A catalyst class: confirmed takeover approach at a specific price."},
    ),
    (
        {"headline": "Mizuho Maintains Underperform on Sunrun, Raises Price Target to $11",
         "summary": "Analyst cites improving cash generation but keeps rating unchanged.",
         "source": "alpaca_benzinga", "source_tier": 2, "symbols": ["RUN"],
         "channels": [], "is_new_story": True, "independent_outlets": 1},
        {"material": False, "tickers": ["RUN"], "direction_hint": "unclear",
         "urgency": "low", "novelty_score": 0.3, "confidence": 0.9,
         "reason": "Analyst action without a rating change: maintained rating with a PT move is negative category 1."},
    ),
    (
        {"headline": "Apple Stock Hits 52-Week High",
         "summary": "Shares of Apple touched a fresh 52-week high Tuesday amid a broad tech rally.",
         "source": "alpaca_benzinga", "source_tier": 2, "symbols": ["AAPL"],
         "channels": [], "is_new_story": True, "independent_outlets": 1},
        {"material": False, "tickers": ["AAPL"], "direction_hint": "unclear",
         "urgency": "low", "novelty_score": 0.1, "confidence": 0.95,
         "reason": "Price-action commentary: news about the price is not a catalyst, the move already happened."},
    ),
    (
        {"headline": "Celldex CFO Sam Martin Plans to Retire in 2027",
         "summary": "Company announces long-term succession plan; Martin to remain through transition.",
         "source": "rss:globenewswire", "source_tier": 3, "symbols": ["CLDX"],
         "channels": [], "is_new_story": True, "independent_outlets": 1},
        {"material": False, "tickers": ["CLDX"], "direction_hint": "unclear",
         "urgency": "low", "novelty_score": 0.4, "confidence": 0.85,
         "reason": "Distant-future scheduled event: an orderly retirement a year-plus out is negative category 3, not an unexpected departure."},
    ),
    (
        {"headline": "Nexatech (NASDAQ: NXTC) to Acquire Regional IT Consultancy for $6.25M",
         "summary": "All-cash deal expected to close in Q4; Nexatech market cap ~$4.2B.",
         "source": "rss:prnewswire-news", "source_tier": 3, "symbols": ["NXTC"],
         "channels": [], "is_new_story": True, "independent_outlets": 1},
        {"material": False, "tickers": ["NXTC"], "direction_hint": "unclear",
         "urgency": "low", "novelty_score": 0.6, "confidence": 0.85,
         "reason": "Sub-materiality transaction: a $6.25M bolt-on against a multi-billion market cap is negative category 4."},
    ),
    (
        {"headline": "8-K - ZENITH PHARMA INC (0001234567) (Filer)",
         "summary": "Item 8.01 Other Events: FDA complete response letter received for ZP-401.",
         "source": "edgar", "source_tier": 1, "symbols": [],
         "channels": ["filing", "form:8-K", "8-K"], "is_new_story": True,
         "independent_outlets": 1},
        {"material": True, "tickers": [], "direction_hint": "down",
         "urgency": "high", "novelty_score": 1.0, "confidence": 0.9,
         "reason": "Regulatory/clinical catalyst class: CRL on a pipeline drug; ticker not stated in filing text."},
    ),
]


def render_item(item: dict, cluster: dict) -> str:
    """The user-turn content: item facts + cluster context, compact JSON.
    Synthetic (sympathy-lane) signals add a 'sympathy' block: A1 judges
    materiality FOR THAT TICKER given the parent item and stated relation."""
    payload = {
        "headline": item.get("headline"),
        "summary": item.get("summary"),
        "source": item.get("source"),
        "source_tier": item.get("source_tier"),
        "symbols": item.get("symbols", []),
        "channels": item.get("channels", []),
        "is_correction": item.get("is_correction", False),
        "is_new_story": cluster.get("is_new_story"),
        "independent_outlets": cluster.get("independent_outlets"),
    }
    if item.get("sympathy"):
        payload["sympathy"] = item["sympathy"]     # {ticker, relation, rationale}
    return json.dumps(payload, ensure_ascii=False)


def build_messages(item: dict, cluster: dict,
                   retry_error: str | None = None) -> list[dict]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for shot_in, shot_out in FEW_SHOT:
        messages.append({"role": "user", "content": json.dumps(shot_in, ensure_ascii=False)})
        messages.append({"role": "assistant", "content": json.dumps(shot_out, ensure_ascii=False)})
    user = render_item(item, cluster)
    if retry_error:
        user += ("\n\nYour previous response was invalid: " + retry_error +
                 "\nRespond again with ONLY a valid JSON object.")
    messages.append({"role": "user", "content": user})
    return messages
