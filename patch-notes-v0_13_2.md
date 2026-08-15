# v0.13.2 — Dashboard hotfix 2 (restore origin / total_cost / pct_pnl)

**Symptom:** after v0.13.1 brought the dashboard back, the positions
panel's ORIGIN column (news | scanner | thesis) went blank, along with
total cost and % P&L.

**Root cause:** migration 013 rebuilt the `dash_positions` view from the
BASE schema file instead of the live definition — the exact mistake
migration 009's postmortem documented for CHECK constraints, repeated on a
view. Migration 006 had extended the view with `origin`, `total_cost` and
`pct_pnl` (the dashboard reads all three); 013's rebuild silently dropped
them. `test_schema_vocab.py` mechanically guards constraint vocabularies
against this, but nothing guarded views.

**Fix:** migration `015-restore-dash-view.sql` rebuilds the view as the
full union — 006's complete column list in 006's order, with 013's `side`
APPENDED at the end (append-only, so future changes can use
CREATE OR REPLACE again). Two improvements ride along:

- `pct_pnl` is now **side-aware**: a SHORT position's % P&L is positive
  when the price fell. Under the 006 formula a winning short would have
  displayed as a loss.
- `ALTER VIEW ... OWNER TO trader` is in the same migration (the v0.13.1
  lesson, applied).

No code changes, no config changes, one service restart (c6-dashboard).

**Rules reaffirmed in 015's header:** rebuild any object from its CURRENT
LIVE definition (`SELECT pg_get_viewdef(...)`), never from the base schema
file; and every CREATE/DROP+CREATE ends with an OWNER TO trader.

**Release contents:** 3 new files (migration 015 + these notes + the
deploy guide). Zero replaced files.
