# v0.12.28 — the four WDAY fixes (2026-08-13)

## Why

WDAY, 14:36 ET: Reuters reports Silver Lake in talks to buy Workday. The
stock goes 186.27 → ~220 in ten minutes, is halted three times on LULD
circuit breakers, and closes 207 (+19% on the day, its best in a decade).

**The intelligence layer was flawless.** A1 classified it in 30 seconds
("M&A catalyst class: credible takeover talks with a named potential
acquirer", conf 0.95). A2 wrote a correct long thesis 43 seconds later
*while the stock was still at its pre-news price*, explicitly naming the
entry ahead of the move. Nothing downstream could use any of it.

Full evidence trail: `claude/wday-veto-review-2026-08-13.md`.

Three independent causes, each sufficient on its own. None was a bug —
all three were design limits that had been in place since Phase 3 and only
became visible when a story large enough lit them all at once.

## Fix 1 — corroboration that could actually be earned

The veto was `CREDIBILITY`: `independent_outlets: 1`, `required: 2`.

The expected culprit was the carried Boeing/Leidos undercounting item. It
wasn't. Cluster 22084 held four items and **all four came from
`alpaca_benzinga`**. The count was right.

That is the finding. `alpaca_benzinga` is our only journalistic feed —
EDGAR is filings, RSS is PR wires and macro. For a story a reporter breaks
rather than a company announcing itself, `independent_outlets` is **pinned
at 1 forever**. Against `required_outlets: high: {2: 2}`, every
media-reported catalyst with `magnitude_est ≥ 0.05` was unreachable *by
arithmetic*. Not rarely — never. It produced the same journal rows as a
discriminating rule, which is why it hid for a month behind the largest
veto bucket on the board (28 of 53 gate vetoes on 2026-08-13 alone).

There was a perverse edge too: A2's honest 8% premium estimate is what
crossed `impact_high_min` into the strict bucket. A 4% estimate would have
**passed**.

**`gate.cluster_growth`** adds a capped effective-outlet credit for a story
cluster *growing*. The justification is what cluster membership already
means: C2 drops anything ≥0.9 cosine to a seen item, so a member that
*joins* a cluster is necessarily a substantively different article about
the same story (WDAY's follow-ons scored 0.810 and 0.866). One wire filing
three distinct write-ups is real evidence a story isn't fabricated — just
not *independent-outlet* evidence, so it is credited as less:

```yaml
cluster_growth:
  enabled: true
  items_per_credit: 1     # distinct items (by item_id — revisions excluded)
  max_credit: 1           # hard cap
```

`effective_outlets = independent_outlets + credit` is what the matrix now
tests, and every number is journaled. **The baseline invariant survives by
arithmetic, not by promise:** Tier-3 single-source high-impact requires 3
and can reach at most 2, so it still never passes alone. A lone item with
no cluster growth is unchanged. Pinned in tests both ways.

## Fix 2 — the maturity deferral stopped eating the entry window

Even with corroboration credited, the trade was already dead three checks
later. C3 deferred at 14:37:19 to `mature_ts` 14:40:00 (`min_confirm_bars:
3`). At its **first legal look**, `pct_move` was **7.39%** — past
`extended_pct: 0.06` and unreachable forever. The band where this trade was
takeable (≥1.5%, <6%) opened and closed entirely inside the deferral.

`confirm_bars_for()` lets high-urgency catalysts mature on
**`fast_confirm_bars: 1`** instead of 3 — 14:38:00 rather than 14:40:00 on
the WDAY clock. A1's `urgency` now rides through A2's gate body to make
this decision (additive, nullable; a message without the key behaves
exactly as before).

**This is not a rollback of v0.11.10.** The floor is one *completed* bar,
never zero, so the window still physically contains data and
MARKETDATA_MISSING remains the thing that fix was built to prevent. The
fast path is also bounded above by `min_confirm_bars` — it can only ever
shorten the wait.

## Fix 3 — the re-check loop abandons a state it cannot win

C3 re-checked WDAY **nine times over 25 minutes**, watching `pct_move`
climb to 18.38% and fall back to 12.41%, then journaled `CREDIBILITY` at
minute 29. By then `GATE_EXTENDED` had been the operative blocker since
minute 4 — nothing the loop was waiting for could have produced a PASS.

`abandon_recheck()` ends the loop once `pct_move ≥ extended_pct` and
journals **`GATE_EXTENDED`**, carrying the superseded code in
`payload.masked_reason` and setting `abandoned_recheck: true`. The tuning
dataset stops attributing these to credibility, which matters because
that bucket is exactly what Fix 1 is meant to be measured against.

## Fix 4 — stale bars can no longer manufacture an entry

Consecutive re-checks three minutes apart returned byte-identical
`pct_move` **and** `vol_mult` (0.07392/34.3, then 0.18377/60.48) on a stock
printing 60× relative volume. That is a cache, not a quiet tape.

`MarketState.bar_age_secs` is now measured from the newest since-window bar
and journaled on every verdict. A verdict that would otherwise PASS but
rests on a bar older than **`max_bar_age_secs: 120`** returns the new
re-checkable veto `STALE_MARKETDATA`. Deliberately last in the check order:
staleness can block an entry, never create one. Unknown age (`None`)
disables the check rather than guessing.

## Fix 5 — the scanner can see large caps, and gets vetoed names back

**Zero `scanner_candidates` rows for WDAY.** Not `SUPPRESSED_NEWS`, not
`FILTERED` — never evaluated. Meanwhile the lane spent its whole session on
warrants (`BBBY.WS`, `DAVEW`, `KWMWW`…), preferreds (`AHT.PRD/PRG/PRH/PRI`)
and leveraged ETFs. Four market-data calls each, to reach a guaranteed
`PRICE_FLOOR` reject. The only real common stocks it looked at all day were
MU, IREN, ACHR, BYND, HTZ, PATH and OPEN.

Three changes:

- **`most_actives_by: [volume, trades]`.** `volume` ranks by *share count*,
  which structurally favours cheap stock — WDAY's 8.75M shares was ~$1.8bn
  and nowhere near a top-50 by shares. `trades` ranks by trade count, which
  is price-neutral and is where a high-priced large cap in a news explosion
  actually appears. One leg per ranking; a failing leg is skipped, never
  fatal.
- **`movers_top: 50`** (was hard-coded 20 — the config comment has admitted
  since v0.12.17 that 20 slots are all microcap spikes).
- **`exclude_derivative_shapes`.** `looks_like_derivative()` drops warrants,
  rights, units and preferreds on symbol shape *before any I/O* — dotted
  CQS suffixes plus the Nasdaq five-character W/R/U convention. Four-letter
  tickers are never matched; a false positive costs a real trade, a false
  negative costs only the measurement we already pay.

And the structural one: **`news_owns_it_until_veto`.** The news lane
escalated WDAY at 14:36 and vetoed it at 15:05. For that whole half hour
the scanner would have stood down `NEWS_OWNS_IT` for a lane that was in the
process of declining the trade — and after the veto, no lane owned it at
all. Both behaved correctly on their own terms and the combination traded
nothing. An escalation now counts as ownership only while it is still
*alive*; a terminal GATE VETO hands the name back, and the scanner judges
it on its own tape-based merits (it may still reject it — `SCANNER_STALE`
and `SCANNER_PARABOLIC` exist for exactly this shape).

## Would this have caught WDAY?

**Possibly. Not certainly, and the honest answer matters more than a
comfortable one.** All three of Fix 1, 2 and 5 had to land for any path to
exist:

- 14:38:00 (Fix 2) — cluster at 2 items, credit 1, effective outlets 2,
  credibility satisfied (Fix 1). `vol_mult` was ~34× against a 2.5×
  requirement. It comes down to whether `pct_move` was still under 6% at
  14:38; we know it was 7.39% at 14:40:07 and ~0% at 14:37:16. **That is a
  coin flip, and the counterfactual data cannot resolve it.**
- The scanner path (Fix 5) is the more robust one: at 14:40 WDAY was +7%
  with the move still running, above VWAP, on 34× relative volume — the
  `scalp_v1` setup exactly, in the lane built for *remaining* move rather
  than the move already made.

Keep the counterfactual in view: entering at the 15:05 veto price would
have **lost** (`max_up +0.145%`, `max_down −3.161%`, eod 207.00). This
trade existed between roughly 14:40 and 14:50 and nowhere else. And three
LULD halts on an unconfirmed rumour is also the profile that round-trips
−15% on a denial. These fixes buy a *shot* at this class of setup; they do
not promise this outcome.

## Files

REPLACED (8): `config/gate.yaml`, `config/scanner.yaml`,
`src/c3_gate/rules.py`, `src/c3_gate/service.py`,
`src/a2_analyst/service.py`, `src/c10_scanner/rules.py`,
`src/c10_scanner/screener.py`, `src/c10_scanner/service.py`,
plus `tests/unit/test_gate_recheck.py` (the RECHECKABLE_VETOES pin,
deliberately extended).

NEW (3): `tests/unit/test_wday_v0_12_28.py`, `patch-notes-v0_12_28.md`,
`v0_12_28-deploy-guide.md`.

**12 changed files**, pyproject pencil edit to `0.12.28`.
**No migration** — `scanner_candidates.reject_reason` and
`decisions.veto_reason` are unconstrained TEXT, verified against the DDL,
so `INSTRUMENT_SHAPE` and `STALE_MARKETDATA` need no vocabulary change.
No new timers. No dashboard change (veto codes render generically — checked
after the v0.12.27 originChip lesson).

## Tests

**54 new**, every one pinned to a real number from the 2026-08-13 journal
so a future refactor that restores the old behaviour fails with the
incident in the message: the single-item veto still fires; the second
article satisfies credibility; the credit is capped at 1 (2, 4 and 40
items all give 1); Tier-3 single-source high-impact still never passes;
disabling the credit restores v0.12.27 exactly; the EH shadow branch shares
the credit; the literal WDAY maturity clock (14:40:00 vs 14:38:00); the
fast path can neither reach zero bars nor exceed `min_confirm_bars` and is
case-insensitive and disableable; abandon fires at 0.07392 and 0.18377 but
not 0.059 or `None`; a stale bar blocks a PASS but never creates one;
every derivative symbol from that day's scan log is rejected and every real
ticker survives; `merge_universe` unions N legs without duplicates and is
back-compatible at one leg.

Suite: **572 passed**, 2 failed — both pre-existing and confirmed identical
on a clean `v0.12.27` checkout (`test_cik_map` env, `test_triage_v047`
date-drift, already on the housekeeping list).

## Watch (first session after deploy)

1. `veto_reason` mix — CREDIBILITY should fall from its 28/day perch.
   If it collapses to near zero, `items_per_credit: 1` is too generous;
   raise it to 2 before touching anything else.
2. `GATE_EXTENDED` count should RISE — that is Fix 3 telling the truth
   about vetoes previously mislabelled, not a new problem.
3. `journal.scanner_candidates`: real large-cap tickers appearing, and
   `INSTRUMENT_SHAPE` absorbing what used to be PRICE_FLOOR/DOLLAR_VOLUME.
4. Any `STALE_MARKETDATA` row is a live market-data lead — the 3-minute
   cache is still unexplained and this veto is how we find it.
5. Counterfactuals on everything above, per the standing §14 discipline.
   **Do not tune any threshold from a single day, including this one.**

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.27
sudo systemctl restart c3-gate a2-analyst c10-scanner
```

Or leave the code and disable any fix individually in config — each is
behind its own switch: `cluster_growth.enabled: false`, `fast_urgency: []`,
`abandon_when_extended: false`, `max_bar_age_secs: 0`,
`most_actives_by: [volume]`, `exclude_derivative_shapes: false`,
`news_owns_it_until_veto: false`.
