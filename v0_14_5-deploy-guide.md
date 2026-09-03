# Deploy Guide — v0.14.5 (analyst "thinking out loud" fix + scanner sees big liquid movers)

**What this does — two fixes from one investigation:**

1. **The analyst thinking-leak (the urgent one).** The analyst has been
   failing on about two-thirds of its calls — writing its reasoning as prose
   instead of the structured answer the rest of the system needs — so almost
   nothing became a trade. This is why 2026-09-03 opened zero positions. The
   cause is one missing setting on the analyst's model (and two other
   services on the same model). This adds it, plus an alarm so it can never
   again run a whole day unnoticed.
2. **The scanner's big-liquid-mover blind spot (the original MSTR
   question).** MSTR ran +13.5% on 09-03 and the scanner never flagged it,
   because a name that huge rarely trades at 3× its (already massive) average
   volume — its only row all day was a rejection at 2.94×. This relaxes the
   volume bar for very liquid names and credits their size in the ranking, so
   the next MSTR-type mover is both seen and actionable. All tunable settings,
   nothing hard-coded.

**No database changes. No model change** (just settings on the existing
model). Config + code.

**Time:** about 15 minutes.

**When: any time — but it restarts four live services** (a2-analyst,
a12-guard, a3-risk, c10-scanner), so do it **outside market hours** (after
16:00 ET / before 09:30 ET). The market is closed now, so this evening is
ideal — and the sooner it's in, the sooner tomorrow trades normally.

---

## Part 0 — Confirm the diagnosis (1 minute, optional but reassuring)

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
```

```bash
psql "$PIPELINE_DSN" -c "
SELECT left(payload->>'error',45) AS error, count(*)
FROM journal.decisions
WHERE stage='ANALYST' AND action='REJECT' AND payload ? 'error'
  AND ts >= current_date GROUP BY 1 ORDER BY 2 DESC LIMIT 3;"
```

If you see a pile of `output is not valid JSON: Expecting value: line 1`
rows, that's the bug this fixes. (If the market's been closed a while and
there are no rows today, skip it — the fix is the same.)

## Part 1 — Get the pack onto your PC

1. Download `v0_14_5-pack.zip` from the chat.
2. Right-click → **Extract All** → into a **NEW empty folder**.
3. You'll get a **config** folder, a **src** folder, a **tests** folder,
   and two loose `.md` files.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **config**, **src** and **tests** folders and both `.md`
   files.
3. **Seven files are REPLACED:**
   - `config/a2.yaml`
   - `config/a12.yaml`
   - `config/risk.yaml`
   - `config/scanner.yaml`
   - `src/a2_analyst/service.py`
   - `src/c10_scanner/rules.py`
   - `src/c10_scanner/service.py`

   **Three files are NEW:**
   - `tests/unit/test_v0_14_5.py`
   - `patch-notes-v0_14_5.md`
   - `v0_14_5-deploy-guide.md`
4. Commit message:
   `v0.14.5: analyst disable_thinking + invalid-output alarm; scanner large-cap volume handling (MSTR)`
5. **Commit changes**, then open the commit and confirm **10 changed files**
   (7 replaced + 3 new). A different number → stop, tell Claude.

## Part 3 — Version bump and release

1. `pyproject.toml` → pencil → change `version = "0.14.4"` to
   `version = "0.14.5"` → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.14.5` → title
   `v0.14.5 — analyst thinking-leak fix` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.14.5
```

```bash
sudo -u trader git -C /opt/pipeline describe --tags
```

Expect `v0.14.5`.

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

**Expected:** `2 failed, 767 passed`. **Both failures are pre-existing**,
not from this release:
`test_triage_v047.py::test_confidence_required` (long-standing) and
`test_a7_c5.py::test_render_busy_day_with_narrative` (an A7 rendering issue
already present on v0.14.4). Any OTHER failure → stop and paste it.

Just the new tests:

```bash
env -u PIPELINE_DSN .venv/bin/python -m pytest tests/unit/test_v0_14_5.py -q
```

Expect `16 passed`.

## Part 6 — Restart the four services

```bash
sudo systemctl restart a2-analyst a12-guard a3-risk c10-scanner
```

```bash
for s in a2-analyst a12-guard a3-risk c10-scanner; do echo "$s: $(systemctl is-active $s)"; done
```

All four must say `active`. If any doesn't:

```bash
sudo journalctl -u a2-analyst -n 30 --no-pager
```

…and paste it (swap in whichever unit failed).

## Part 7 — Confirm the fix worked (THE important check — tomorrow after the open)

This is the one verification that matters, because the fix depends on the
model honouring the setting. After ~30–60 minutes of trading tomorrow:

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
```

```bash
psql "$PIPELINE_DSN" -c "
SELECT
  count(*) FILTER (WHERE payload ? 'error') AS invalid,
  count(*) AS total,
  round(100.0*count(*) FILTER (WHERE payload ? 'error')/nullif(count(*),0)) AS pct_invalid
FROM journal.decisions
WHERE stage='ANALYST' AND ts >= current_date;"
```

- **`pct_invalid` near 0–5% → fixed.** The analyst is emitting JSON again;
  yesterday it was ~65%. You should also start seeing THESIS rows and, when
  the gates agree, actual trades.
- **`pct_invalid` still high (50%+) →** the model is ignoring the setting.
  Don't panic and don't roll back — paste this output to Claude; the
  fallback (a prompt-level switch) is a quick follow-up, and the new alarm
  below will already be flagging it.

Also confirm the new alarm is wired (any time):

```bash
psql "$PIPELINE_DSN" -c "
SELECT component, status, left(detail,60) AS detail
FROM journal.health WHERE component='analyst';"
```

Healthy shows `OK  consuming signal.analyst (invalid 0/20)` once it has
samples. If it ever reads `DEGRADED ... check disable_thinking`, that's the
new guard doing its job — the analyst is failing and you'll see it in the
morning briefing banner instead of losing a silent day.

## Part 8 — Confirm the scanner change (next busy day, optional)

On a day a big liquid name makes a real move, this shows the large-cap tier
and the new reject codes at work:

```bash
psql "$PIPELINE_DSN" -c "
SELECT ticker,
       metrics->>'move_pct' AS move, metrics->>'rel_volume' AS relvol,
       metrics->>'score' AS score, status, reject_reason
FROM journal.scanner_candidates
WHERE scan_date = current_date
  AND (metrics->>'adv20_dollars')::numeric >= 1000000000
ORDER BY ts DESC LIMIT 15;"
```

A large-cap at, say, 2.3× relative volume should now show `EMITTED` (or a
score-carrying `CAPPED`) rather than `FILTERED / REL_VOLUME`. If a genuine
mega-cap mover still reads `REL_VOLUME`, paste it back and we'll tune
`large_cap_min_rel_volume` from the number.

## Rollback (only if a service won't start)

```bash
sudo -u trader git -C /opt/pipeline checkout v0.14.4
sudo systemctl restart a2-analyst a12-guard a3-risk c10-scanner
```

(To keep everything but undo just the scanner scoring, the settings at the
bottom of `config/scanner.yaml` revert it without a rollback — see the
patch notes.)
