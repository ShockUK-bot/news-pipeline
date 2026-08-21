# v0.13.10 — Dashboard shows the side (and its dollar P&L learns the sign)

Operator question that triggered it: "How will I tell on the c6-dashboard
if it's a long or short? Do we need a new field?"

**No new field needed** — `side` has been in the `dash_positions` view
since migration 015 (v0.13.2) and every API response already carries it.
The page just never rendered it. Two changes, one file:

1. **New Side column** in the Open Positions table, right after Ticker:
   a green `LONG` chip (the quiet default) or a red `SHORT` chip (loud on
   purpose — that's the row where price-down is green and the stop sits
   ABOVE the entry, and it must never be misread as a long). Positions
   opened before v0.13.0 have no side value and render `LONG`, which is
   what they are.
2. **The row's dollar Unrl P&L is now side-aware.** The page computed it
   locally as `(current − entry) × qty` — long math, the third copy of
   this exact bug (after the `dash_positions` `pct_pnl` fixed in v0.13.2
   and A6/A8's `r_progress` fixed in v0.13.7). A winning short would have
   shown a red dollar figure next to a green percentage. Now `dir`
   (−1 for SHORT) multiplies through, matching the view's `pct_pnl` and
   the stats tile (whose SQL was made side-aware in v0.13.0).

Family tally, for the record: `common/direction.py` was supposed to be
"one sign, one place," and the places that kept their own copies —
the view, A6's loader, A8's briefing, and this page — have each needed a
follow-up. This was the last known private copy; anything new found doing
its own `(price − entry)` arithmetic should be treated as a bug on sight.

**Release contents:** 1 replaced file (`dashboard/index.html`) + these
notes + the deploy guide. No migration, no config, no Python changes,
no test-count change. Restart c6-dashboard only.
