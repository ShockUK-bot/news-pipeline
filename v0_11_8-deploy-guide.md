# Deploy Guide — v0.11.8 (heavy-slot unit into repo + dead-man marketdata off-hours)

**What this does:** (1) puts the corrected `llama-heavy.service` into GitHub so
the heavy-model fix survives future checkouts, and (2) stops the dead-man from
going yellow every night on expected off-hours marketdata staleness.

**No trading-logic changes.** The only restart is `c4-exec`, best at market
close.

---

## Part 1 — Get the pack

Download `v0_11_8-pack.zip`, extract. You'll get `ops` and `src` folders and two
`.md` files.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **ops** and **src** folders and the two `.md` files.
3. **Two files are REPLACED:** `ops/systemd/llama-heavy.service` and
   `src/c4_exec/deadman.py`.
4. Commit message: `v0.11.8: heavy-slot unit fix + dead-man marketdata off-hours`
5. Commit; confirm **4 changed files** (2 replaced + 2 new `.md`).

## Part 3 — Version bump + release

1. `pyproject.toml` → `version = "0.11.7"` → `version = "0.11.8"` → commit to
   `main`.
2. **Releases → Draft a new release** → tag `v0.11.8` → title
   `v0.11.8 — heavy-slot + dead-man marketdata` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.8
```

**About `llama-heavy.service`:** nothing to do on the Spark. Its active copy in
`/etc/systemd/system/` is already the corrected version (you edited it directly
during diagnosis), and it matches this repo copy now. This step just keeps git
in sync. The heavy model already loads — you verified it.

## Part 5 — Activate the dead-man change (at market close)

The dead-man runs inside `c4-exec`, so it picks up the marketdata tweak on the
next restart. Do it **when the market is closed** (C4 reconciles broker state on
boot):

```bash
sudo systemctl restart c4-exec
sudo systemctl status c4-exec --no-pager
```

Give it ~2 minutes (one monitor pass), then confirm the dead-man is green
off-hours:

```bash
psql "$PIPELINE_DSN" -c "SELECT component, status, detail, updated_ts FROM journal.health WHERE component='deadman';"
```

- Off-hours you should now see `deadman | OK | all heartbeats fresh` — no more
  `stale: marketdata`.
- During RTH, if marketdata ever genuinely goes stale, it will still alert and
  block exactly as before — this only changed the off-hours behaviour.

## Confirm it worked

- Dashboard **System health** panel: `deadman` green when the market's closed.
- Heavy slot: tonight's 21:30 A5 run (and tomorrow's 07:00 A4 / reports) should
  log `slot=heavy` instead of `slot=analyst`:
  `sudo journalctl -u a5-thematic -n 30 --no-pager | grep -i slot`

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.7
sudo systemctl restart c4-exec
```

(The heavy-slot `-m` fix on the Spark stays regardless — it's a direct edit in
`/etc/systemd/system/llama-heavy.service`.)
