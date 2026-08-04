# Deploy Guide — v0.12.13 (pre-market late pass)

**What this release does:** the overnight queue is no longer read just once
a day at 07:00 ET — a new "late pass" keeps checking it every 10 minutes
until the open, so a PLTR-style signal that breaks at 07:46 gets forwarded
to the analyst for the opening bell instead of rotting until tomorrow.
Full story in `patch-notes-v0_12_13.md`.

**When to do this: any time the market is closed.** ~10 minutes. No
migration. No service restarts — one NEW timer to install and enable
(two small copy commands you haven't seen in earlier deploys; they're
spelled out in Part 4).

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_13-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   four folders (`src`, `config`, `tests`, `ops`) and two loose `.md`
   files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show paths like `src/a4_premarket/late.py` and
> `ops/systemd/a4-late.timer` — with the folders in front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` + `config` + `tests` + `ops` folders and the two
   `.md` files.
2. **One file is REPLACED:** `config/a4.yaml`. **Six are NEW:**
   `src/a4_premarket/late.py`, `tests/unit/test_a4_late.py`,
   `ops/systemd/a4-late.service`, `ops/systemd/a4-late.timer`, the patch
   notes, and this guide.
3. Commit message: `v0.12.13: pre-market late pass`
4. **Commit changes**, open the commit, confirm **7 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.12"` →
   `version = "0.12.13"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.13` → title
   `v0.12.13 — pre-market late pass` → **Publish**.

## Part 4 — Pull, install the new timer, enable it

Paste this whole block on the Spark:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.13
sudo cp /opt/pipeline/ops/systemd/a4-late.service /etc/systemd/system/
sudo cp /opt/pipeline/ops/systemd/a4-late.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now a4-late.timer
```

The last command prints one line about creating a symlink — that's it
succeeding.

## Part 5 — Verify

**1. The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_a4_late.py tests/unit/test_a4_premarket.py tests/unit/test_triage_router.py tests/unit/test_a5_thematic.py -q'
```

Expect `50 passed`.

**2. The timer is armed for tomorrow:**

```bash
systemctl list-timers a4-late.timer --no-pager
```

The `NEXT` column shows tomorrow at 06:00 your time (07:00 ET). (If you
deploy during the 06:00–08:59 window itself, it shows the next 10-minute
mark instead — also correct.)

**3. Live safety-guard proof (evening deploys only — recommended):** fire
the pass manually right now:

```bash
sudo systemctl start a4-late.service
sudo journalctl -u a4-late.service --since "2 minutes ago" --no-pager | tail -3
```

Expect a `late pass no-op` line. That is the same-session guard working:
started in the evening, the pass refuses to touch the queue (tomorrow's
open isn't today's), exactly as designed. Tonight's post-close messages
stay ranked by tomorrow's 07:00 sheet, untouched.

**4. No watchdog email** — nothing restarts in this deploy, so the inbox
stays quiet.

## Part 6 — Reboot survival (standing check)

```bash
systemctl is-enabled a4-late.timer a4-premarket.timer
```

Both: `enabled`.

## Part 7 — The behavioral proof: tomorrow morning

From 06:00 your time the timer fires every 10 minutes. In the evening (or
any time after 08:30):

**1. The passes ran:**

```bash
sudo journalctl -u a4-late.service --since today --no-pager | grep -E "late pass|late candidate" | tail -25
```

Early firings show `late pass no-op` (sheet not built yet — correct);
firings after ~06:05 show `late pass done` with a `fresh=` count; any
`late candidate` line is a signal the old code would have missed —
ticker, item, priority.

**2. What got forwarded:**

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
psql "$PIPELINE_DSN" -c "SELECT ts, ticker, left(payload->>'headline',60) AS headline FROM journal.decisions WHERE stage='PREMARKET' AND action='LATE_CANDIDATE' AND ts::date=current_date ORDER BY ts;"
```

And whether the daily budget ever filled up (rows here mean a VERY busy
pre-market — each was journaled visibly instead of dropped silently):

```bash
psql "$PIPELINE_DSN" -c "SELECT count(*) AS over_budget FROM journal.decisions WHERE stage='PREMARKET' AND action='IGNORE' AND payload->>'late'='true' AND ts::date=current_date;"
```

**3. The full chain for any late candidate** — at 09:45 ET (open +
blackout) A2 evaluates it at live prices, then C3 gates it like any other
signal:

```bash
psql "$PIPELINE_DSN" -c "SELECT ts, stage, action, coalesce(veto_reason,'-') AS reason FROM journal.decisions WHERE ticker='PUT-TICKER-HERE' AND ts::date=current_date ORDER BY ts;"
```

**Zero late candidates on a quiet pre-market is normal** — the proof of
life is the `late pass done` log lines. A busy earnings morning is where
it earns its keep.

## Rollback

```bash
sudo systemctl disable --now a4-late.timer
sudo -u trader git -C /opt/pipeline checkout v0.12.12
```

(The timer units can stay in /etc/systemd/system harmlessly, or remove
them with `sudo rm /etc/systemd/system/a4-late.service /etc/systemd/system/a4-late.timer && sudo systemctl daemon-reload`.)
