# v0.11.5 — Stop the sympathy-lane analysis loop (multi-ticker stories)

## Symptom

On 2026-07-20 a single Benzinga article — "QUICK SPARK: Taco Bell, Cava,
Chipotle Visits Plunge on Food-Borne Parasite" (`alpaca:60560127`, symbols
`CAVA, CMG, YUM`) — was sent to the analyst (A2) roughly once a minute for
hours. The journal showed the same `item_id` at revision 1 producing a THESIS,
getting a `LONG_ONLY` gate veto, and then re-analyzing again ~50–70s later —
YUM alone 90+ times in six hours. The analyst queue backed up into the
thousands, delaying every legitimate signal behind it.

This is **not** the story-level duplication that v0.4.7 fixed. C1/C2 dedup and
A1 repeat-suppression were all working (566 `SUPPRESS` rows that day). This is
a distinct failure mode in the **sympathy / synthetic lane**.

## Root cause

The article names three tradeable tickers, all hit by the same bearish event.
The sympathy lane had no depth limit, so they analyzed each other in a loop:

1. `A2Service.handle()` (src/a2_analyst/service.py) enqueues one
   `signal.synthetic` per `related_opportunity` on **every** thesis — including
   theses that are themselves synthetic-derived. No depth cap.
2. `A1Service.handle_synthetic()` re-triages the parent item for the sympathy
   ticker and routes it back to `signal.analyst` **without** the repeat
   suppression that the primary path (`handle()`) applies — synthetic decisions
   are deliberately excluded from story suppression.

So: A2 analyzes YUM → down thesis → gate veto (LONG_ONLY) → fan out synthetic
CAVA + CMG → A1 triages them (no suppression) → A2 analyzes CAVA → fan out YUM
+ CMG → … forever. The `LONG_ONLY` veto correctly blocks the *trade*, but never
stops the *analysis* fan-out, and every generation spawns the next
(`derived_from` climbing 7635 → 7642 → 7646 → 7650 → …).

## Fix

**One change, one file: a depth cap on the sympathy fan-out**
(`src/a2_analyst/service.py`).

Only a **primary** thesis — one derived directly from a real news item — may
spawn synthetic sympathy signals. A synthetic-derived thesis does its analysis,
journals, and gates as before, but does **not** fan out again. The fan-out loop
is now wrapped in `if derived_from is None:`. `derived_from` is `None` on
primary analyst signals and is stamped with the parent decision id on
synthetic-origin ones by `A1.handle_synthetic()`, so it is exactly the depth
guard needed.

This makes sympathy **one hop deep from real news** — which is the design
intent all along ("one signal.synthetic per related opportunity" from a
real-news thesis, spec §10). A three-ticker story now costs at most three
analyses (the primary plus one per sibling), each once, instead of an unbounded
cascade. The log line's `synthetics=` count now also reflects what was actually
enqueued (0 on synthetic-derived theses).

Deliberately **not** changed: down-direction theses still fan out from a
primary (a bearish story on one name can be a legitimate bullish sympathy play
on a competitor — direction is not the discriminator, depth is). And chained /
second-order sympathy is intentionally out of scope; if it's ever wanted it
needs explicit cycle detection, not accidental unbounded recursion.

## Regression test

`tests/integration/test_analyst_gate_flow.py` — new `test_09`: a
synthetic-derived thesis with a `related_opportunity` is processed by A2; it
asserts the THESIS is still journaled and gated, but that **no** second-
generation `signal.synthetic` message is enqueued. Paired with the existing
`test_07` (which proves a *primary* thesis still fans out), the two together
pin the behavior so this loop can't come back silently.

## Changed files

- `src/a2_analyst/service.py` — sympathy fan-out gated on `derived_from is
  None`; log `synthetics=` count made accurate. (REPLACED)
- `tests/integration/test_analyst_gate_flow.py` — added `test_09`. (REPLACED)

## What this does NOT touch

No database, no schema, no config, no models, no other service. Only
`a2-analyst` runs the changed code, so only `a2-analyst` needs restarting.

## Rollback

`git checkout v0.11.4` on the Spark and restart `a2-analyst`. Nothing else to
undo.
