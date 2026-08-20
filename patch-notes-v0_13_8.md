# v0.13.8 — the MRNA/MSTR fixes: the scanner sees all day, spends its budget wisely, and gets a fast lane to the analyst (2026-08-20)

Fixes the five defects behind the 2026-08-19 non-trades (full evidence:
`claude/mrna-mstr-review-2026-08-19.md`). That day MRNA closed **+177%**
and MSTR **+12.4%**; the pipeline traded neither. The scanner found MRNA,
emitted it as its best-scored candidate of the day (0.9554, 48.6× rel-vol)
— and the signal waited **24 minutes** in the analyst queue against a
5-minute gate staleness budget. MSTR was never even looked at: the whole
6/day emission budget was spent between 09:51:36 and 09:54:23, after which
the scanner **went completely blind for the rest of the session**.

Operator decisions folded in: daily escalation cap **6 → 15**, and both
releases (scanner fixes + analyst fast lane) combined into one.

## Fix 1 — the daily cap no longer blinds the scanner (C10)

`scan_once` early-returned the moment `max_per_day` was reached. On 08-19
the last `scanner_candidates` row of the session is 09:54:23; MSTR's
afternoon move has no row of any status — not FILTERED, not CAPPED,
*unseen*. That violates the founding doctrine ("everything C10 sees is
journaled") and starves A9 of exactly the days worth measuring.

Now: cap reached → **OBSERVE mode** (new pure `rules.scan_mode`). The
scanner keeps scanning and journaling; would-be emissions journal
`CAPPED/PER_DAY`; nothing is emitted; the health line reads
`observing — daily cap reached (N/15)`. `observe_after_cap: false`
restores the old behaviour.

## Fix 2 — emission budget is spent on candidates that can actually trade (C10)

On 08-19, half the budget went to scores 0.48–0.55, and two more slots
went to down-movers whose short entry was already impossible — WYFI had no
borrow (three SHORT_UNAVAILABLE vetoes that day) and AXTI was through the
−10% Reg-SHO trigger by gate time. All knowable before spending the slot.

The inline cap ladder in `scan_once` is replaced by one pure function,
`rules.emission_disposition`, checked in this order:

1. **`min_emit_score: 0.60`** — below the floor journals
   `FILTERED/SCORE_FLOOR`, consumes nothing.
2. **Short-side viability** (`precheck_short_availability: true`) — a
   down-mover at/through `ssr_trigger_pct` (−10%) journals
   `FILTERED/SSR_RESTRICTED`; one not easy-to-borrow (same
   `common/assets.py` client and policy bar as C3's gate check, 30-min
   TTL, lookup deferred until the candidate would otherwise emit) journals
   `FILTERED/SHORT_UNAVAILABLE`. Honest cost, documented in config: a
   filtered down-mover also loses its rare long-capitulation path — the
   journaled rows let A9 prove whether that ever matters. No creds / no
   answer → no verdict (C3 still fails closed downstream).
3. The caps: per-scan (2), per-day (**15**, was 6), **per-hour (6, NEW)**,
   concurrent (2) — each journaling its own CAPPED reason.

**`max_per_hour: 6`** is the pacing layer: 08-19's burst spent 6/6 in
2m47s; now a wild open can spend at most 6 in any rolling hour, so budget
survives to the afternoon by construction (the same shape as A4's
late-pass pacing, v0.12.15). Raising the day cap to 15 raises the
*analyst-call* budget only — trade-level protections (2 concurrent scanner
positions, 3-loss breaker, `max_trades_per_day` 5, 0.5× sizing) are all
unchanged.

## Fix 3 — scanner signals claim FIRST at the analyst (A1 → A2)

`queue.claim_next` has always ordered by `(priority, available_ts)` and
the A12 position-touching path has always enqueued at 0 — but scanner
signals went onto `signal.analyst` at the default 100 and waited behind
the entire news backlog. 08-19: all six waited 24–26 minutes; three died
`SCANNER_STALE` on moves that kept running; A2's own model latency was
only ~40s each.

Now `A1.handle_scanner` enqueues at **`router.scanner_analyst_priority:
10`** (config/a1.yaml) — behind position-touching (0), ahead of news
(100). Bounded by construction: at most `max_per_day` one-model-call
signals a day, so the worst case for a news item is a ~minute of extra
wait against its 30-minute evaluation window. A1's own consume loop also
claims `signal.scanner` first (it is empty on almost every iteration; one
indexed no-row query).

## Fix 4 — the queue wait is journaled (A2)

The 24-minute waits had to be reconstructed by subtracting log timestamps.
Every ANALYST decision payload (thesis, no-trade, invalid-output, data
-unavailable) now carries **`queue_wait_secs`** — `enqueued_ts` to claim —
and the `thesis` / `analyst no-trade` log lines print `queue_wait_s`.
This is the number that proves (or refutes) Fix 3 within a week, straight
from GATE-LAB-style queries.

## Fix 5 — cosmetic schema violations stop costing model calls (A2)

Six times in one 40-minute window on 08-19, A2 produced a valid thesis
whose only defect was `"45 minutes"` instead of `"45_minutes"` — each one
burned a full ~30–50s retry on the exact queue starving the scanner lane
(~10% of morning throughput). Two layers:

- `schema.coerce_window` (pure, `mode="before"` validator): normalizes
  case/space/hyphen variants and unit synonyms (`45 min`, `45-Minutes`,
  `2 hrs` → `120_minutes`, `3 days` → `3_sessions`). Anything ambiguous
  (`two_weeks`, `soon`) passes through untouched and fails to the strict
  validator exactly as before.
- The prompt now states the EXACT format in both lanes, with counter-
  examples ("never '60 minutes' or '1_hour'").

## Deliberately NOT in this release

- **A4 late-pass question** (why MRNA's 08:29 item never won a late slot
  despite priority ordering + carryover existing since v0.12.15) — folds
  into the standing A4 capacity review; needs its own diagnosis first.
- **Scanner-lane A2 calibration** (A2 called "fade, conf 0.35" at +107% on
  a stock that closed +177%) — log-and-watch until ≥10 scanner-origin
  cases; the gate-lab data says its fade calls average −2.33% (right).
- **Finding 6** (the MMM fill placed over A3's own "critical
  contradiction" note) — needs one follow-up query before any change.

## Changed files

REPLACED (8):
- `src/c10_scanner/rules.py` — scan_mode, emission_disposition
- `src/c10_scanner/service.py` — observe mode, disposition loop, borrow
  client, `_emitted_last_hour`
- `src/a1_triage/service.py` — scanner enqueue priority; scanner queue
  claimed first
- `src/a2_analyst/service.py` — queue_wait_secs journaled + logged
- `src/a2_analyst/schema.py` — coerce_window + before-validator
- `src/a2_analyst/prompt.py` — exact window format, both lanes
- `config/scanner.yaml` — max_per_day 15, max_per_hour, min_emit_score,
  precheck_short_availability, ssr_trigger_pct, assets_ttl_secs,
  observe_after_cap
- `config/a1.yaml` — router.scanner_analyst_priority

NEW (3):
- `tests/unit/test_v0_13_8.py`
- `patch-notes-v0_13_8.md`, `v0_13_8-deploy-guide.md`

No schema migration: `CAPPED`/`FILTERED` and the new reject reasons fit
migration 007's existing status CHECK (reasons are free text).

## Tests

42 new, all incident-pinned to 08-19 journal numbers. Unit suite expected
**1 failed, 725 passed** (v0.13.7's 683 + 42; the failure is the
pre-existing `test_triage_v047.py::test_confidence_required`).

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.7
sudo systemctl restart a1-triage a2-analyst c10-scanner
```

Which restores: the scanner going blind at the cap, 6 slots spent by
09:54 on a busy open, and 24-minute scalp-signal queue waits. There is no
reason to.
