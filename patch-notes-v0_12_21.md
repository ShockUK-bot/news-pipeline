# v0.12.21 — stale recommendations dropped from the morning briefing (2026-08-09)

## The bug (operator-reported)

Every morning briefing since Aug 3 recommended closing JNJ. JNJ closed
on Aug 3 (TIME-stop exit, position 5); the journal, reconcile loop, and
broker all agreed the book was flat. The briefing was wrong anyway.

## Root cause

A8's `a6_section` surfaces the LATEST A6 nightly-review row with no
relevance check. The latest-row design is deliberate (a Monday briefing
should show Friday's review) — but A6 only writes REVIEW rows while
positions are open, so once the book goes flat the "latest review" is
frozen at the last night a position existed, recommendations included.
The plain-text fact sheet was actually safe (it joins recos to open
positions), but the subject line counted the stale reco and the
narrative model received it in the facts JSON — and led the summary
with it, presenting days-old advice as current.

## Fix

`a6_section` now takes the current open book's position_ids and filters
the review's recos to positions still open, adjusting the
recommendations count to match. The dropped count is recorded as
`stale_recos_dropped` so the fact sheet stays honest. Review STATS
remain (labeled with run_date) — history is fine, stale advice is not.
When the book is flat, recommendation count is 0, subject line stops
counting phantom recos, and the narrator has nothing stale to lead with.

Not touched, on purpose: the EOD sheet (an explicitly historical recap,
date-labeled) and the latest-row lookup itself (the Monday-shows-Friday
behavior stays for open positions).

## Files

REPLACED (1): `src/a8_briefing/facts.py`.
NEW (3): `tests/unit/test_a8_stale_recos.py`, these patch notes, the
deploy guide.
Plus the pencil edit: `pyproject.toml` version → `0.12.21`.

No migration. **No service restarts** — A8 is a oneshot timer; the next
morning run (Monday pre-open) loads the fixed code automatically.

## Tests

Release set **89 green**: 4 new (the exact flat-book JNJ shape; open
position keeps its reco; mixed book filters only closed; no-review
stays None) + the existing A8 suite + the v0.12.20 set.

## Verification

Monday's morning email: no JNJ mention, subject line shows 0 position
recos, and if you check the journaled facts:

```sql
SELECT payload->'facts'->'a6'->'review'->>'stale_recos_dropped'
FROM journal.decisions
WHERE stage='SYSTEM' AND agent='A8' AND action='BRIEFING'
ORDER BY ts DESC LIMIT 1;
```

should show `1` (the JNJ reco being dropped) until a new position opens
and A6 writes a fresh review.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.20
```
(no restarts either way)
