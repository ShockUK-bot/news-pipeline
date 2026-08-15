-- Migration 014 — restore dash_positions ownership (v0.13.0 hotfix,
-- 2026-08-15)
--
-- WHAT WENT WRONG: migration 013 rebuilt journal.dash_positions
-- (DROP + CREATE, to add the side column) but omitted the
-- ALTER ... OWNER TO trader that every object-creating migration carries
-- (004/005/007/011 pattern). The rebuilt view was owned by postgres, the
-- trader role lost SELECT, and the dashboard's first query failed —
-- observed as "c6 dashboard disconnected, no data" from deploy day.
-- Trading was unaffected; only the dashboard read path broke.
--
-- RULE FOR ALL FUTURE MIGRATIONS (the lesson, alongside 009's): any
-- migration that CREATEs or DROP+CREATEs an object must end with
-- ALTER ... OWNER TO trader for that object.
--
-- Idempotent: safe to run even after the manual one-liner fix.

BEGIN;

ALTER VIEW journal.dash_positions OWNER TO trader;

INSERT INTO journal.schema_meta VALUES
  (14, now(), 'hotfix: dash_positions owner -> trader (013 rebuild dropped it)');

COMMIT;
