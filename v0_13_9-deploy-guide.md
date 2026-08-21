# Deploy Guide — v0.13.9 (broker refusals become decisions)

**What this release does:** fixes the fault that let four days of short
orders fail silently (Alpaca's 403 refusals became invisible retry loops
instead of journaled rejections). You've already fixed the Alpaca account
— shorts can start filling on the very next signal even before this
deploy — so this release is about making sure nothing like that four-day
silence can ever happen again, plus cleaning up the 7 stuck intents.

**Note:** this replaces the pack briefly named "v0_13_3-pack.zip" earlier
today — that version number was already taken by the RSS release. Delete
that old zip if you still have it; upload THIS one.

No migration, no config changes. Two services restart. ~15 minutes, safe
while live.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_13_9-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `src` and `tests` folders and two loose `.md` files.

## Part 2 — Upload to GitHub (browser, same as always)

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in: the **src** folder, the **tests** folder, and the two **`.md`**
   files.
3. **5 files are REPLACED:**
   - `src/common/broker.py`
   - `src/a3_risk/service.py`
   - `src/c4_exec/reconcile.py`
   - `src/c4_exec/service.py`
   - `tests/unit/test_shorting.py`

   **2 files are NEW:**
   - `patch-notes-v0_13_9.md`
   - `v0_13_9-deploy-guide.md`
4. Commit message: `v0.13.9: broker refusals -> BROKER_REJECT, account
   shorting pre-check (ACCOUNT_NO_SHORTING)`
5. Confirm the commit shows **7 changed files**.

## Part 3 — Version bump + release

1. `pyproject.toml` → edit → `version = "0.13.8"` → `"0.13.9"` → commit.
2. **Releases → Draft a new release** → tag `v0.13.9` → title
   `v0.13.9 — broker refusals become decisions` → **Publish**.

## Part 4 — Pull onto the Spark and restart

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.9
```

("local changes would be overwritten" → stop, paste to Claude.)

```bash
sudo systemctl restart a3-risk c4-exec
```

```bash
for s in a3-risk c4-exec; do echo "$s: $(systemctl is-active $s)"; done
```

Both `active`. C4 reconciles at startup and now publishes the account's
shorting capability. Since you already fixed the Alpaca account, confirm
it reads as ENABLED:

```bash
export PIPELINE_DSN="$(sudo grep -m1 -oE 'postgresql://[^\"]*' /etc/pipeline/pipeline.env)"
psql "$PIPELINE_DSN" -c "
SELECT key, value FROM journal.control
WHERE key IN ('shorting_enabled','account_multiplier','regt_buying_power');"
```

Expected: `shorting_enabled = 1`, `account_multiplier = 2`,
`regt_buying_power` ≈ 177000. If `shorting_enabled = 0`, stop and paste
the output — that would mean the account change didn't stick.

## Part 5 — Clean up the 7 stuck intents (one command)

Their queue messages are already dead-lettered; this corrects the
journal's bookkeeping (new signals mint new intent ids, nothing can
replay):

```bash
psql "$PIPELINE_DSN" -c "
UPDATE journal.intents SET status='REJECTED'
WHERE side='SELL_SHORT' AND status='PENDING';"
```

Expect `UPDATE 7`.

## Part 6 — (Optional) Run the tests on the Spark

```bash
sudo rm -rf /tmp/qdrant-*
```

```bash
cd /opt/pipeline && sudo -u trader .venv/bin/python -m pytest tests/unit -q 2>&1 | tail -3
```

Expect **1 failed, 727 passed** (v0.13.8's 725 + 2 new; the failure is the
pre-existing `test_triage_v047` one).

## Part 7 — The first real short (what to watch)

The account is enabled and the code is honest — the next qualifying
bearish signal should produce the system's first short fill. After each
close:

```bash
psql "$PIPELINE_DSN" -c "
SELECT ts, ticker, stage, action, veto_reason
FROM journal.decisions
WHERE ts > now() - interval '1 day'
  AND (veto_reason IN ('ACCOUNT_NO_SHORTING','BROKER_REJECT')
       OR (stage='ORDER' AND action='FILLED'))
ORDER BY ts DESC LIMIT 20;"
```

When the first short FILLED row appears, run these two and paste both:

```bash
psql "$PIPELINE_DSN" -c "
SELECT position_id, ticker, side, qty_open, avg_entry, initial_stop,
       r_unit, opened_ts
FROM journal.positions WHERE side='SHORT' ORDER BY opened_ts DESC;"
```

```bash
psql "$PIPELINE_DSN" -c "
SELECT o.order_role, o.state, o.qty, o.limit_price, o.stop_price
FROM journal.orders o JOIN journal.positions p USING (position_id)
WHERE p.side='SHORT' ORDER BY o.order_id DESC LIMIT 10;"
```

What correct looks like: `initial_stop` ABOVE `avg_entry` on every short,
`r_unit` positive, and a resting `CATASTROPHE_STOP` order (a BUY stop)
with `stop_price` above the entry. Anything else — paste it immediately.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.8
sudo systemctl restart a3-risk c4-exec
```

(Reverting re-hides broker refusals — only roll back if something is
actually broken, and tell Claude either way.)
