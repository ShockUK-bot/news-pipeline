# Deploy Guide — v0.12.9 (analyst no-trade verdict + scanner expiry)

**What this release does:** ends the "model output invalid" flood you saw
on the decision tape. The analyst model can now legally answer "nothing
left to trade" (it was being punished for honesty), and week-old scanner
signals get discarded in code instead of burning a minute of model time
each. Full story in `patch-notes-v0_12_9.md`.

**When to do this: today, before Monday's open.** ~10 minutes. Two
services restart (`a1-triage`, `a2-analyst`) — safe while the market is
closed.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_9-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   three folders (`src`, `config`, `tests`) and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show paths like `src/a2_analyst/schema.py` and
> `tests/unit/test_no_trade_expiry.py` — with the folders in front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` + `config` + `tests` folders and the two `.md` files.
2. **Five files are REPLACED:** `src/a2_analyst/schema.py`,
   `src/a2_analyst/prompt.py`, `src/a2_analyst/service.py`,
   `src/a1_triage/service.py`, `config/a1.yaml`. **Three are NEW:** the
   test file, the patch notes, and this guide.
3. Commit message: `v0.12.9: analyst no-trade verdict + scanner expiry`
4. **Commit changes**, open the commit, confirm **8 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.8"` →
   `version = "0.12.9"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.9` → title
   `v0.12.9 — analyst no-trade verdict` → **Publish**.

## Part 4 — Pull, restart the two services

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.9
sudo systemctl restart a1-triage a2-analyst
```

## Part 5 — Verify

**1. The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_no_trade_expiry.py tests/unit/test_analyst_gate.py tests/unit/test_triage_router.py tests/unit/test_scanner.py -q'
```

Expect `89 passed`.

**2. Services healthy after restart:**

```bash
systemctl is-active a1-triage a2-analyst
sudo journalctl -u a2-analyst --since "2 minutes ago" --no-pager | tail -5
```

Both `active`; no tracebacks in the log tail.

**3. Housekeeping — flush the chat message stuck since Jul 16** (found
during today's queue audit; one row, inert):

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
psql "$PIPELINE_DSN" -c "UPDATE queue.messages SET done_ts=now(), last_error='flushed by operator 2026-08-02 (stale since 07-16)' WHERE queue_name='chat.request' AND done_ts IS NULL;"
```

Expect `UPDATE 1`.

**4. The behavioral proof comes from Monday's session.** Two things to
look for in the evening:

```bash
psql "$PIPELINE_DSN" -c "SELECT action, payload->>'no_trade' AS no_trade, count(*) FROM journal.decisions WHERE stage='ANALYST' AND ts::date = current_date GROUP BY 1,2;"
```

- Rows with `action=REJECT, no_trade=true` are the new honest verdicts —
  each cost ONE model call. `REJECT` rows with `no_trade` empty and
  reason "model output invalid" should be rare again — if they're not,
  paste one raw_output to Claude.
- On the dashboard tape, no-trade rejects show the model's actual
  assessment ("analyst no-trade: the move is fully exhausted…") instead
  of "model output invalid after 2 attempts".

And if the scanner queue ever backs up again during downtime, recovery
will show `TRIAGE DISCARD` rows with reason `scanner signal expired`
instead of a REJECT flood — instant, no model time.

## Part 6 — Reboot survival (standing check)

```bash
systemctl is-enabled a1-triage a2-analyst c7-watchdog.timer
```

All three: `enabled`.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.8
sudo systemctl restart a1-triage a2-analyst
```
