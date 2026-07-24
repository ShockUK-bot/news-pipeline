# Deploy Guide — v0.12.5 (exit engine live-bar fix + overnight halt fix)

**What this release does:** fixes the bug that froze JNJ's price on the
dashboard this morning (positions held overnight were wrongly treated as
halted stocks), AND a hidden bug it uncovered: the exit engine was
crashing on every live price bar right after updating the price, so
stops/trails/invalidations were never actually evaluated on real market
data. Full story in `patch-notes-v0_12_5.md`.

**When to do this: NOW, during market hours.** ~10 minutes. Only
`c4-exec` restarts, and it re-syncs with the broker before doing anything.
Until this is deployed, open positions are protected by the broker's
catastrophe stop only — the tighter software stops are not running.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_5-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a
   `src` folder, a `tests` folder, and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** Select the
> `src` folder, the `tests` folder, and the two `.md` files, and drag
> that whole selection into the upload box. The preview must show paths
> like `src/c4_exec/engine.py` and
> `tests/integration/test_exit_engine_flow.py` — with the folders in
> front.

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload
   files** → drag the `src` folder + `tests` folder + the two `.md` files.
2. **Three files are REPLACED:** `src/c4_exec/engine.py`,
   `src/c4_exec/service.py`, `tests/integration/test_exit_engine_flow.py`.
   **Two are NEW:** the patch notes and this guide.
3. Commit message: `v0.12.5: exit engine live-bar fix + overnight halt fix`
4. **Commit changes**, then open the commit and confirm it shows
   **5 changed files** with the folder paths above. A different number —
   stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.4"` →
   `version = "0.12.5"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.5` → title
   `v0.12.5 — exit engine live-bar fix` → **Publish**.

## Part 4 — Pull and restart (one service)

On the Spark, one at a time:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.5
sudo systemctl restart c4-exec
```

## Part 5 — Verify (the whole point)

**Right away (2–3 minutes after the restart):**

```bash
sudo journalctl -u c4-exec --since "-3 minutes" --no-pager | grep -ci "engine loop error"
```

This must print **`0`**. Before the fix it printed one per minute. (Run it
again a few minutes later to be sure a full engine pass has happened.)

One WARNING line you MAY still see once after restart is
`predicate arm failed ... UNRESOLVABLE_REF: prenews_price` — that's the
known follow-up issue in the patch notes, not this fix failing.

**The mark keeps moving:**

```bash
psql "$PIPELINE_DSN" -c "SELECT ticker, last_price, last_price_ts FROM journal.positions WHERE status='OPEN';"
```

`last_price_ts` should advance every minute or so during market hours.

**Tomorrow at the open (the overnight fix):** if a position is held
overnight tonight, the dashboard price must start moving within ~2 minutes
of 08:30 your time, and this must print `0`:

```bash
sudo journalctl -u c4-exec --since today --no-pager | grep -ci "halt heuristic froze"
```

**Tests (optional but recommended):**

```bash
cd /opt/pipeline && sudo -u trader env PYTHONPATH=src PIPELINE_DSN="$PIPELINE_DSN_TEST" EMBEDDER=hash MARKETDATA=fake BROKER=fake /opt/pipeline/.venv/bin/python -m pytest tests/integration/test_exit_engine_flow.py -q
```

Expect `12 passed`.

## Rollback (if anything misbehaves)

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.4
sudo systemctl restart c4-exec
```

No migrations, no config changes — nothing else to undo. (Rolling back
also brings the three bugs back, so only do this if v0.12.5 itself
misbehaves, and tell Claude.)
