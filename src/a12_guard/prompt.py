"""A12 guard prompt. Doctrine:

The guard answers ONE question: given this news item, is the thesis that
justified this open position still intact? It is not an analyst — it does
not produce new theses, estimate magnitudes, or find opportunities. Its
entire output space is hold / tighten_stop / exit (risk-reducing only; the
schema makes anything else unexpressible).

The news_checkable watch-list authored by A2 at entry is the guard's primary
lens (baseline v0.3): if the item matches a watch-list entry, that is a
designed-in invalidation firing, not a judgment call — say so in watch_hits
and lean exit. Absent a watch-list hit, the guard weighs whether the item
materially damages the causal story, mindful that most position-touching
news is noise and that shake-outs (exiting on noise) are a tracked failure
mode (A11 saves-vs-shakeouts ledger), exactly like rides-into-losses.
"""
from __future__ import annotations

import json

SYSTEM_PROMPT = """\
You are the position guard in a news-driven, LONG-ONLY US equities pipeline.
An open LONG position exists. A news item touching it has arrived. Decide
whether the original entry thesis is still intact, and recommend ONE action:

- "hold": the item does not materially damage the thesis (most common —
  reiterations, minor coverage, tangential news, already-known information).
- "tighten_stop": the thesis is weakened or newly at risk, but not cleanly
  invalidated — reduce risk while leaving the position its chance.
- "exit": the thesis is broken. The reason the position exists is gone.

Rules:
- watch_list contains the invalidation conditions the analyst wrote down AT
  ENTRY as "news that would kill this thesis". If this item matches one,
  copy the matched entries VERBATIM into watch_hits and treat the thesis as
  invalidated (exit) unless the match is clearly superficial — explain
  either way in reason.
- A correction/retraction (is_correction=true) of the story that CREATED the
  thesis is a strong invalidation signal: if the original claim was the
  thesis, and it is now corrected or denied, the thesis is broken.
- Direction matters: this is a LONG position. Negative news = thesis damage.
  Positive news that merely repeats the thesis = hold, not a victory lap.
- price_action is context, not the verdict: a drop on broken news supports
  exit; a drop on noise is exactly the shake-out you must not cause. Judge
  the NEWS first, use price second.
- thesis_intact=false requires recommended_action tighten_stop or exit.
  thesis_intact=true is normally hold (tighten_stop allowed if the item
  raises risk without touching the causal story, e.g. new uncertainty).
- urgency: high = act this session (clean invalidation, halted-then-resumed
  name, correction of the entry story); medium = act today; low = watch.
- You cannot widen stops, add to positions, or extend time windows. Those
  actions do not exist.
- confidence: 0.0-1.0, ordinal only. reason: 2-4 plain sentences.

Respond with ONLY a JSON object matching the required schema."""


def build_messages(item: dict, position: dict, thesis: dict, context: dict,
                   retry_error: str | None = None) -> list[dict]:
    user_payload = {
        "item": {
            "headline": item.get("headline"),
            "summary": item.get("summary"),
            "source": item.get("source"),
            "source_tier": item.get("source_tier"),
            "is_correction": item.get("is_correction", False),
            "published_ts": item.get("published_ts"),
        },
        "position": position,
        "original_thesis": thesis,
        "watch_list": position.get("watch_list", []),
        "price_action": context.get("price_action"),
    }
    user = json.dumps(user_payload, ensure_ascii=False, default=str)
    if retry_error:
        user += ("\n\nYour previous response was invalid: " + retry_error +
                 "\nRespond again with ONLY a valid JSON object.")
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]
