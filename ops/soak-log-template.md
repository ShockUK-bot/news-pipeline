# Paper-Soak Daily Log — Session N of 10

Date (ET):            | Operator present: yes/no (target: last 5 = no)
Attended intervention this session: none / <describe + why>

## Morning check (pre-open, after soak-check.sql)
- [ ] H1 health all OK, fresh
- [ ] H5 backup < 26h
- [ ] H6 dead-letter count = 0
- [ ] Quarantine reviewed (S5) — rows: __
- [ ] Gaps explained (S1) — open gaps: __
- Control flags: kill_switch=__ breaker=__ block_entries=__ capital=__ max/day=__

## Session
- Decisions by stage/action (soak-check §4): paste
- REJECT rate (S2): __% — trend vs yesterday: up/flat/down
- Oldest signal.triage pending peak (S3): __s
- Entries today: __ / max __ ; all limit-DAY w/ intent_id COID (H7): yes/no
- Cat stops broker-side verified (H3): yes/no
- 15:45/15:55 overnight matrix + 16:00 session-close pass (S6): fired as expected? __

## Restarts / incidents
- c4-exec restarts: __ ; reconciliation drift (H2): 0 / <detail>
- Dead-man events: ALERT __ / BLOCK_ENTRIES __ — causes: __
- Breaker trips (H8): __

## Dedup eyeball (S4)
- Clusters spot-checked: __ ; over/under-clustering notes: __

## Anomalies & notes (anything surprising in soak-check output)
-

## Config changes (require service restart for honest config_version)
- none / <git SHA before -> after, what, why>
