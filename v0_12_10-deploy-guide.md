# Deploy Guide — v0.12.10 (gate re-check window + veto counterfactuals)

**What this release does:** the gate's 30-minute confirmation window
becomes a real window (Aug 3 it was a single check ~4 minutes after
publish — that's how all 21 theses died), and every veto now gets measured
after the close so we can finally tune the placeholder thresholds on
evidence. Plus: no more false watchdog emails during deploy restarts.
Full story in `patch-notes-v0_12_10.md`.

**When to do this: before Tuesday's open.** ~10 minutes. One migration,
one service restart (`c3-gate`) — safe while the market is closed. The
watchdog needs no restart (it's relaunched fresh by its timer every 5
minutes and picks up the new code by itself).

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_10-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   four folders (`src`, `config`, `schema`, `tests`) and two loose `.md`
   files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show paths like `src/c3_gate/counterfactual.py` and
> `schema/migrations/010-gate-counterfactuals.sql` — with the folders in
> front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` + `config` + `schema` + `tests` folders and the two
   `.md` files.
2. **Four files are REPLACED:** `src/c3_gate/service.py`,
   `src/c7_watchdog/service.py`, `config/gate.yaml`,
   `tests/unit/test_watchdog.py`. **Five are NEW:**
   `src/c3_gate/counterfactual.py`,
   `schema/migrations/010-gate-counterfactuals.sql`,
   `tests/unit/test_gate_recheck.py`, the patch notes, and this guide.
3. Commit message: `v0.12.10: gate re-check window + veto counterfactuals`
4. **Commit changes**, open the commit, confirm **9 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.9"` →
   `version = "0.12.10"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.10` → title
   `v0.12.10 — gate re-check window` → **Publish**.

## Part 4 — Pull, apply the migration, restart the gate

Paste this whole block on the Spark (the export line makes psql work;
harmless if you already ran it today):

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.10
psql "$PIPELINE_DSN" -f /opt/pipeline/schema/migrations/010-gate-counterfactuals.sql
psql "$PIPELINE_DSN" -c "SELECT max(schema_version) FROM journal.schema_meta;"
sudo systemctl restart c3-gate
```

The migration prints `BEGIN / CREATE TABLE / CREATE INDEX / CREATE INDEX
/ INSERT 0 1 / COMMIT`, and the query must show `10`.

Apply to the test database too (an error saying the database does not
exist is fine — skip it):

```bash
psql "${PIPELINE_DSN%/*}/trading_test" -f /opt/pipeline/schema/migrations/010-gate-counterfactuals.sql
```

## Part 5 — Verify

**1. The migration created the actual table** (the v0.12.2 lesson —
verify the artifact, not just the version number):

```bash
psql "$PIPELINE_DSN" -c "\d journal.gate_counterfactuals" | head -8
```

You should see the table with `cf_id`, `decision_id`, `ticker`… columns.

**2. The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_gate_recheck.py tests/unit/test_gate_defer.py tests/unit/test_watchdog.py tests/unit/test_analyst_gate.py tests/unit/test_schema_vocab.py -q'
```

Expect `69 passed`.

**3. The gate is healthy on the new code:**

```bash
systemctl is-active c3-gate
sudo journalctl -u c3-gate --since "2 minutes ago" --no-pager | tail -5
```

`active`; the log shows `C3 up` and no tracebacks.

**4. No false watchdog email from this deploy.** The c3-gate restart you
just did is itself the test — within 5 minutes the watchdog runs a pass;
the inbox should stay QUIET (before this release, a pass landing
mid-restart emailed a false SERVICE_DOWN). Confirm the pass ran clean:

```bash
sudo journalctl -u c7-watchdog --since "10 minutes ago" | grep "watchdog pass" | tail -2
```

Expect `findings=0 ... alert=none`.

## Part 6 — Reboot survival (standing check)

```bash
systemctl is-enabled c3-gate c7-watchdog.timer
```

Both: `enabled`.

## Part 7 — The behavioral proof comes from Tuesday's session

In the evening, three things to look at:

**1. Re-checks happened** (new log lines — each is one extra look the old
code never took):

```bash
sudo journalctl -u c3-gate --since today | grep -c "gate RECHECK"
```

Any number above 0 proves the window is live. Final vetoes now journal at
`minutes` up to ~29 instead of always 3–6:

```bash
psql "$PIPELINE_DSN" -c "SELECT veto_reason, count(*), round(avg((payload->>'minutes')::numeric),1) AS avg_minutes FROM journal.decisions WHERE stage='GATE' AND action='VETO' AND ts::date=current_date GROUP BY 1;"
```

**2. Counterfactual rows exist and fill after the close:**

```bash
psql "$PIPELINE_DSN" -c "SELECT count(*) FILTER (WHERE NOT complete) AS pending, count(*) FILTER (WHERE complete) AS filled FROM journal.gate_counterfactuals;"
```

During the session vetoes accumulate as `pending`; within ~30 minutes
after the close they flip to `filled`.

**3. If anything PASSES the gate** it will show as `GATE | PASS` in the
decision tape and flow to A3/C4 — the first candidate entry since Jul 27.
Nothing passing on day one is NOT a failure: thresholds are unchanged;
what's new is that confirmation arriving at minute 12 now counts, and
every miss is being measured. After ~a week we tune from
`journal.gate_counterfactuals`.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.9
sudo systemctl restart c3-gate
```

The new table is additive and can stay in place.
