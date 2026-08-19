# v0.13.7 — A6 column-list drift: `p.side` was added to the SELECT and not to the key list (2026-08-19)

**Fixes the `a6-eod` + `a6-nightly` `LAST_RUN_FAILED` pair reported by the
C7 watchdog on 2026-08-19.** One file replaced, one new DB-free test file.
No schema changes, no config changes, no service restarts (both units are
oneshot timers — they pick the fix up on their next fire).

---

## The bug — one line short of a key list

`src/a6_position_review/context.py::load_open_positions` builds its result
dicts by zipping a **hand-written** key tuple against the row tuple:

```python
return [dict(zip(cols, r)) for r in rows]
```

v0.13.0 (short selling, 2026-08-14) added `p.side` to the SELECT list —
and did not add `"side"` to `cols`. **18 columns selected, 17 names.**
`zip()` truncates silently, so every key from index 14 on shifted by one:

| # | selected | mapped to key | should be |
|---|---|---|---|
| 14 | `p.side` | `thesis_payload` | `side` |
| 15 | `d.payload` | `thesis_reason` | `thesis_payload` |
| 16 | `d.reason` | `thesis_confidence` | `thesis_reason` |
| 17 | `d.confidence` | **dropped** | `thesis_confidence` |

`build_pack` then runs `(pos["thesis_payload"] or {}).get("thesis")` against
the string `"SHORT"`:

```
AttributeError: 'str' object has no attribute 'get'
```

Reproduced exactly, off the v0.13.6 source, with a row shaped as Postgres
returns it. That is the traceback in `journalctl -u a6-nightly`.

## Why both units, why now, why not before

Both entrypoints go through the same loader and the same `build_pack`:
`run_eod` (line 125) and `run_nightly` (line 283). One defect, two dead
timers.

It only bites when the book is **not** empty. An empty book exits early at
`SKIPPED_NO_POSITIONS` with status 0, which is why the units stayed green
from v0.13.0's deploy on 15 Aug until a position was actually open at fire
time. `a6-eod` filters `horizon='SHORT'`, so its failure is itself
evidence: **there is an open SHORT-horizon position right now.**

Not caught by the suite because the coverage that would have caught it —
`tests/integration/test_position_review_flow.py` — needs a live PostgreSQL
and is not run on the Spark (no `trading_test` database; the same gap that
keeps `test_cik_map` deselected). Every A6 unit test builds its own dicts
and never touches the loader.

## Second defect in the same file (silent, still live until this deploy)

`build_pack` calls `r_progress(...)` **without** the `side` argument that
v0.13.0 added, so it defaults to `LONG`. A6's own packs and — through
`a8_briefing/facts.py`, which reads the same loader — **the morning
briefing** have been reporting R-progress with the **wrong sign for short
positions** since 15 Aug: a winning short shows as a loser. A8 also asks
for `p.get("side")`, which the truncated zip never produced, so every
position has been rendering as `LONG`.

Same family as v0.13.0's `dash_positions` view (`pct_pnl` not side-aware)
— shorting shipped with the sign fixed in `common/direction.py` and missed
in the two places that keep their own copy.

## The fix

**1. The loader takes its keys from the cursor, like everything else does.**
Every other row-reader in this codebase — A1, A2, A12, A13, C4 `state.py`,
`common/queue.py` — already uses `cols = [d.name for d in cur.description]`,
which cannot drift from the SELECT. A6 was the outlier. The three
decision-side columns are now aliased in SQL (`d.payload AS thesis_payload`,
etc.) so callers see the same key names as before.

**2. `build_pack` passes `side` into `r_progress`** and puts `side` in the
pack — the nightly prompt already tells the model *"each position states
its side"*, and until now no position did.

**3. `side` defaults to `"LONG"` when NULL** (positions opened before
v0.13.0).

## The guard — the class, not the instance

`tests/unit/test_a6_context_columns.py` (NEW, DB-free, 4 tests):

- **Structural:** walks every function in `src/`, and for any that both
  builds a `SELECT` and assigns a literal `cols` tuple, asserts one name
  per selected column. Run against v0.13.6 it names the offender and the
  counts. Two such sites exist today — A6's (was broken) and
  `c11_thesis/service.py::open_positions_all` (9/9, correct).
- **Contract:** A6's SELECT must carry `p.side` and the three `AS thesis_*`
  aliases.
- **Behavioural:** `build_pack` over a realistic row survives, keeps
  `thesis`/`thesis_reason` in the right fields, and gives a SHORT that has
  moved 50 → 44 on a 3.0 R-unit **+2.0R, not −2.0R**. NULL side → `LONG`.

All four are **red on v0.13.6, green on v0.13.7** (verified both ways).

## Changed files

- `src/a6_position_review/context.py` (**REPLACED**)
- `tests/unit/test_a6_context_columns.py` (**NEW**)
- `pyproject.toml` → `0.13.7` (pencil edit)

## Tests

4 new. Unit suite expected `1 failed, 683 passed` (v0.13.6's 679 + 4; the
one failure is the pre-existing `test_triage_v047.py::test_confidence_required`).

## What was lost while it was broken

Between 15 Aug and this deploy, on every day a position was open:

- **no EOD overnight-hold recommendation** — C4's code overnight rule
  governed unaided (the documented degradation, but it degraded silently
  through a crash rather than through `SKIPPED_NO_MODEL`);
- **no nightly review, no `POSITION_REVIEW` rows, no `STALE_FLAG` rows** —
  the code-side staleness rule never ran either, because it runs *after*
  `build_pack`;
- **no nightly ALERT emails** (indistinguishable from a quiet night);
- **morning-briefing R-progress sign-inverted for shorts.**

Nothing here touches order flow. A6 is recommendation-only and C4's exit
layers were unaffected throughout.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.6
```

Which restores the crash. There is no reason to.
