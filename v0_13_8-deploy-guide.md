# Deploy Guide — v0.13.8 (scanner sees all day + fast lane to the analyst)

**What this does:** fixes everything we found in the MRNA/MSTR review.
After this deploy: the scanner keeps watching (and writing down what it
sees) even after it has used up its daily trades; its daily budget goes up
from 6 to 15 with a 6-per-hour pace so one wild open can't spend it all;
weak candidates and un-shortable down-movers no longer waste slots;
scanner signals jump to the front of the analyst's queue instead of
waiting 20+ minutes behind news; the analyst's queue wait is now recorded
on every decision; and a formatting quirk that was wasting ~10% of the
analyst's busy-morning capacity is fixed.

**No database changes.** Config + code only.

**Time:** about 20 minutes.

**When: OUTSIDE market hours** — this restarts three live services
(a1-triage, a2-analyst, c10-scanner). Market closes **15:00 your time**;
any time this evening after that is perfect, or tomorrow before 08:30.
Do NOT deploy mid-session.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_13_8-pack.zip` from the chat.
2. Right-click → **Extract All** → into a **NEW empty folder**.
3. You'll get a **src** folder, a **config** folder, a **tests** folder,
   and two loose `.md` files.

## Part 2 — Upload to GitHub

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload
   files**.
2. Drag in the **src**, **config** and **tests** folders and both `.md`
   files.
3. **Eight files are REPLACED** (GitHub handles this automatically):
   - `src/c10_scanner/rules.py`
   - `src/c10_scanner/service.py`
   - `src/a1_triage/service.py`
   - `src/a2_analyst/service.py`
   - `src/a2_analyst/schema.py`
   - `src/a2_analyst/prompt.py`
   - `config/scanner.yaml`
   - `config/a1.yaml`

   **Three files are NEW:**
   - `tests/unit/test_v0_13_8.py`
   - `patch-notes-v0_13_8.md`
   - `v0_13_8-deploy-guide.md`
4. Commit message:
   `v0.13.8: scanner observes after cap, budget pacing + prechecks, analyst fast lane (MRNA/MSTR 2026-08-19)`
5. **Commit changes**, then open the commit and confirm it shows
   **11 changed files** (8 replaced + 3 new). A different number → stop,
   tell Claude.

## Part 3 — Version bump and release

1. `pyproject.toml` → pencil icon → change `version = "0.13.7"` to
   `version = "0.13.8"` → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.13.8` → title
   `v0.13.8 — scanner all-day vision + analyst fast lane` → **Publish**.

## Part 4 — Pull onto the Spark

Open a terminal on the Spark, one line at a time:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.8
```

Confirm it took:

```bash
sudo -u trader git -C /opt/pipeline describe --tags
```

Expect `v0.13.8`.

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

**Expected:** `1 failed, 725 passed` — 42 more than v0.13.7. The single
failure should still be the pre-existing
`test_triage_v047.py::test_confidence_required`.

Anything else in the failure list → stop and paste it to Claude.

Just the new tests on their own:

```bash
env -u PIPELINE_DSN .venv/bin/python -m pytest tests/unit/test_v0_13_8.py -q
```

Expect `42 passed`.

## Part 6 — Restart the three services

```bash
sudo systemctl restart a1-triage a2-analyst c10-scanner
```

Then confirm all three came back:

```bash
for s in a1-triage a2-analyst c10-scanner; do echo "$s: $(systemctl is-active $s)"; done
```

All three must say `active`. Anything else:

```bash
sudo journalctl -u c10-scanner -n 30 --no-pager
```

…and paste it to Claude (swap in whichever unit failed).

## Part 7 — Same-evening sanity checks (2 minutes)

The scanner idles outside 09:50–15:15 ET, so tonight you're just checking
clean startups:

```bash
sudo journalctl -u c10-scanner -n 5 --no-pager
```

Expect a `C10 up` line showing `caps=2/scan 15/day`.

```bash
sudo journalctl -u a2-analyst -n 5 --no-pager
```

Expect `A2 up` with no errors after it.

## Part 8 — Next-session behavioural checks (tomorrow, after the open)

1. **The fast lane is real.** After the scanner's first emission of the
   day (if any), run:

   ```bash
   export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
   ```

   ```bash
   psql "$PIPELINE_DSN" -c "
   SELECT ts AT TIME ZONE 'America/New_York' AS et, ticker, action,
          payload->>'origin' AS origin, payload->>'queue_wait_secs' AS wait_s
   FROM journal.decisions
   WHERE stage='ANALYST' AND ts > current_date
   ORDER BY ts DESC LIMIT 10;"
   ```

   Every row now has a `wait_s` number. **Scanner-origin rows should show
   double-digit seconds, not 1,400+** like 08-19.

2. **The scanner never goes dark.** Any afternoon (especially after a busy
   open):

   ```bash
   psql "$PIPELINE_DSN" -c "
   SELECT max(ts) AT TIME ZONE 'America/New_York' AS last_row, count(*)
   FROM journal.scanner_candidates WHERE scan_date = current_date;"
   ```

   `last_row` should track the current time all session — never freeze at
   mid-morning again.

3. **Budget pacing.** On a busy day:

   ```bash
   psql "$PIPELINE_DSN" -c "
   SELECT status, reject_reason, count(*)
   FROM journal.scanner_candidates
   WHERE scan_date = current_date GROUP BY 1,2 ORDER BY 3 DESC;"
   ```

   New codes you may see and what they mean: `SCORE_FLOOR` (too weak to
   spend budget on), `SSR_RESTRICTED` / `SHORT_UNAVAILABLE` (down-mover we
   couldn't short anyway), `PER_HOUR` (pacing working). `EMITTED` should
   no longer be able to hit 6 within the first hour.

Paste all three outputs into the chat tomorrow and I'll read them against
the 08-19 baseline.

## Rollback (if anything goes wrong)

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.7
sudo systemctl restart a1-triage a2-analyst c10-scanner
```
