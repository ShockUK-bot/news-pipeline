-- 023 (v0.30.2, 2026-11-07): allow status 'SHADOW' on journal.scanner_candidates.
-- v0.28.0's catalyst shadow lanes journal SHADOW rows; the status check
-- constraint (migration that created the table) did not list it, so every
-- insert failed from 2026-11-03 to 2026-11-06 (31 'catalyst shadow error'
-- lines in c10-scanner) and the lanes measured nothing. Additive: widens
-- the allowed set, touches no rows.
ALTER TABLE journal.scanner_candidates DROP CONSTRAINT IF EXISTS scanner_candidates_status_check;
ALTER TABLE journal.scanner_candidates ADD CONSTRAINT scanner_candidates_status_check
    CHECK (status = ANY (ARRAY['EMITTED'::text, 'FILTERED'::text, 'SUPPRESSED_NEWS'::text,
                               'CAPPED'::text, 'BREAKER'::text, 'SHADOW'::text]));
