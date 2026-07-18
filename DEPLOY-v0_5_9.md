# Deploy Guide — v0.5.9 (SIP feed + MARKETDATA_MISSING + RSS timestamp fix)

**What this release does:** activates your new Algo Trader Plus subscription
(the code was hardcoded to the free IEX feed — subscribing alone changed
nothing), makes missing market data visible instead of silently vetoing, and
stops globenewswire RSS items being quarantined over their timestamp format.

**When to do this:** any time the market is closed. This weekend is ideal.
Total time: about 15 minutes. Nothing here touches the database schema or
your trading configs.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_5_9-pack.zip` from the chat.
2. Right-click it → **Extract All** → extract to a folder you can find
   (e.g. your Desktop). You'll get a folder `v0_5_9-pack` containing:
   `src`, `tests`, `env.example`, `PATCH_NOTES_v0_5_9.md`, `DEPLOY-v0_5_9.md`.

## Part 2 — Upload to GitHub (browser, same as v0.5.8)

1. Go to `github.com/ShockUK-bot/news-pipeline`.
2. Click **Add file → Upload files**.
3. Drag in, from the extracted folder: the **src** folder, the **tests**
   folder, and the loose files **env.example**, **PATCH_NOTES_v0_5_9.md**,
   **DEPLOY-v0_5_9.md**. (Folder paths are preserved; the seven replaced
   files overwrite the existing ones, the three new files are added.)
4. Commit message: `v0.5.9: SIP feed, MARKETDATA_MISSING veto, RSS timestamp fix`
5. Click **Commit changes**.
6. Right side of the repo page: **Releases → Draft a new release** → tag
   `v0.5.9` on `main` → title `v0.5.9 — SIP data feed + gate visibility` →
   **Publish**.

## Part 3 — Tell the Spark to use the SIP feed

Open a terminal on the Spark (the usual way). Edit the secrets file:

```bash
sudo nano /etc/pipeline/pipeline.env
```

(Type your login password if asked — nothing shows while typing, that's
normal.) Use the arrow keys to go to the line after `ALPACA_SECRET_KEY=...`
and add this new line:

```
ALPACA_FEED=sip
```

Save and exit: press **Ctrl+O**, then **Enter**, then **Ctrl+X**.

## Part 4 — Pull v0.5.9 onto the Spark

```bash
git -C /opt/pipeline fetch --tags
git -C /opt/pipeline checkout v0.5.9
```

You should see it mention switching to `v0.5.9`. If it prints an error about
"local changes would be overwritten", STOP and tell Claude what it says —
don't force anything.

## Part 5 — Run the tests on the Spark

```bash
cd /opt/pipeline
export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test
.venv/bin/python -m pytest tests/unit -q
```

Expect the last line to say **161 passed** (a number of warnings is fine).
If anything says FAILED, stop and paste the output to Claude.

## Part 6 — Restart the pipeline services

```bash
sudo systemctl restart c1-ingestion c2-dedup c8-regime a1-triage a2-analyst c3-gate a3-risk c4-exec
```

Then confirm C3 picked up the new feed:

```bash
sleep 5 && sudo journalctl -u c3-gate -n 30 --no-pager | grep -i feed
```

You should see a line containing `feed=sip`. Also check overall health:

```bash
systemctl is-active c1-ingestion c2-dedup c8-regime a1-triage a2-analyst c3-gate a3-risk c4-exec
```

Every line should say `active`.

## Part 7 — Prove SIP data is really flowing

This asks Alpaca for the same July 16 window we used in the diagnosis — but
now through your subscription:

```bash
sudo -u trader env PYTHONPATH=/opt/pipeline/src bash -c 'set -a; source /etc/pipeline/pipeline.env; set +a; cd /opt/pipeline && .venv/bin/python - <<PY
import asyncio
from datetime import datetime, timezone, timedelta
from common.marketdata import get_marketdata, avg_minute_volume

async def main():
    md = get_marketdata()
    end = datetime(2026, 7, 16, 13, 45, tzinfo=timezone.utc)
    start = end - timedelta(minutes=30)
    for sym in ["VOYA", "UNH", "GE", "ABT", "LMT", "IIIN", "AAPL"]:
        bars = await md.minute_bars(sym, start, end)
        print(sym, "bars:", len(bars), " avg_min_volume:", avg_minute_volume(bars))

asyncio.run(main())
PY'
```

**PASS looks like:** every symbol shows close to 30 bars (was 3–16 on IEX),
and volumes 10–50× bigger (AAPL was ~9,000/min on IEX; consolidated is in the
hundreds of thousands). If instead you see an error mentioning
"subscription" or a 403, the Algo Trader Plus subscription isn't active on
the API keys in pipeline.env — tell Claude.

## Part 8 — Watch Monday's open

1. **Dashboard, vetoed-trades panel:** vetoes should now show real `vol_mult`
   numbers. `MARKETDATA_MISSING` should be rare or absent — if it shows up
   repeatedly, the health panel will also show marketdata DEGRADED and we
   investigate.
2. **A first PASS:** with real volume data, confirming signals can finally
   pass C3. Watch for the first GATE PASS → RISK → ORDER chain.
3. **Quarantine:** globenewswire items should now flow instead of piling into
   quarantine. Spot-check:

```bash
sudo journalctl -u c1-ingestion --since "1 hour ago" --no-pager | grep -c BAD_TIMESTAMP
```

   Expect 0 (or close to it).

4. **End of week**, run this in psql for the veto mix under the new feed:

```sql
SELECT (ts AT TIME ZONE 'America/New_York')::date AS day, veto_reason, count(*)
FROM journal.decisions
WHERE action='VETO' AND ts > now() - interval '7 days'
GROUP BY 1,2 ORDER BY 1,2;
```

Bring the numbers to Claude — that's the data for tuning the gate thresholds
(the config values are still the baseline placeholders, and that tuning is
the designed §14 follow-up now that the gate can finally see).

## Rollback (any time, ~3 minutes)

```bash
sudo nano /etc/pipeline/pipeline.env       # change ALPACA_FEED=sip to iex (or delete the line)
git -C /opt/pipeline checkout v0.5.8
sudo systemctl restart c1-ingestion c2-dedup c8-regime a1-triage a2-analyst c3-gate a3-risk c4-exec
```
