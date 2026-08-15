# Deploy Guide — v0.13.0 (short selling, shadow-first)

**What this release does:** the system learns to short. Bearish theses —
the kind that cost the most in measured missed profit — now run the whole
pipeline on all three lanes. It ships in **SHADOW mode: zero short orders
can be placed** until you flip one config line. Deploying this is no
riskier than a normal patch; the new code path journals instead of trading.

**This release HAS a database migration** (013 — additive only, no data
touched) and one new config file. Eight services get restarted.

**When to do this: any evening, before the next open.** About 25 minutes.

If anything in Part 4, 5 or 6 prints something you don't expect, **stop
and paste it to Claude** rather than pressing on.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_13_0-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `src`, `config`, `schema`, `dashboard`, `ops`, `tests` folders and two
   loose `.md` files.

## Part 2 — Upload to GitHub (browser, same as always)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload
   files**.
2. Drag in ALL of: the **src**, **config**, **schema**, **dashboard**,
   **ops**, **tests** folders and the two **`.md`** files.
3. **35 files are REPLACED** (GitHub handles this automatically):
   - `config/scanner.yaml`
   - `dashboard/app.py`
   - `ops/snapshot_nav.py`
   - `src/a12_guard/context.py`, `src/a12_guard/prompt.py`,
     `src/a12_guard/service.py`
   - `src/a13_chat/prompt.py`
   - `src/a2_analyst/prompt.py`
   - `src/a3_risk/service.py`, `src/a3_risk/sizing.py`
   - `src/a4_premarket/prompt.py`
   - `src/a5_thematic/prompt.py`
   - `src/a6_position_review/context.py`, `src/a6_position_review/prompt.py`
   - `src/a7_report/facts.py`, `src/a7_report/narrative.py`
   - `src/a8_briefing/facts.py`, `src/a8_briefing/narrative.py`
   - `src/c10_scanner/rules.py`, `src/c10_scanner/screener.py`,
     `src/c10_scanner/service.py`
   - `src/c3_gate/rules.py`, `src/c3_gate/service.py`
   - `src/c4_exec/engine.py`, `src/c4_exec/exits.py`,
     `src/c4_exec/mechanics.py`, `src/c4_exec/overnight.py`,
     `src/c4_exec/reconcile.py`, `src/c4_exec/service.py`,
     `src/c4_exec/state.py`
   - `src/common/broker.py`, `src/common/invalidation_dsl.py`
   - `tests/unit/test_a12_guard.py`, `tests/unit/test_analyst_gate.py`,
     `tests/unit/test_schema_vocab.py`

   **7 files are NEW:**
   - `config/shorting.yaml`            ← the switch lives here
   - `schema/migrations/013-shorting.sql`
   - `src/common/direction.py`         ← all directional math, one place
   - `src/common/assets.py`            ← the borrow-status client
   - `tests/unit/test_shorting.py`
   - `patch-notes-v0_13_0.md`
   - `v0_13_0-deploy-guide.md`
4. Commit message: `v0.13.0: short selling — direction gate, side-aware
   sizing/exec/exits, shadow mode`
5. **Commit changes**, then open the commit and confirm it shows **42
   changed files**. Anything different — stop and tell Claude.

## Part 3 — Version bump + release

1. Open `pyproject.toml` in the repo → pencil (edit) icon → change
   `version = "0.12.28"` to `version = "0.13.0"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.13.0` → title
   `v0.13.0 — short selling (shadow-first)` → **Publish**.

## Part 4 — Pull onto the Spark, migrate, restart

Open a terminal on the Spark and run these one at a time:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.0
```

If that prints "local changes would be overwritten", **stop** and paste it
to Claude — don't force anything.

Now the migration — test database first, then live. `ON_ERROR_STOP` means
a failure stops immediately instead of half-applying:

```bash
sudo -u postgres psql -d trading_test -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/013-shorting.sql
```

```bash
sudo -u postgres psql -d trading -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/013-shorting.sql
```

Each should end with `COMMIT`. Any ERROR line — stop, paste it to Claude.

Then restart the services that load the new code:

```bash
sudo systemctl restart c3-gate a3-risk c4-exec c10-scanner a2-analyst a12-guard a13-chat c6-dashboard
```

And confirm they all came back:

```bash
for s in c3-gate a3-risk c4-exec c10-scanner a2-analyst a12-guard a13-chat c6-dashboard; do echo "$s: $(systemctl is-active $s)"; done
```

All eight should print `active`. If any says `failed`, paste
`sudo journalctl -u <that-service> -n 30 --no-pager` to Claude.

## Part 5 — Confirm migration + config actually loaded

```bash
sudo -u postgres psql -d trading -c "
SELECT (SELECT max(schema_version) FROM journal.schema_meta) AS schema_v,
       (SELECT count(*) FROM journal.positions WHERE side IS NULL) AS null_sides;"
```

Expected: `schema_v` = **13**, `null_sides` = **0** (every historical
position backfilled LONG).

```bash
sudo -u trader python3 -c "
import yaml
s = yaml.safe_load(open('/opt/pipeline/config/shorting.yaml'))['shorting']
sc = yaml.safe_load(open('/opt/pipeline/config/scanner.yaml'))['scanner']
print('mode        ', s['mode'])
print('lanes       ', s['lanes'])
print('etb/ssr     ', s['etb_only'], s['ssr_veto'])
print('caps        ', s['max_short_heat_pct'], s['max_gross_short_notional_pct'])
print('losers leg  ', sc['include_losers'])
"
```

Expected output:

```
mode         shadow
lanes        {'news_short': True, 'news_long': True, 'scanner': True}
etb/ssr      True True
caps         0.015 0.3
losers leg   True
```

**`mode: shadow` is the line that guarantees zero short orders.**

## Part 6 — Run the tests on the Spark

```bash
sudo rm -rf /tmp/qdrant-*
```

(the hard-won line — stale Qdrant locks have produced phantom failures
twice)

```bash
cd /opt/pipeline && sudo -u trader .venv/bin/python -m pytest tests/unit -q 2>&1 | tail -5
```

Expect **611 passed** plus the same pre-existing failures you already have
(the date-drift housekeeping set). The new `tests/unit/test_shorting.py`
contributes **39 passes**. Any failure naming `test_shorting` — paste it
to Claude.

---

## Part 7 — What to watch (the shadow week)

Bearish theses now flow PAST the old first-check veto, so the journal
changes shape immediately. After each close, run:

```bash
export PIPELINE_DSN="$(sudo grep -m1 -oE 'postgresql://[^\"]*' /etc/pipeline/pipeline.env)"
psql "$PIPELINE_DSN" -c "
SELECT action, veto_reason, count(*) FROM journal.decisions
WHERE ts > current_date AND (veto_reason IN ('LONG_ONLY','SHORT_UNAVAILABLE','SSR_RESTRICTED','EX_DIVIDEND')
       OR action = 'SHADOW_SHORT')
GROUP BY 1,2 ORDER BY 3 DESC;"
```

```bash
psql "$PIPELINE_DSN" -c "
SELECT ticker, veto_ts, price_at_veto, price_30m, price_eod, max_down_pct
FROM journal.gate_counterfactuals
WHERE rule='shadow_short' ORDER BY veto_ts DESC LIMIT 20;"
```

And the sign-sanity check — this should ALWAYS return zero rows:

```bash
psql "$PIPELINE_DSN" -c "
SELECT d.decision_id, d.ticker
FROM journal.decisions d
WHERE d.action='SHADOW_SHORT'
  AND ((d.payload->'exit_policy'->'initial_stop'->>'price')::numeric
       <= (d.payload->'sizing'->>'limit_price')::numeric);"
```

What the results mean:

- **`SHADOW_SHORT` rows appearing** = the pipeline is sizing real shorts
  end-to-end. Each one's counterfactual row gets priced by the existing
  sweep ~20 min after the close.
- **`LONG_ONLY` should drop to near zero** (it now only means "lane
  off"). `SHORT_UNAVAILABLE` rows measure exactly what the ETB-only
  policy costs; `SSR_RESTRICTED` measures the SSR veto.
- **The sign-sanity query returning ANY row is a stop-everything bug** —
  a short whose stop isn't above its entry. Paste it immediately.
- Scanner: expect down-mover tickers in `scanner_candidates` for the
  first time.

## Part 8 — Flipping to LIVE (when the data says so — NOT deploy day)

Suggested bar, ~5+ trading days of shadow: at least ~10 priced
`shadow_short` rows; positive average outcome using `price_30m`/`price_eod`
vs `price_at_veto` (for a short, DOWN is profit); zero sign-sanity hits;
`SHORT_UNAVAILABLE` not eating the signals you actually wanted. Then:

```bash
sudo nano /opt/pipeline/config/shorting.yaml
```

Change `mode: shadow` to `mode: live` (and, if you want to stage it, set
`news_long: false` / `scanner: false` to keep those lanes shadowing).
Save, then:

```bash
sudo systemctl restart c3-gate a3-risk c4-exec
```

That's the entire go-live. To pause shorts at any time: set
`mode: shadow` back and restart the same three services.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.28
sudo systemctl restart c3-gate a3-risk c4-exec c10-scanner a2-analyst a12-guard a13-chat c6-dashboard
```

Migration 013 is additive-only and can safely stay in place under
v0.12.28 (old code never reads the new column; the intents CHECK still
accepts BUY/SELL). Config-level alternatives, no rollback needed: any
`shorting.lanes` flag `false` turns one lane long-only;
`scanner.include_losers: false` stops the loser leg; deleting the
`shorting:` block's `mode` line (or setting `mode: off`) restores
long-only behavior everywhere, exact veto strings included.

## One thing not to do

Don't judge the short book on its first day or two of shadow rows —
that's the same one-day trap as WDAY. Let the sweep price a real sample
first. Shadow mode exists precisely so patience is free.
