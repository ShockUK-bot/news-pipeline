# Deploy Guide — v0.12.6 (scale-out fires once)

**What this release does:** fixes the repeated profit-taking seen on JNJ
right after v0.12.5 went live — the "sell half at target" rule fired every
minute instead of once, selling 18 of 19 shares in five minutes. All sales
were profitable; the bug is that it didn't keep the other half running as
designed. Full story in `patch-notes-v0_12_6.md`.

**When to do this: NOW, during market hours.** ~5 minutes. Only `c4-exec`
restarts. Until this is in, any position that reaches its profit target
will be repeatedly halved instead of scaled out once.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_6-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a
   `src` folder, a `tests` folder, and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** Select the
> `src` folder, the `tests` folder, and the two `.md` files, and drag that
> whole selection into the upload box. The preview must show
> `src/c4_exec/engine.py` and
> `tests/integration/test_exit_engine_flow.py` — with the folders in
> front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files** →
   drag the `src` folder + `tests` folder + the two `.md` files.
2. **Two files are REPLACED:** `src/c4_exec/engine.py`,
   `tests/integration/test_exit_engine_flow.py`. **Two are NEW:** the
   patch notes and this guide.
3. Commit message: `v0.12.6: scale-out fires once`
4. **Commit changes**, open the commit, confirm **4 changed files**.
   Different number — stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.5"` →
   `version = "0.12.6"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.6` → title
   `v0.12.6 — scale-out fires once` → **Publish**.

## Part 4 — Pull and restart (one service)

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.6
sudo systemctl restart c4-exec
```

## Part 5 — Verify

**Right away (2–3 minutes after restart):**

```bash
sudo journalctl -u c4-exec --since "-3 minutes" --no-pager | grep -ci "engine loop error"
```

Must print `0` (same clean state as v0.12.5 — this release must not
regress it).

**The behavioral check** can't be forced without a position at target, so
it's a standing rule for the next scale-out: when a position hits its
profit target you should see exactly ONE `TARGET` row for it in
`journal.exits`, roughly half the shares, and no more. Quick look any
time:

```bash
psql "$PIPELINE_DSN" -c "SELECT position_id, count(*) FROM journal.exits WHERE exit_layer='TARGET' GROUP BY position_id;"
```

Any position with count > 1 after today = tell Claude. (Position 5 will
show 5 — that's today's incident, from before this fix.)

**Tests (optional but recommended):**

```bash
cd /opt/pipeline && sudo -u trader env PYTHONPATH=src PIPELINE_DSN="$PIPELINE_DSN_TEST" EMBEDDER=hash MARKETDATA=fake BROKER=fake /opt/pipeline/.venv/bin/python -m pytest tests/integration/test_exit_engine_flow.py -q
```

Expect `13 passed`.

## Rollback (if anything misbehaves)

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.5
sudo systemctl restart c4-exec
```

Nothing else to undo — but rolling back restores the repeated scale-out
bug, so only if v0.12.6 itself misbehaves, and tell Claude.
