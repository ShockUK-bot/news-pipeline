# Deploy Guide — v0.12.8 (restore A6's event vocabulary)

**What this release does:** fixes the crash the new watchdog caught on
its first pass — the nightly position review has been unable to journal
its verdicts since v0.12.1 because two allowed values were accidentally
deleted from a database constraint. One migration, one test file, no
restarts. Full story in `patch-notes-v0_12_8.md`.

**When to do this: before Monday 19:00 CT** (the next scheduled nightly
review) — and ideally now, because the same missing value sits on the
long lane's target-hit path during market hours. ~10 minutes.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_8-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a
   `schema` folder, a `tests` folder, and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show `schema/migrations/009-restore-event-vocab.sql` and
> `tests/unit/test_schema_vocab.py` — with the folders in front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `schema` folder + `tests` folder + the two `.md` files.
2. **All 4 files are NEW, nothing is replaced.**
3. Commit message: `v0.12.8: restore position_events vocabulary`
4. **Commit changes**, open the commit, confirm **4 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.7"` →
   `version = "0.12.8"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.8` → title
   `v0.12.8 — restore A6 event vocabulary` → **Publish**.

## Part 4 — Pull and apply the migration

Paste this whole block (the export line makes psql work; harmless if you
already ran it today):

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.8
psql "$PIPELINE_DSN" -f /opt/pipeline/schema/migrations/009-restore-event-vocab.sql
psql "$PIPELINE_DSN" -c "SELECT max(schema_version) FROM journal.schema_meta;"
```

The migration prints `BEGIN / ALTER TABLE / ALTER TABLE / INSERT 0 1 /
COMMIT`, and the query must show `9`.

If a test database exists, apply it there too (an error saying the
database does not exist is fine — skip it):

```bash
psql "${PIPELINE_DSN%/*}/trading_test" -f /opt/pipeline/schema/migrations/009-restore-event-vocab.sql
```

## Part 5 — Verify

**1. The new tests (plus the watchdog's, ~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_schema_vocab.py tests/unit/test_watchdog.py -q'
```

Expect `21 passed`.

**2. Re-run the nightly review that's been failing** (safe to run now —
it does exactly what Monday's timer would do; it may take a few minutes
because it consults the model):

```bash
sudo systemctl start a6-nightly
systemctl status a6-nightly --no-pager | head -5
```

`Active:` must end with `(dead)` and the `Process:` line with
`status=0/SUCCESS`. Then see the review it was trying to write all week:

```bash
psql "$PIPELINE_DSN" -c "SELECT event_id, event_type, actor, ts FROM journal.position_events WHERE position_id=5 ORDER BY event_id DESC LIMIT 3;"
```

A fresh `POSITION_REVIEW` row by `A6` should be on top.

**3. Watch the watchdog agree.** Run a pass by hand:

```bash
sudo systemctl start c7-watchdog.service
sleep 3
sudo journalctl -u c7-watchdog -n 3 --no-pager
```

With a6-nightly fixed you should see `findings=0 … alert=RECOVERED` (or
`alert=none` if a recovery was already sent) — and a
`[watchdog] RECOVERED` email within ~5 minutes. That closes the loop:
found by the watchdog, fixed, confirmed by the watchdog.

## Part 6 — Reboot survival (standing check)

Nothing new to enable in this release, so the check is just:

```bash
systemctl is-enabled c7-watchdog.timer a6-nightly.timer
```

Both lines: `enabled`.
