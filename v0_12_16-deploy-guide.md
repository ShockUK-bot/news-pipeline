# Deploy Guide — v0.12.16 (quiet budget exhaustion)

**What this release does:** this morning the paced budget worked
perfectly (24 forwards spread 07:10 → 09:20 ET), but the moment the last
slot was spent, ~100 leftover items hit the decision tape as one giant
"budget exhausted" burst. After this release, leftovers are quietly
returned to the overnight queue instead, and tomorrow's 07:00 sheet gives
them its normal ranked look (and journals each one properly). Full story
in `patch-notes-v0_12_16.md`.

**When to do this: any time the market is closed.** ~5 minutes — the
smallest deploy yet: ONE code file, no config changes, no restarts, no
timers, no migration.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_16-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   one folder (`src`) and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDER itself, not its contents.** The preview must show
> the path `src/a4_premarket/late.py` — with the folder in front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` folder and the two `.md` files.
2. **One file is REPLACED:** `src/a4_premarket/late.py`. **Two are
   NEW:** the patch notes and this guide.
3. Commit message: `v0.12.16: quiet budget exhaustion`
4. **Commit changes**, open the commit, confirm **3 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.15"` →
   `version = "0.12.16"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.16` → title
   `v0.12.16 — quiet budget exhaustion` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.16
```

Done — the already-installed timer runs the new code at its next firing.

## Part 5 — Verify

**1. The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_a4_late.py tests/unit/test_a4_premarket.py tests/unit/test_triage_router.py tests/unit/test_a5_thematic.py -q'
```

Expect `57 passed`.

**2. The right version is live:**

```bash
sudo -u trader git -C /opt/pipeline describe --tags
```

Expect `v0.12.16`.

## Part 6 — The behavioral proof: tomorrow morning

Any time after ~08:30 your time:

**1. Pacing unchanged** — forwards still spread across the morning:

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
psql "$PIPELINE_DSN" -c "SELECT ts, ticker FROM journal.decisions WHERE stage='PREMARKET' AND action='LATE_CANDIDATE' AND ts::date=current_date ORDER BY ts;"
```

**2. The burst is gone** — this should now be zero EVERY day, busy or
quiet (yesterday: ~100):

```bash
psql "$PIPELINE_DSN" -c "SELECT count(*) AS exhaustion_burst FROM journal.decisions WHERE stage='PREMARKET' AND action='IGNORE' AND payload->>'late'='true' AND ts::date=current_date;"
```

**3. The leftovers reached tomorrow's sheet instead** — run this the
morning AFTER a busy day and you'll see yesterday's deferred items in the
sheet's below-top-K IGNOREs (with yesterday's headlines), which is their
new, honest disposition:

```bash
sudo journalctl -u a4-late.service --since today --no-pager | grep "late pass done" | tail -5
```

`deferred=` counts now include post-exhaustion items; `over_budget=` no
longer appears in the log line (it can't happen anymore).

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.15
```
