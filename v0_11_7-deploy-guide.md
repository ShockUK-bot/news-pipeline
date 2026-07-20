# Deploy Guide — v0.11.7 (health polish: heartbeats, dead-man map, EDGAR)

**What this does:** clears the two nagging DEGRADED lines on the dashboard
(`Deadman stale: gate` and `edgar … ReadTimeout`) and fixes a latent bug where
the dead-man wasn't actually monitoring A1/A2. Four small files, **no trading
logic changed.**

**When:** the ingestion/gate/triage parts are safe any time. The one execution-
engine restart (`c4-exec`) is best saved for **market close** — see Part 5.

---

## Part 1 — Get the pack

1. Download `v0_11_7-pack.zip`, extract. You'll get a `src` folder and two
   `.md` files.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **src** folder and the two `.md` files.
3. **Four files are REPLACED:** `src/c3_gate/service.py`,
   `src/a1_triage/service.py`, `src/c4_exec/deadman.py`,
   `src/c1_ingestion/sources/edgar.py`.
4. Commit message: `v0.11.7: heartbeats + dead-man map + EDGAR resilience`
5. Commit; confirm **6 changed files** (4 replaced + 2 new `.md`).

## Part 3 — Version bump + release

1. `pyproject.toml` → `version = "0.11.6d"` → `version = "0.11.7"` → commit to
   `main`.
2. **Releases → Draft a new release** → tag `v0.11.7` → title
   `v0.11.7 — health polish` → **Publish**.

## Part 4 — Pull onto the Spark + restart the safe services

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.7
sudo systemctl restart c3-gate a1-triage c1-ingestion
sudo systemctl is-active c3-gate a1-triage c1-ingestion
```

All three should print `active`. Give it ~2 minutes, then confirm the two
DEGRADED lines clear:

```bash
psql "$PIPELINE_DSN" -c "SELECT component, status, detail, updated_ts FROM journal.health WHERE component IN ('deadman','gate','triage','edgar') ORDER BY component;"
```

- `gate` and `triage` should have a **fresh** `updated_ts` (within the last
  minute or two) and status OK.
- `deadman` should flip to `OK — all heartbeats fresh` (it may still show the
  old `stale: gate` for up to a minute until the next monitor pass).
- `edgar` should be OK (a transient timeout now needs 3 in a row to show
  DEGRADED).

## Part 5 — Restart c4-exec at market close (activates the dead-man map fix)

The dead-man runs inside `c4-exec`. Restarting it picks up the corrected
component map so it starts monitoring A1 triage and A2 analyst. Because
`c4-exec` is the execution engine and reconciles broker positions on boot, do
this **when the market is closed**:

```bash
sudo systemctl restart c4-exec
sudo systemctl status c4-exec --no-pager
sudo journalctl -u c4-exec -n 20 --no-pager | grep -i "reconcil"
```

Look for `active (running)` and a reconciliation summary line. Nothing else to
do — the dead-man will now show fresh triage/analyst monitoring.

(If you'd rather not wait: it's still safe during market hours since C4
reconciles from the broker on boot, but market-closed is the low-risk choice.
The gate DEGRADED line already cleared in Part 4 regardless.)

## Confirm it worked

The dashboard **System health** panel should show `deadman` green and `edgar`
green (steady), with `gate`/`triage` timestamps updating every minute.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.6d
sudo systemctl restart c3-gate a1-triage c1-ingestion c4-exec
```

No database or schema changes were made.
