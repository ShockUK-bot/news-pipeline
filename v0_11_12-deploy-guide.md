# Deploy Guide — v0.11.12 (marketdata heartbeat probe — dead-man flapping fix)

**What this release does:** stops the dead-man safety switch from blocking
your trades during normal quiet news stretches. Today (2026-07-22) the switch
blocked and unblocked entries dozens of times all session because the
market-data heartbeat was only refreshed when news happened to reach the
gate — and it blocked the system's **first-ever confirmed BUY signal** (PSKY,
12:43) by 18 seconds. After this release, the gate checks the market-data
connection itself every 60 seconds, so the safety switch only trips when the
data feed is *actually* down. **No database changes, no new timers, no new
permissions, no new keys.**

**When to do this: before tomorrow's open.** ~10 minutes. Until it's
deployed, entries stay blocked most of every session and any PASS will keep
dying at A3 with `BLOCK_ENTRIES`.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_11_12-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a
   folder `v0_11_12-pack` containing a `src` folder, a `config` folder, a
   `tests` folder, and two loose `.md` files.

## Part 2 — Upload to GitHub (browser, same as always)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in, from the extracted `v0_11_12-pack` folder: the **src** folder,
   the **config** folder, the **tests** folder, and the two **`.md`** files.
3. **Two files are REPLACED** (GitHub handles this automatically):
   - `src/c3_gate/service.py`
   - `config/gate.yaml`

   **Three files are NEW:**
   - `tests/unit/test_marketdata_probe.py`
   - `patch-notes-v0_11_12.md`
   - `v0_11_12-deploy-guide.md`
4. Commit message: `v0.11.12: marketdata heartbeat probe (dead-man flapping fix)`
5. **Commit changes**, then open the commit and confirm it shows **5 changed
   files** (2 replaced + 3 new). If it shows anything different — stop and
   tell Claude before going further.

## Part 3 — Version bump + release

1. Open `pyproject.toml` in the repo → pencil (edit) icon → change
   `version = "0.11.11"` to `version = "0.11.12"` → **Commit changes** to
   `main`.
2. **Releases → Draft a new release** → tag `v0.11.12` → title
   `v0.11.12 — marketdata heartbeat probe` → **Publish**.

## Part 4 — Pull onto the Spark and restart the gate

Open a terminal on the Spark and run these one at a time:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.12
sudo systemctl restart c3-gate
```

If the `checkout` line prints an error about "local changes would be
overwritten", **stop** and paste it to Claude — don't force anything.

Then confirm it came back up:

```bash
sudo systemctl is-active c3-gate
```

Should print `active`. If it says `failed`, paste the last 20 lines of
`sudo journalctl -u c3-gate -n 20 --no-pager` to Claude.

## Part 5 — (Optional but recommended) Run the tests on the Spark

```bash
cd /opt/pipeline
export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test
.venv/bin/python -m pytest tests/unit/test_marketdata_probe.py -q
```

Expect the last line to say **7 passed**.

## Part 6 — Confirm the fix is alive

**Right away (any time, even after hours):** give it a minute or two, then:

```bash
psql "$PIPELINE_DSN" -c "SELECT component, status, detail, updated_ts FROM journal.health WHERE component='marketdata';"
```

You should see `OK` with detail `probe ok (SPY)` and an `updated_ts` that
moves forward every ~60 seconds each time you re-run the command. A moving
timestamp is the whole fix — that's what feeds the dead-man.

**Tomorrow during market hours (the real proof):**

```bash
sudo journalctl -u c4-exec --since today --no-pager | grep -ci "dead-man BLOCK"
```

Yesterday this pattern fired dozens of times. Tomorrow it should print `0`
(or at most a `1` from before the deploy). If you see repeated blocks naming
`marketdata` again, paste them to Claude.

## Rollback (if anything misbehaves)

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.11
sudo systemctl restart c3-gate
```

Nothing else to undo — no migrations, no timers, no config-value changes
(the two new gate.yaml keys just match the code's built-in defaults).
