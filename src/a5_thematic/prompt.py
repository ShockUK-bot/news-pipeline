"""A5 prompt. Doctrine: A5 is the system's LONG memory — it turns the
no-ticker / long-horizon news lane into a persistent store of standing
theses with dated evidence. It never trades, sizes, or ranks entries; the
store's only trading influence is indirect (router thesis-match facts, A2
context, A6 staleness review, the A8 briefing).

v0.12.23 — TWO MODES. The Phase-8 prompt was written for a healthy store:
"most nights should attach evidence or ignore; new theses are rare". With
an EMPTY store that instruction is a deadlock — "attach evidence to an
existing thesis" is impossible when none exist, so the only remaining ops
are the-rare-thing and ignore, and the model correctly picks ignore every
time. (Live evidence: 113 items in, 113 IGNOREs out, 0 theses, three
weeks. See claude/diagnose-thesis-store-2026-08-10.md.)

So the mode is chosen by CODE from the store's actual population:
  BOOTSTRAP (active theses < store.bootstrap_min_theses) — seed the store;
             returning zero new theses is explicitly called a failure.
  STEADY    (at or above the floor) — the original conservative wording,
             unchanged.
The flip is automatic in both directions: seed the store and it reverts to
conservative; expire back below the floor and it re-seeds.
"""
from __future__ import annotations

import json

# --------------------------------------------------------------------------
# shared core — identical in both modes
# --------------------------------------------------------------------------

_CORE = """\
You are the macro/thematic analyst in a news-driven, LONG-ONLY US equities
pipeline. You maintain the persistent THESIS STORE: durable, weeks-to-months
structural stories (drivers), each with beneficiary tickers and a dated
evidence log. Tonight you receive (a) the current ACTIVE theses, (b) fresh
news items, and (c) — on deep passes — a wider week-in-review context pack.

For each input item choose exactly one:
- attach it as "evidence" to ONE existing thesis (thesis_id from the list;
  polarity: supports / contradicts / neutral; note: one line on why), OR
- seed a NEW thesis (put it in new_theses with anchor_item_id = the item;
  do not also list it in items), OR
- "ignore" it (commentary, one-off events, noise).

A new thesis needs:
- driver: the causal mechanism in 1-3 sentences (what structurally changed,
  why it persists for weeks+). "Stocks went up" is not a driver.
- beneficiaries: 1-5 LONG-side tickers with the relation and a one-line
  rationale each. Only liquid US-listed names. No ETFs unless the theme has
  no pure-play equity.
- invalidation: 1-4 news-checkable conditions that would kill the thesis.
- confidence: honest 0-1.

reviews: for existing theses whose picture changed tonight — confidence up
or down ("keep" + new confidence), "invalidate" (an invalidation condition
was met — cite it in the note), or "realized" (the repricing has happened).
Do NOT propose expiry for mere quietness; staleness expiry is automatic.

Rules:
- Use only thesis_ids and item_ids that appear in the input. Never invent
  identifiers, tickers, or events.
- Contradicting evidence is valuable — log it with polarity "contradicts"
  rather than ignoring it.
- Every "ignore" MUST carry a short note saying why (2-8 words, e.g.
  "single-company earnings beat", "no durable driver", "already covered by
  th-2026-004"). An ignore with an empty note is not acceptable — the
  operator reads these to tune this prompt.
- summary: 2-4 sentences on tonight's thematic picture for the operator.

Respond with ONLY a JSON object matching the required schema."""

# --------------------------------------------------------------------------
# mode blocks
# --------------------------------------------------------------------------

_STEADY_BLOCK = """\

STORE STATUS: POPULATED ({n_active} active theses).

New theses are RARE (most nights: zero or one) and must name a causal
driver, not a headline echo. Most items tonight should attach as evidence
to an existing thesis, or be ignored. New theses rarely deserve confidence
above 0.6."""

_BOOTSTRAP_BLOCK = """\

STORE STATUS: BOOTSTRAP — the store holds only {n_active} active thes{plural}.
Seeding it is your PRIMARY TASK tonight.

Propose {target} new standing theses from the material below. This
instruction OVERRIDES any "new theses are rare" discipline you might
otherwise apply: that rule exists to stop a healthy store filling with
noise, and it does not apply to an empty one. "Attach as evidence to an
existing thesis" is mostly unavailable to you tonight because there are
barely any theses to attach to — that is the situation you are here to fix.
**Returning zero new theses tonight is a FAILURE, not caution.**

What you are looking for is the durable structural story UNDERNEATH the
individual headlines. Productive categories:
- capital-spending cycles (datacenter build-out, grid, fabs, defence
  procurement, reshoring)
- regulatory and policy shifts with a multi-quarter runway
- supply/demand imbalances in a physical input (power, copper, uranium,
  shipping, memory, rare earths)
- rate, inflation and credit regime changes
- technology adoption curves crossing a commercial threshold
- geopolitics: sanctions, tariffs, conflict, supply-route risk

Several unrelated headlines pointing at the SAME underlying driver is
exactly the signal you want — that convergence is stronger evidence than
any single dramatic headline. A thesis does not need a spectacular anchor
item; anchor it on whichever input item best evidences the driver.

Aim for {target} well-SEPARATED theses on different drivers rather than
several angles on one story. Confidence 0.3-0.6 is right for a freshly
seeded thesis — you are not being asked to be certain, you are being asked
to be specific and falsifiable. A thesis you would be embarrassed to be
wrong about in six weeks is better than no thesis at all, because the
invalidation conditions you write will kill it cheaply if it is wrong."""


def system_prompt(n_active: int, bootstrap: bool, target: int) -> str:
    """Mode-selected system prompt. Pure — code decides the mode."""
    if bootstrap:
        block = _BOOTSTRAP_BLOCK.format(
            n_active=n_active, target=target,
            plural="is" if n_active == 1 else "es")
    else:
        block = _STEADY_BLOCK.format(n_active=n_active)
    return _CORE + "\n" + block


def build_messages(theses: list[dict], items: list[dict], deep: bool,
                   retry_error: str | None = None, *,
                   bootstrap: bool = False, bootstrap_target: int = 5,
                   context: dict | None = None) -> list[dict]:
    payload: dict = {
        "mode": "sunday_deep_pass" if deep else "nightly",
        "store_status": "bootstrap" if bootstrap else "populated",
        "active_theses": theses,
        "fresh_items": items,
    }
    if context:
        payload["week_in_review"] = context
    user = json.dumps(payload, ensure_ascii=False, default=str)
    if deep:
        user += ("\n\nDeep pass: also re-examine EVERY active thesis above "
                 "against the week's evidence balance — move confidences "
                 "that have drifted, and invalidate/realize where the story "
                 "has resolved.")
    if bootstrap:
        user += (f"\n\nReminder: the store has {len(theses)} active theses. "
                 f"Seed {bootstrap_target} new ones from this material. "
                 "An empty new_theses list is a failed run.")
    if retry_error:
        user += ("\n\nYour previous response was invalid: " + retry_error +
                 "\nRespond again with ONLY a valid JSON object.")
    return [{"role": "system",
             "content": system_prompt(len(theses), bootstrap,
                                      bootstrap_target)},
            {"role": "user", "content": user}]
