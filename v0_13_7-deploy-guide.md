# Deploy Guide — v0.13.7 (fix the two dead A6 timers)

**What this does:** fixes the crash that has been killing both `a6-eod`
(the 14:45 overnight-hold check) and `a6-nightly` (the 19:00 position
review) since short selling went live. One code file replaced, one new
test file.

**No database changes. No config changes. No service restarts.** Both A6
units are timers that run once and exit, so they pick the new code up on
their next fire by themselves.

**Time:** about 15 minutes.

**When:** as soon as you can. `a6-nightly` fires at **19:00 your time** —
if this is deployed before then, tonight's review runs clean with nothing
further from you. `a6-eod` fires at **14:45 your time**; if that has
already passed today, Part 7 re-runs it by hand.

---

## Part 0 — Confirm the diagnosis first (1 minute)

Before changing anything, let's make sure the box agrees with me. Copy this
in one line at a time:

```bash
sudo journalctl -u a6-nightly -n 40 --no-pager | tail -20
```

**What you're looking for:** a `Traceback` ending in

```
AttributeError: 'str' object has no attribute 'get'
```

with `context.py` and `build_pack` in the lines above it.

If you see that — this guide is the fix, carry on to Part 1.
If you see **anything else**, stop and paste what you got to Claude. The
rest of this guide assumes that traceback.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_13_7-pack.zip` from the chat.
2. Right-click → **Extract All** → into a **NEW empty folder**.
3. You'll get `v0_13_7-pack` containing a **src** folder, a **tests**
   folder, and two loose `.md` files.

## Part 2 — Upload to GitHub

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload
   files**.
2. Drag in the **src** folder, the **tests** folder, and both `.md` files.
3. **One file is REPLACED:**
   - `src/a6_position_review/context.py`

   **Three files are NEW:**
   - `tests/unit/test_a6_context_columns.py`
   - `patch-notes-v0_13_7.md`
   - `v0_13_7-deploy-guide.md`
4. Commit message:
   `v0.13.7: fix A6 column-list drift (p.side) — both A6 timers crashed on any open position`
5. **Commit changes**, then open the commit and confirm **4 changed files**
   (1 replaced + 3 new). A different number → stop, tell Claude.

## Part 3 — Version bump and release

1. `pyproject.toml` → pencil icon → change `version = "0.13.6"` to
   `version = "0.13.7"` → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.13.7` → title
   `v0.13.7 — fix A6 column-list drift` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.7
```

Confirm it took:

```bash
sudo -u trader git -C /opt/pipeline describe --tags
```

Expect `v0.13.7`.

## Part 5 — Run the tests

```bash
cd /opt/pipeline
```

```bash
export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test
```

```bash
env -u PIPELINE_DSN .venv/bin/python -m pytest tests/unit -q --deselect tests/unit/test_cik_map.py::test_end_to_end_stored_with_symbols
```

**Expected:** `1 failed, 683 passed` — 4 more than last time. The single
failure should still be the pre-existing
`test_triage_v047.py::test_confidence_required`.

Anything else in the failure list → stop and paste it to Claude.

To see just the new tests on their own:

```bash
env -u PIPELINE_DSN .venv/bin/python -m pytest tests/unit/test_a6_context_columns.py -q
```

Expect `4 passed`.

## Part 6 — Run the nightly review by hand

This is the real proof, and it is safe to run now — A6 only writes journal
rows and (if it has recommendations) one email. It never places orders.

It may take several minutes: it starts the big model, reviews each open
position, then stops the model again.

```bash
sudo systemctl start a6-nightly
```

```bash
systemctl status a6-nightly --no-pager | head -6
```

**What you want:** the `Active:` line ends with `(dead)` and the `Process:`
line ends with `status=0/SUCCESS`.

If it says `status=1/FAILURE`, stop and send Claude the output of:

```bash
sudo journalctl -u a6-nightly -n 40 --no-pager
```

Then look at what it wrote — the review that has been missing all week:

```bash
sudo -u postgres psql -d trading -c "SELECT decision_id, action, ticker, ts FROM journal.decisions WHERE stage='POSITION_REVIEW' AND agent='A6' ORDER BY decision_id DESC LIMIT 5;"
```

You should see fresh rows dated today — a `REVIEW` row plus one row per
position (`HOLD`, `TRIM_RECO`, `EXIT_RECO`, or `STALE_FLAG`).

**Note:** having run it by hand, tonight's 19:00 firing will quietly no-op
— it is idempotent per day. That is correct, not a failure.

## Part 7 — Run the EOD check by hand (only if 14:45 has passed today)

Skip this if it is still before 14:45 — the timer will do it.

```bash
sudo systemctl start a6-eod
```

```bash
systemctl status a6-eod --no-pager | head -6
```

Same success test: `(dead)` and `status=0/SUCCESS`.

The overnight-hold recommendation it produces after the close is stale
advice for today — it is a **recommendation only**, nothing acts on it, and
running it proves the unit works. Tomorrow's 14:45 firing is the one that
matters.

## Part 8 — Confirm with the watchdog

```bash
sudo systemctl start c7-watchdog.service
```

```bash
sleep 3
```

```bash
sudo journalctl -u c7-watchdog -n 3 --no-pager
```

You want `findings=0` and `alert=RECOVERED`, and a **`[watchdog] RECOVERED`
email** within about 5 minutes. That closes the loop: found by the watchdog,
fixed, confirmed by the watchdog.

If `findings=` is not 0, the watchdog has found something else as well —
paste the line to Claude.

## Part 9 — Reboot survival (standing check)

No new units in this release, so this is just the routine confirmation:

```bash
systemctl is-enabled a6-eod.timer a6-nightly.timer c7-watchdog.timer
```

All three lines: `enabled`.

## Part 10 — Tomorrow morning

Two things worth a glance in the 07:35 briefing email:

1. Any **short** position's R-progress now reads **positive when the trade
   is winning**. Before this fix it was printed with the sign flipped, so
   ignore what previous briefings said about short-position R.
2. Positions now show their real side. Before this fix everything said
   `LONG`.

---

## Rollback (if something misbehaves)

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.6
```

This puts the crash back, so only do it if v0.13.7 causes something
unexpected — and tell Claude what.
