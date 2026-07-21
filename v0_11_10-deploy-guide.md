# Deploy Guide — v0.11.10 (gate defers fast signals until minute bars exist)

**What this does:** stops the C3 gate terminally vetoing fast in-session
signals as `MARKETDATA_MISSING` ~60s after publish (the GM/EMBJ "no volume
bars" dashboard errors). Fast signals are now deferred a few minutes and then
evaluated with real data.

**Five files replaced, three new. No database change, no systemd file change.
One service restart (`c3-gate`) — safe at any time, including market hours
(the queue redelivers anything in flight).**

---

## Part 1 — Get the pack

Download `v0_11_10-pack.zip` and extract it. You'll get a `src` folder, a
`config` folder, a `tests` folder, and two `.md` files.

## Part 2 — Upload to GitHub

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **src** folder, the **config** folder, the **tests** folder,
   and the two `.md` files.
3. **Five files are REPLACED:**
   - `src/common/queue.py`
   - `src/c3_gate/service.py`
   - `src/c3_gate/rules.py`
   - `config/gate.yaml`
   - `tests/integration/test_analyst_gate_flow.py`

   **Three files are NEW:**
   - `tests/unit/test_gate_defer.py`
   - `patch-notes-v0_11_10.md`
   - `v0_11_10-deploy-guide.md`
4. Commit message:
   `v0.11.10: defer gate evaluation until minute bars can exist`
5. Commit. Confirm **8 changed files**.

## Part 3 — Version bump + release

1. Open `pyproject.toml`, change `version = "0.11.9"` → `version = "0.11.10"`,
   commit to `main`.
2. **Releases → Draft a new release** → tag `v0.11.10` → title
   `v0.11.10 — gate defer-until-mature` → **Publish**.

## Part 4 — Pull onto the Spark and restart the gate

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.10
sudo systemctl restart c3-gate
```

Only `c3-gate` needs a restart — it's the only always-on service whose code
changed. (`queue.py` also changed, but the only caller of the new `defer()`
helper is C3 itself; every other service keeps working with the code it has
until its next natural restart.)

Confirm it came back up:

```bash
sleep 3 && systemctl is-active c3-gate && sudo journalctl -u c3-gate -n 5 --no-pager
```

You should see `active` and a fresh `C3 up` line.

## Part 5 — Watch it work (during market hours)

The new behaviour is visible in the gate's own log. During RTH, run:

```bash
sudo journalctl -u c3-gate --since "30 minutes ago" --no-pager | grep -E "DEFER|VETO|PASS"
```

**What you should see:**

- `gate DEFER` lines with `delay_secs=…` for fast-arriving news — each one is
  a signal that would have been killed as `MARKETDATA_MISSING` before today.
- A few minutes after each DEFER, the **same signal_id** comes back as a
  normal `gate PASS` or `gate VETO` line with a real reason
  (`GATE_NO_CONFIRM`, `LONG_ONLY`, …) — this time computed from actual bars.
- `MARKETDATA_MISSING` should become **rare**. If you see one now, take it
  seriously: it means a properly-sized window had zero bars — usually a
  trading halt on that ticker, otherwise a real data problem.

**On the dashboard:** the System health `marketdata` row should stop flipping
to DEGRADED mid-session with "no volume bars for …". The vetoed-trades panel
should show real `vol_mult` numbers on fast signals instead of nulls.

## Part 6 — End-of-week check

Same veto-mix query as always, now with trustworthy fast-signal data in it:

```sql
SELECT (ts AT TIME ZONE 'America/New_York')::date AS day, veto_reason, count(*)
FROM journal.decisions
WHERE action='VETO' AND ts > now() - interval '7 days'
GROUP BY 1,2 ORDER BY 1,2;
```

Expect the `MARKETDATA_MISSING` bucket to collapse to ~0 after deploy day.
Bring the numbers to Claude — this also feeds the still-open §14
gate-threshold tuning, which finally has honest inputs for fast signals.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.9
sudo systemctl restart c3-gate
```

Nothing else to undo — no schema or systemd changes were made.
