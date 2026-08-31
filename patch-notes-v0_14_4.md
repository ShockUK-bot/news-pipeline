# v0.14.4 — The watchdog learns to ask the right witness

**Base:** built on the tree the Spark is running, `v0.14.3` (commit
`9f67b93`), which the 2026-08-31 git comparison confirmed identical to
`origin/main`. Every v0.14.x change is therefore already inside these files;
this release overrides nothing.

**What triggered it:** on 2026-08-27 the Spark rebooted at 17:35:24 CDT.
`a1-triage` started five seconds later, before DNS was answering, and hung
inside `A1Service()` on a `HEAD https://huggingface.co/BAAI/bge-small-en-v1.5`
request. It never reached `consume_loop`, so it never wrote a heartbeat and
never triaged an item again. It stayed `active (running)` for **85 hours** and
consumed **3.767 seconds of CPU** in that time. Friday 28 August was a full
trading session with no triage, therefore no thesis, no gate pass, and no new
entry. Exits and stops were unaffected throughout; c4-exec was never involved.

Nothing restarted it because **systemd only restarts services that exit**, and
a hung process never exits. Nothing alerted on it because C7 asked systemd the
same question systemd had already answered wrong, and reported
`all clear (35 units checked)` through the whole outage. The dead-man DID see
it, and its only escalation for `triage` is ALERT, so four consecutive morning
briefings carried one identical yellow line at the bottom of the email.

**No trading logic, gates, sizing, or execution behaviour changes.** Every
change here is observability. The one behavioural change is that three
services now write a heartbeat they always should have written.

---

## 1. The embedder no longer needs the network to start (the actual cause)

`ops/systemd/{a1-triage,a2-analyst,c2-dedup}.service` gain:

```
Environment=HF_HUB_OFFLINE=1
Environment=TRANSFORMERS_OFFLINE=1
```

These are the only three units that call `get_embedder()` at startup
(a2-analyst and c2-dedup reach it through `context.py` and `cluster.py`).
The model is already cached at
`/home/trader/.cache/huggingface/hub/models--BAAI--bge-small-en-v1.5`; with
these set, sentence-transformers loads from that cache and never opens a
socket. A **missing** cache becomes a loud startup failure instead of a silent
hang, which is the trade we want.

Worth stating plainly, because it was my first theory and it was wrong:
`After=network-online.target` and `Wants=network-online.target` were **already
present** in these units and did not help. `network-online.target` was reached
while name resolution was still failing. Ordering cannot fix this. Not needing
the network can.

## 2. Three services had startup-only heartbeats

`c2-dedup`, `a3-risk` and `a13-chat` wrote their `journal.health` row **once,
at startup, and never again**. On 2026-08-31 all three read `OK` with a
timestamp of 5595 minutes — 93.25 hours — which is 17:37 on the 27th, the
reboot minute. All three were alive and working. Their rows were simply lying.

v0.11.7 added the 60-second heartbeat to A1 and C3 and noted A2 already had
one. It never reached C2, A3 or A13, and nothing noticed because a stale `OK`
looks exactly like a fresh `OK`.

**A3 risk was the dangerous one.** It is the position sizing agent, it logs
nothing while idle, it had no periodic heartbeat, and it was not in
`config/deadman.yaml`. Had it died, no layer of this system would have said
so, and the only symptom would have been trades quietly stopping — which is
indistinguishable from a quiet news day.

New `Heartbeat` helper in `src/c1_ingestion/heartbeat.py`; call `start()`
before the loop and `tick()` each iteration, writing at most once per
interval. A1, A2 and C3 keep their existing inline version deliberately: they
work, they are on the trading path, and churning them buys nothing.

## 3. `set_health` can no longer kill a service

Also in `heartbeat.py`: `set_health` now catches and logs instead of raising.
Previously a transient database error thrown from a periodic heartbeat
propagated out of the consume loop and out of `main()`, killing the process —
the mirror image of the hang and just as quiet. Now the write fails, the row
goes stale, and C7 reports `HEARTBEAT_STALE` within five minutes. **Staleness
is a signal we can see. A dead process is not.**

## 4. C7 asks the service, not systemd

New `src/common/health.py` holds one definition of "stale", read by both C7
and the A8 briefing so they can never disagree.

`config/watchdog.yaml` services gain `max_age_min`. A service is now checked
for freshness whenever it declares both a `health:` component and a limit.
This is the check that catches a process that is up but wedged — **and the
mechanism already existed.** The `heartbeats:` block has been there since
v0.12.7 and covered `exec`, `deadman`, `marketdata`, `mailer`, `backup` and
`earnings`. It was never pointed at `triage`, `dedup`, `risk`, `gate`,
`analyst` or `guard`. It is now pointed at all of them.

`c10-scanner` deliberately declares **no** limit, because C10 writes its row
only inside the 09:50–15:15 ET scan window and a limit there would alarm every
evening. That omission is documented in the file so nobody "fixes" it later.

Also added: `a13-chat` to `services:` (it was absent entirely); `macro-fetch`,
`thesis-entry` and `a4-late` to `timers:`; `queue_prune` and `broker_api` to
`heartbeats:`. After this release every non-dynamic health row on the Spark is
claimed by some check.

## 5. Orphaned health rows are cleaned up

`ingestion:testsource` had been reading `OK` for **47.8 days**, a July test
source whose row nothing writes to. The 2026-08-16 review flagged this pattern
and cleared two rows by hand; this one survived, which is the argument for
automating it.

C7 now warns on an unclaimed non-dynamic row after 7 days and deletes any
unclaimed row after 30. Dynamic `ingestion:*` rows are deleted but never
warned about: GapMonitor writes those on gap open and close only, so a
websocket connected for two weeks correctly has a two-week-old row, and
nagging about it daily would rebuild the very yellow-line-you-stop-reading
failure that let this outage run for four mornings.

The rule, stated once: **a row that lies is worse than no row.**

## 6. The dead-man stops losing information

`src/c4_exec/deadman.py`, two changes:

- The health row was written **inside** the alert loop, once per stale
  component, each write overwriting the last. Only the final component was
  ever visible, so a genuinely dead ingestion could hide behind a stale gate.
  It is now one row naming every stale component, worst first.
- New optional `critical_min` per component makes the row **DOWN** rather than
  DEGRADED once an outage is long rather than momentary. `triage` was stale
  for 85 hours and rendered identically to a five-minute blip.

`risk`, `dedup` and `guard` are added to `config/deadman.yaml`. They are
alert-only, so this adds monitoring with **zero** new blocking behaviour —
the same argument v0.11.7 made when triage and analyst were added. Escalation
to BLOCK_ENTRIES is still driven solely by `block_entries_min`, which only
ingestion and marketdata declare.

`actions` keeps its exact original keys; severity is computed locally, so
`test_09_deadman_ladder_and_ownership` and every other caller see the shape
they always did.

## 7. The morning briefing leads with outages

`ops_section()` used to select `WHERE status <> 'OK'`, which structurally
**cannot** see this failure: `triage` read `OK` the entire time. It now reads
every row, computes ages, and applies the same limits C7 uses.

Any outage now appears in the **subject line** and as a banner **above the
narrative**, with the exact `systemctl restart` command. Replaying the real
2026-08-31 health table through the new code produces:

```
SUBJECT: [OUTAGE] dedup stale 3.9 days +3 more — Morning briefing 2026-08-31

*** PIPELINE OUTAGE — READ THIS FIRST ***
  dedup: no heartbeat for 3.9 days (limit 10 min) — nothing reaches triage without it
      fix: sudo systemctl restart c2-dedup
  ...
  triage: no heartbeat for 3.5 days (limit 10 min) — news AND scanner signals die unheard
      fix: sudo systemctl restart a1-triage
  A component that stops writing its heartbeat is not quiet. It is not running.
```

Against the previous four briefings, which said `All health components OK`
under a single DEGRADED dead-man line.

Durations render in human units throughout. Nobody reacts to `5102.3min`.
People react to `3.5 days`.

## 8. `pyproject.toml` has been invalid TOML since v0.14.0

Found while building this release:

```toml
version = "0.14.0        <-- no closing quote
```

The file does not parse (`TOMLDecodeError: Illegal character '\n'`). I first
read this as an uncommitted edit made directly on the Spark; the 2026-08-31
git comparison proved otherwise — `git status` is clean and the Spark matches
`origin/main` — so the missing quote is **committed and on GitHub, carried by
every release from v0.14.0 through v0.14.3**. It has no effect on the running
services, which use `PYTHONPATH` and `python -m`, which is why four releases
shipped over it, but `pip install -e .` fails on any checkout. Repaired and
set to `0.14.4`, which doubles as this release's version bump — there is no
separate bump step in the deploy guide.

---

## Validation

Ran here, against the real source tree pulled off the Spark:

- **25 new unit tests pass** (`tests/unit/test_health_freshness.py`) —
  pure functions, no database, no systemd. They cover the freshness limits,
  the worst-first ordering, the rth_only skip, absent-is-not-stale, the
  opt-out for windowed services, the orphan warn/delete split, alert
  fingerprinting, and the briefing subject and banner.
- **The incident replayed.** Feeding the actual `journal.health` ages from the
  Spark at 14:52 on 2026-08-31 through the new `evaluate()` with the real
  `config/watchdog.yaml`:
  - **before:** 4 CRITICAL findings — `triage`, `dedup`, `risk`, `chat`
  - **after** the fix is deployed and those services restarted: **silent**
- Every Python file in `src/` compiles.
- Both config files parse identically under PyYAML **and** the built-in
  tiny-YAML fallback in `common/config.py`, including the new inline comments
  and the top-level `never_orphan` list.
- `pyproject.toml` now parses; `tomllib` reports version `0.14.4`.

**Not run here** (no database, no systemd, no models in this environment):
the full `pytest -q` suite. Part 8 of the deploy guide runs it on the Spark.
The one file I could not check against is `tests/` itself, which was not in
the tarball — if `test_09_deadman_ladder_and_ownership` fails, send me the
output; I kept `actions` shape-compatible specifically to avoid that.

## Files

**NEW (2):** `src/common/health.py`,
`tests/unit/test_health_freshness.py`

**REPLACED (14):** `src/c1_ingestion/heartbeat.py`,
`src/c2_dedup/service.py`, `src/a3_risk/service.py`,
`src/a13_chat/service.py`, `src/c7_watchdog/service.py`,
`src/c4_exec/deadman.py`, `src/a8_briefing/facts.py`,
`src/a8_briefing/render.py`, `config/watchdog.yaml`, `config/deadman.yaml`,
`ops/systemd/a1-triage.service`, `ops/systemd/a2-analyst.service`,
`ops/systemd/c2-dedup.service`, `pyproject.toml`

No database migration. No new dependency. No model change.

**Every replaced file was built from the copy running on the Spark**, not from
the GitHub snapshot, because the synced copy I can read is stale — it still
showed the pre-v0.11.7 `COMPONENT_MAP`. Any uncommitted Spark edit in these
files is therefore preserved rather than reverted, and uploading will bring
GitHub into line with the Spark for them.

## Deploy note — ordering matters this time

The new `config/watchdog.yaml` starts requiring `dedup`, `risk` and `chat` to
be fresh **the moment the checkout lands**, but those three only start
heartbeating once they are **restarted**. Between those two moments C7 would
fire three false CRITICALs. The deploy guide therefore stops
`c7-watchdog.timer` first and starts it again at the end. It self-corrects
either way (a RECOVERED email follows), but there is no reason to send it.

`c4-exec` needs a restart for the dead-man change. It is the execution engine
and reconciles broker positions on boot, so do it with the market closed.

## Rollback

`git checkout v0.14.3`, restore the three unit files
(`sudo cp ops/systemd/*.service /etc/systemd/system/ && sudo systemctl
daemon-reload`), restart the six touched services. No schema changes, so
nothing to undo in the database. The health rows C7 deleted as orphans do not
come back, which is the intended outcome.
