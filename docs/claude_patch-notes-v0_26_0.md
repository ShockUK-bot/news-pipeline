# Patch notes v0.26.0 (2026-09-22, 21:40 CT): profit lock and price confirmed re-entry

Operator direction (2026-09-22 evening): lock in gains once a trade is positive; re-enter a name when the reason still stands instead of forgetting it. Built and deployed the same evening, paper account.

## 1. Percent profit lock (C4 exit ladder)

Why: the R ladder never protected a thesis position. One R on that lane is three daily ATRs, 17 to 35 percent of price on RIOT, FRMI and INVX, so "up 10 percent" was a third of an R and breakeven at 1R never arrived. RIOT's best moment was +12.4 percent (0.37R), FRMI's first run +10.3 percent (0.34R); both gave it back.

- `src/c4_exec/exits.py`: `profit_lock: {activate_pct, trail_pct}` on a profile. Once the high water mark is `activate_pct` in the position's favour, a stop `trail_pct` behind the high is proposed; the tighter of it and the R ladder wins; tighten only; journaled as a TRAIL ratchet with "profit lock" in the reason. Side aware (a short's high water mark is its low).
- `config/exit_profiles.yaml`: `thesis_v1` and `long_term_v1` get `{activate_pct: 0.08, trail_pct: 0.05}`. Scalp and news profiles are unchanged: their R ladders already engage at 4 to 8 percent.
- `src/c4_exec/engine.py` and `service.py`: a position opened before its profile carried `profit_lock` reads it from the live profile in memory, so FRMI (open, +10 percent tonight) and INVX are covered without editing journal rows.

## 2. Price confirmed re-entry (C11)

Why: the thesis store keeps a thesis alive after its position dies and C11 re-plans nightly, so re-entry already existed; the trigger was the calendar (five day cooling off), which is how INVX was bought back the morning after its stop and fell another 6 percent.

- `src/c11_thesis/service.py`: `forced_exit_levels()` (ticker to price of its last STOP, CATASTROPHE, INVALIDATION, REVIEW, GUARD, BREAKER or BREAKEVEN exit, any lane, 30 days) and pure `reentry_verdict()`: a candidate is skipped `THESIS_SKIP / REENTRY_WAIT` with the exit price, the level to reclaim and the last close until the last close is back above the level it was stopped at. `config/thesis_entry.yaml` `entry.reentry: {enabled, lookback_days: 30, reclaim_buffer_pct: 0.0}`; `reentry_cooloff_days` 5 to 1 (keeps the next morning rebuy out; price decides after that).
- Declined re-entries are journaled with their numbers, so A11 can replay them later and A9 can judge the rule.

## Tests and services

`tests/unit/test_v0_26_0.py` (lock arms at 8 percent and trails 5 percent, long and short, tighter of the two, re-entry verdicts, config pins, wiring). Suite green. `c4-exec` restarted after the close; `thesis-entry` is a timer and uses the new rule from its next run (2026-09-23 21:15 CT).

## Rollback

Remove the two `profit_lock` lines and `reentry.enabled: false`, `reentry_cooloff_days: 5`; restart `c4-exec`. Or `git reset --hard v0.25.2` on main and restart `c4-exec`.
