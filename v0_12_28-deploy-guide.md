# Deploy Guide — v0.12.28 (the four WDAY fixes)

**What this release does:** today (2026-08-13) WDAY jumped +25% on a
Reuters takeover report. Your system spotted it perfectly — A1 classified
the catalyst in 30 seconds, A2 wrote a correct thesis while the stock was
*still unmoved* — and then three separate rules stopped it from ever
trading. This release fixes all three, plus the reason the scanner never
even looked at the biggest mover on the tape.

**No database changes, no new timers, no new keys, no new permissions.**
Three services get restarted.

**When to do this: before tomorrow's open.** About 15 minutes. Nothing is
broken right now — the system will keep running fine without this — but
every day you wait is another day the gate cannot act on a
high-impact news story from a wire, which is a large share of what it sees.

If anything in Part 4 or 5 prints something you don't expect, **stop and
paste it to Claude** rather than pressing on. Nothing below is urgent
enough to guess at.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_28-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a
   folder `v0_12_28-pack` containing a `src` folder, a `config` folder, a
   `tests` folder, and two loose `.md` files.

## Part 2 — Upload to GitHub (browser, same as always)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload
   files**.
2. Drag in, from the extracted `v0_12_28-pack` folder: the **src** folder,
   the **config** folder, the **tests** folder, and the two **`.md`**
   files.
3. **Nine files are REPLACED** (GitHub handles this automatically):
   - `config/gate.yaml`
   - `config/scanner.yaml`
   - `src/c3_gate/rules.py`
   - `src/c3_gate/service.py`
   - `src/a2_analyst/service.py`
   - `src/c10_scanner/rules.py`
   - `src/c10_scanner/screener.py`
   - `src/c10_scanner/service.py`
   - `tests/unit/test_gate_recheck.py`

   **Three files are NEW:**
   - `tests/unit/test_wday_v0_12_28.py`
   - `patch-notes-v0_12_28.md`
   - `v0_12_28-deploy-guide.md`
4. Commit message: `v0.12.28: WDAY fixes — cluster-growth credibility, fast
   catalyst path, recheck abandon, scanner universe`
5. **Commit changes**, then open the commit and confirm it shows **12
   changed files** (9 replaced + 3 new). If it shows anything different —
   stop and tell Claude before going further.

## Part 3 — Version bump + release

1. Open `pyproject.toml` in the repo → pencil (edit) icon → change
   `version = "0.12.27"` to `version = "0.12.28"` → **Commit changes** to
   `main`.
2. **Releases → Draft a new release** → tag `v0.12.28` → title
   `v0.12.28 — the four WDAY fixes` → **Publish**.

## Part 4 — Pull onto the Spark and restart

Open a terminal on the Spark and run these one at a time:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.28
```

If that prints an error about "local changes would be overwritten",
**stop** and paste it to Claude — don't force anything.

```bash
sudo systemctl restart c3-gate a2-analyst c10-scanner
```

Then confirm all three came back up:

```bash
for s in c3-gate a2-analyst c10-scanner; do echo "$s: $(systemctl is-active $s)"; done
```

All three should print `active`. If any says `failed`, paste the output of
`sudo journalctl -u <that-service> -n 30 --no-pager` to Claude.

## Part 5 — Confirm the new config actually loaded

This is worth 30 seconds, because a YAML typo is the one failure mode that
doesn't show up as a dead service:

```bash
sudo -u trader python3 -c "
import yaml
g = yaml.safe_load(open('/opt/pipeline/config/gate.yaml'))['gate']
s = yaml.safe_load(open('/opt/pipeline/config/scanner.yaml'))['scanner']
print('cluster_growth   ', g['cluster_growth'])
print('fast_urgency     ', g['fast_urgency'], 'bars', g['fast_confirm_bars'])
print('abandon/max_age  ', g['abandon_when_extended'], g['max_bar_age_secs'])
print('actives legs     ', s['most_actives_by'], 'movers_top', s['movers_top'])
print('derivative filter', s['exclude_derivative_shapes'])
print('news owns->veto  ', s['news_owns_it_until_veto'])
"
```

Expected output:

```
cluster_growth    {'enabled': True, 'items_per_credit': 1, 'max_credit': 1}
fast_urgency      ['high'] bars 1
abandon/max_age   True 120
actives legs      ['volume', 'trades'] movers_top 50
derivative filter True
news owns->veto   True
```

## Part 6 — (Optional but recommended) Run the tests on the Spark

```bash
sudo rm -rf /tmp/qdrant-*
```

(That line is the hard-won one — stale Qdrant locks have produced phantom
failures twice.)

```bash
cd /opt/pipeline && sudo -u trader .venv/bin/python -m pytest tests/unit -q 2>&1 | tail -5
```

Expect **572 passed** and the same small set of pre-existing failures you
already have (the date-drift ones on the housekeeping list). The number
that matters: `tests/unit/test_wday_v0_12_28.py` contributes **54 passes**.
If you see failures naming `test_wday_v0_12_28`, paste them to Claude.

---

## Part 7 — What to watch tomorrow

Run this after the close and paste the output:

```bash
export PIPELINE_DSN="$(sudo grep -m1 -oE 'postgresql://[^\"]*' /etc/pipeline/pipeline.env)"
psql "$PIPELINE_DSN" -c "
SELECT veto_reason, count(*) FROM journal.decisions
WHERE stage='GATE' AND action='VETO' AND ts > current_date
GROUP BY 1 ORDER BY 2 DESC;"
```

```bash
psql "$PIPELINE_DSN" -c "
SELECT status, reject_reason, count(*) FROM journal.scanner_candidates
WHERE scan_date = current_date GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15;"
```

What each result means:

- **CREDIBILITY drops from ~28.** That's fix 1 working. If it collapses to
  near zero, the credit is too generous — the lever is
  `cluster_growth.items_per_credit: 2`, and nothing else.
- **GATE_EXTENDED rises.** Expected, and *good*. Those vetoes were always
  extended-move vetoes; they were just being journaled under whichever
  check happened to run first. The journal is now telling the truth.
- **Real large-cap tickers in `scanner_candidates`**, and a new
  `INSTRUMENT_SHAPE` bucket absorbing rows that used to be PRICE_FLOOR and
  DOLLAR_VOLUME. If you still see warrants like `DAVEW` getting measured,
  tell Claude — the shape filter missed a pattern.
- **Any `STALE_MARKETDATA` row is interesting.** That 3-minute-stale bar
  cache from today is still unexplained, and this veto is how we catch it
  in the act. Paste any you see.

## One thing not to do

Don't tune any threshold off tomorrow's numbers, or off today's. WDAY was
one day, and entering at the price the gate actually saw at 15:05 would
have **lost money** — the trade only existed for about ten minutes. Let the
counterfactual rows accumulate for a week or two first, exactly as with the
scanner-reject verdicts. These fixes buy a shot at this class of setup;
they don't promise the outcome.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.27
sudo systemctl restart c3-gate a2-analyst c10-scanner
```

Or keep the code and switch off any single fix in config — each has its own
switch: `cluster_growth.enabled: false`, `fast_urgency: []`,
`abandon_when_extended: false`, `max_bar_age_secs: 0`,
`most_actives_by: [volume]`, `exclude_derivative_shapes: false`,
`news_owns_it_until_veto: false`. Config edits need a
`sudo systemctl restart c3-gate c10-scanner` to take effect.
