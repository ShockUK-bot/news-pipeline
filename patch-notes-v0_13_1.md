# v0.13.1 — Dashboard hotfix (dash_positions ownership)

**Symptom:** since the v0.13.0 deploy, the dashboard (c6) shows
disconnected with no data. Trading was never affected — C3/A3/C4, the
journal, the watchdog and the email reports all ran normally throughout.
Only the dashboard's read path broke.

**Root cause:** migration 013 rebuilt the `journal.dash_positions` view
(DROP + CREATE, to add the new `side` column) but omitted the
`ALTER ... OWNER TO trader` line that every object-creating migration in
this repo carries (the 004/005/007/011 pattern). The rebuilt view ended up
owned by `postgres`, so the `trader` role the dashboard connects as gets
*permission denied* on it. The dashboard's very first data query —
`SELECT * FROM journal.dash_positions` — fails, and the front-end reports
disconnected.

**Fix:** migration `014-dashboard-view-owner.sql` — one statement:

    ALTER VIEW journal.dash_positions OWNER TO trader;

Idempotent (safe to run any number of times). No code changes, no config
changes, one service restart (c6-dashboard).

**Rule recorded for all future migrations** (in 014's header, alongside
migration 009's constraint-union rule): any migration that CREATEs or
DROP+CREATEs an object must end with `ALTER ... OWNER TO trader` for that
object.

**Release contents:** 3 new files (migration 014 + these notes + the
deploy guide). Zero replaced files.
