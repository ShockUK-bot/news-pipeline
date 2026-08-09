# v0.12.20 — pre-news exit reference repair (2026-08-09)

## The dormant bug, closed before it wakes up

Position 5 (JNJ, 2026-07-24) journaled `ARM FAILED: MIPError(
'UNRESOLVABLE_REF: prenews_price')` on every exit-engine pass: the
`close_below_prenews` machine invalidation — the exit that flattens a
news trade when price gives back the entire news move — could never arm
on a live position. It has been carried as "dormant, book flat" since
July 24. With this weekend's three releases aimed at making the pipeline
find trades again, it was about to stop being dormant.

## Root cause

The exit DSL resolves `prenews_price` from the position's exit_policy.
The live entry flow never put it there:

- C3's gate PASS snapshot carried prices, spread, ATR — but not the
  pre-news reference the gate itself had just computed;
- A3's `materialize_exit_policy` never wrote a `prenews_price` key.

Integration tests seeded `policy["prenews_price"]` directly, so the gap
was invisible to the suite — a textbook latent-live-path bug (the
2026-07-24 post-mortem's own warning category).

## Fix — thread the reference end to end

1. **C3:** both PASS snapshots now carry `prenews_price` — news lane
   uses the gate's computed pre-news reference (last minute-bar close
   before publish, else prev close); scanner lane uses the detection
   snapshot's prev close (the same convention A2's context already
   uses).
2. **A3:** `materialize_exit_policy` accepts and writes
   `prenews_price` into exit_policy. If the snapshot somehow lacks it,
   the key is omitted and behavior degrades exactly as before (that one
   arm fails, everything else arms) — no new failure mode.
3. **C4:** no change needed — the engine already reads
   `policy["prenews_price"]`; it was just never fed.

## Files

REPLACED (2): `src/c3_gate/service.py`, `src/a3_risk/service.py`.
NEW (3): `tests/unit/test_prenews_exit_ref.py`, these patch notes, the
deploy guide.
Plus the pencil edit: `pyproject.toml` version → `0.12.20`.

No migration. Two service restarts: `c3-gate`, `a3-risk`. (c4-exec
needs no restart — its code is untouched.)

## Tests

Release set **81 green**: 5 new (policy carries/omits the reference;
existing fields unchanged; the arm compiles with it and still raises
without it — pinning the exact failure mode) + risk/exec + all three
gate suites, confirming the snapshot change breaks nothing downstream.

## Verification note

The proof arrives with the NEXT opened position: its
INVALIDATION_ARMED events must show `close_below_prenews` compiled with
a concrete price, and zero `ARM FAILED` lines. Query in the deploy
guide Part 6.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.19
sudo systemctl restart c3-gate a3-risk
```
