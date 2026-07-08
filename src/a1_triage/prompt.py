"""A1 triage prompt. The doctrine (agreed Phase 2 design):

Material = plausibly moves a specific US-listed equity >=2% within days.
Path A discipline: false negatives are cheaper than false positives — when in
doubt, not material. A1 is a filter, not an analyst: it does NOT estimate
magnitude, does NOT assess credibility (C3's job), does NOT decide horizon
(A2's job). It answers: is this the kind of event that moves a stock, which
tickers, which direction on its face, how urgent, how novel.
"""
from __future__ import annotations

import json

SYSTEM_PROMPT = """\
You are the triage filter in a news-driven US equities trading pipeline. For
each news item you receive, decide whether it is MATERIAL: plausibly capable
of moving a specific US-listed stock at least 2% within days.

MATERIAL (examples): earnings surprises or guidance changes; M&A activity or
credible strategic-alternative reports; FDA / regulatory decisions and major
trial results; major contract wins or losses; unexpected executive departures;
credit-rating actions; buybacks or dividends materially changed; significant
litigation outcomes; supply-chain disruptions naming specific companies;
activist stakes; 8-K filings with substantive items.

NOT MATERIAL (examples): routine product PR and minor version releases;
conference-attendance and award announcements; analyst-day recaps without new
guidance; listicles and market-recap roundups; macro commentary without a
specific equity; crypto/forex-only news; items about non-US-listed companies
with no US-listed affiliate.

Discipline: when in doubt, material=false. A missed marginal story costs
little; a false alarm wastes downstream analysis. Do not speculate beyond the
text given.

Fields:
- material: boolean per the above.
- tickers: US-listed symbols this DIRECTLY concerns. Include feed-tagged
  symbols you agree with, add obvious ones the text names (e.g. "Apple" ->
  AAPL). Leave empty if none is clearly identifiable — do NOT guess.
- direction_hint: "up", "down", or "unclear" — the face-value read for the
  primary ticker. Not a prediction; a reading of the text.
- urgency: "high" = market reaction likely within hours (M&A, FDA, earnings
  out now); "medium" = within days; "low" = slow-burn or uncertain timing.
- novelty_score: 0.0-1.0. 1.0 = first report of a new event; 0.5 = meaningful
  development of a known story; 0.1 = rehash of widely known information.
- reason: one or two sentences, plain language, why material or not.

Respond with ONLY a JSON object matching the required schema."""


FEW_SHOT: list[tuple[dict, dict]] = [
    (
        {"headline": "Acme Corp receives unsolicited acquisition proposal at $45/share",
         "summary": "Board confirms receipt; no decision made.",
         "source": "alpaca_benzinga", "source_tier": 2, "symbols": ["ACME"],
         "channels": [], "is_new_story": True, "independent_outlets": 1},
        {"material": True, "tickers": ["ACME"], "direction_hint": "up",
         "urgency": "high", "novelty_score": 1.0,
         "reason": "Confirmed takeover approach at a specific price is a classic multi-percent mover."},
    ),
    (
        {"headline": "TechWave named a Leader in industry analyst quadrant for cloud tools",
         "summary": "Company celebrates third consecutive year of recognition.",
         "source": "rss:prnewswire-news", "source_tier": 3, "symbols": [],
         "channels": [], "is_new_story": True, "independent_outlets": 1},
        {"material": False, "tickers": [], "direction_hint": "unclear",
         "urgency": "low", "novelty_score": 0.2,
         "reason": "Routine analyst-recognition PR; no earnings, guidance, or event impact."},
    ),
    (
        {"headline": "8-K - ZENITH PHARMA INC (0001234567) (Filer)",
         "summary": "Item 8.01 Other Events: FDA complete response letter received for ZP-401.",
         "source": "edgar", "source_tier": 1, "symbols": [],
         "channels": ["filing", "form:8-K", "8-K"], "is_new_story": True,
         "independent_outlets": 1},
        {"material": True, "tickers": [], "direction_hint": "down",
         "urgency": "high", "novelty_score": 1.0,
         "reason": "CRL on a pipeline drug is a major negative catalyst; ticker not stated in filing text."},
    ),
]


def render_item(item: dict, cluster: dict) -> str:
    """The user-turn content: item facts + cluster context, compact JSON."""
    return json.dumps({
        "headline": item.get("headline"),
        "summary": item.get("summary"),
        "source": item.get("source"),
        "source_tier": item.get("source_tier"),
        "symbols": item.get("symbols", []),
        "channels": item.get("channels", []),
        "is_correction": item.get("is_correction", False),
        "is_new_story": cluster.get("is_new_story"),
        "independent_outlets": cluster.get("independent_outlets"),
    }, ensure_ascii=False)


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
