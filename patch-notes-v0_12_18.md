# v0.12.18 — triage catalyst rescue + dedup cooldown (2026-08-07)

## The other half of the SPCX miss

v0.12.17 fixed the scanner lane. This release fixes the news lane, which
independently threw away every SPCX story across two days (12/12 triage
decisions DISCARD/SUPPRESS, zero escalations from 40+ items). Two defects:

### Defect 1 — the catalyst arrived inside a price-reaction story and died

A1's negative category 2 ("price-action commentary") is correct doctrine —
"shares hit 52-week high" is not a catalyst. But on catalyst days the
catalyst arrives WRAPPED in the reaction story: *"SpaceX shares are
trading higher. The company's lockup expired today"* was discarded as
price-action commentary, with a 911M-share lock-up expiry inside it.
Rating changes had the same soft spot: the taxonomy only said an upgrade
"MAY be material."

**Fix (prompt):** two explicit MATERIAL classes — share lock-up
expirations taking effect now (extending capital-structure, alongside
index inclusion/exclusion) and actual analyst rating changes (upgrade/
downgrade from a named firm; maintains/PT-only/initiations stay negative
category 1). Negative category 2 gains an explicit EXCEPTION: an item
naming a concrete catalyst from the material classes is classified by
that catalyst — price-action framing does not neutralize it. Two new
few-shot examples pin both shapes (a lockup-expiry reaction story →
material; a true upgrade → material). The v0.4.7 anti-passthrough
doctrine (the 79.6%-escalate incident) is untouched and its phrases are
now pinned by tests.

### Defect 2 — a discarded cluster was a 24-hour blackhole

Story-level suppression (v0.4.7) treats any prior verdict as final for
24 hours. That's right for ESCALATE (an analyst saw the story; reprints
add nothing) but wrong for DISCARD: Friday's *"shares trading higher
after Argus upgrade"* item was auto-SUPPRESSed into a cluster discarded
long before — a NEW catalyst inherited an old verdict without any model
ever seeing it.

**Fix (code):** asymmetric windows. DISCARD priors now suppress
follow-ups only within `suppression.discard_window_hours` (default 2) —
enough to flood-control wire reprints, which arrive minutes apart —
after which follow-ups get a fresh triage. ESCALATE priors keep the full
24h window. Pure predicate (`discard_cooldown_expired`), unit-tested;
the existing bypasses (corrections, corroboration threshold, held-name
touch) are unchanged and still checked.

## Cost and risk

- Prompt change is model-behavior: expect a modest rise in escalations
  (rating changes ~a handful/day on liquid names; lockup/index events
  rare). A2 absorbs these at ~40s each; C3 still gates everything.
  Watch the escalate share for a few days (query in the deploy guide) —
  if it climbs toward the bad old days (>40%), tell Claude.
- Dedup change adds re-triages for stale-discarded stories: A1-only
  (8B, resident), a few dozen extra calls/day at worst.

## Files

REPLACED (4): `src/a1_triage/prompt.py`, `src/a1_triage/suppression.py`,
`src/a1_triage/service.py`, `config/a1.yaml`.
NEW (3): `tests/unit/test_a1_catalyst_rescue.py`, these patch notes, the
deploy guide.
Plus a one-line pencil edit: `pyproject.toml` version → `0.12.18`.

No migration. One service restart: `a1-triage`.

## Tests

Release set **78 green** — the new `test_a1_catalyst_rescue.py` (prompt
taxonomy pins incl. doctrine-preservation, suppression window asymmetry)
plus test_triage_router, test_scanner, test_scanner_etf_guard (covers
v0.12.17 as well when deployed together).

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.17
sudo systemctl restart a1-triage
```
