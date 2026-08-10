# Deploy Guide — v0.12.23 (thesis store: bootstrap mode + wide pass)

**What this release does:** makes the thesis bot actually produce theses.
It has produced none in three weeks — 113 news items in, 113 "ignore"
decisions out — because its instructions told it to attach news to an
*existing* thesis, and there were never any existing theses to attach to.
This release gives it a cold-start mode, feeds the Sunday deep pass a full
week of news instead of the empty queue it has been getting, and raises the
output limit that was cutting its answers in half.

**Risk: low.** Three files replaced, one test file added. No database
migration, no new services, no new passwords, no restarts. If it misbehaves
you are one command away from putting it back.

**Time: ~15 minutes**, plus a 10-minute proof run you can do the same
evening.

**When: any time after 3:00 PM your time (market close).** The proof run
in Part 6 starts the big 122B model, and the house rule is that the heavy
model never runs during market hours.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_23-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `v0_12_23-pack` containing `src`, `config`, `tests` and two loose `.md`
   files.

## Part 2 — Upload to GitHub (browser, same as always)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload
   files**.
2. Drag in the **src**, **config** and **tests** folders and the two `.md`
   files from inside the `v0_12_23-pack` folder.
3. **Three files are REPLACED this time:**
   - `src/a5_thematic/prompt.py`
   - `src/a5_thematic/service.py`
   - `config/a5.yaml`

   One file is NEW: `tests/unit/test_a5_bootstrap.py`. GitHub handles the
   replacements automatically — just don't skip a folder.
4. Commit message:
   `v0.12.23: A5 bootstrap mode + wide pass + truncation fix`
5. **Commit changes**, then open the commit and confirm **6 changed files**
   (3 replaced + 1 new test + 2 new `.md`). Anything missing → stop, tell
   Claude.

## Part 3 — Version bump + release

1. Open `pyproject.toml` → pencil icon → change `version = "0.12.22"` to
   `version = "0.12.23"` → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.12.23` → title
   `v0.12.23 — A5 bootstrap mode + wide pass` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.23
```

If that second command complains about local changes, stop and tell Claude
— it means something on the box was edited by hand.

## Part 5 — Run the test suite

```bash
cd /opt/pipeline
sudo -u trader bash -c 'export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test MARKETDATA=fake BROKER=fake \
  PIPELINE_DSN=postgresql://trader:PASSWORD@127.0.0.1:5432/trading_test && .venv/bin/python -m pytest tests/ -q'
```

(Replace `PASSWORD` as usual.) Expect **0 failed**, and exactly **15 more
passed** than v0.12.22 printed. Qdrant `.lock` errors →
`sudo rm -rf /tmp/qdrant-*` and re-run.

Want just the part that changed? This should print **25 passed**:

```bash
cd /opt/pipeline
sudo -u trader bash -c 'export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test MARKETDATA=fake BROKER=fake \
  PIPELINE_DSN=postgresql://trader:PASSWORD@127.0.0.1:5432/trading_test && .venv/bin/python -m pytest tests/unit/test_a5_bootstrap.py tests/unit/test_a5_thematic.py -q'
```

Any failure → stop, copy the last 30 lines to Claude.

**Nothing needs restarting.** `a5-thematic` is a oneshot timer: it starts a
fresh process on its next run and reads both the new code and the new
config automatically.

---

## Part 6 — Prove it tonight (the whole point of the release)

The big fix lands on a **deep pass**, which normally only happens on
Sunday. Rather than wait six days, force one now. This release added a
`--deep` switch exactly for this.

Run this **after 3:00 PM your time** — one command, copy it whole:

```bash
sudo -u trader bash -c 'set -a; . /etc/pipeline/pipeline.env; set +a; \
  cd /opt/pipeline && PYTHONPATH=/opt/pipeline/src A5_FORCE_DEEP=1 \
  .venv/bin/python -m a5_thematic.service'
```

It will take **5–15 minutes**: it starts the 122B heavy model (that alone
is a few minutes), reads up to 80 news items from the past week, thinks,
writes, and stops the model again. It prints one long `thematic digest
done` line at the end. Leave it alone until it returns to the prompt.

If that command errors immediately with something about
`/etc/pipeline/pipeline.env`, don't fight it — skip to "If you'd rather
not force it" below.

### What you want to see

The final log line should contain **`new_theses=`** followed by a number
**greater than zero**, and `bootstrap=True`. Then check the store:

```bash
sudo -u postgres psql -d trading -c "SELECT thesis_id, direction, confidence, title FROM journal.theses WHERE status='ACTIVE' ORDER BY confidence DESC;"
```

**Rows here = fixed.** Three weeks of zero, ended. You'll also get the
digest email ("Thesis digest 2026-08-10 — N new theses") through the usual
mailer, listing each thesis with its beneficiary tickers.

### If it still says `new_theses=0`

Not a disaster — this release also added the instrumentation to tell us
*why*. Run this and paste me the output:

```bash
sudo -u postgres psql -d trading -c "SELECT payload FROM journal.decisions WHERE stage='THEMATIC' AND action='DIGEST' ORDER BY ts DESC LIMIT 1;"
```

The numbers in there (`ignored_explicit` vs `ignored_unaddressed`,
`wide_items`, `bootstrap`) point straight at which wall we hit, and that
decides whether we go to the hosted-API pass (v0.12.25) or fix the pack
size first. No more guessing.

### If you'd rather not force it

Do nothing. Tonight's normal 20:30 run will use bootstrap mode over the
day's lane items (a smaller sample — it may or may not seed anything), and
**Sunday 2026-08-16 at 20:30** will be the first real deep pass with the
full week window. The fix is in either way; forcing it just gets the answer
five days sooner.

### One thing to expect

If you run the forced pass now, tonight's scheduled 20:30 firing will
quietly no-op — A5 is idempotent per date and today's digest will already
exist. That is normal and correct, not a failure.

---

## Part 7 — What to watch this week

- **Every night 20:30 CT** — the digest email. Once theses exist, evidence
  should start attaching to them (`evidence_attached=` above zero), which
  is the store's memory finally working.
- **The 06:35 CT morning briefing** — its "Standing theses" section has
  been empty since Phase 9 shipped. It should start showing theses with
  beneficiary tickers.
- **The dashboard decision tape** — `THEMATIC / NEW_THESIS` and
  `THEMATIC / EVIDENCE` rows are both new sights.
- **Router thesis matches** — once a thesis has beneficiary tickers, news
  on those names gets escalation priority (`routing.thesis_matches` on
  triage decisions stops being empty). This is the store's first real
  influence on trading, and it is indirect by design: it changes what gets
  looked at, never what gets bought.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.22
```

That's all. No migration to undo, no service to restart, nothing touching
the broker. The thesis store just goes back to sitting empty.
