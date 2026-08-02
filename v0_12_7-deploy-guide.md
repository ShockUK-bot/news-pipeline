# Deploy Guide — v0.12.7 (C7 watchdog)

**What this release does:** adds a watchdog that emails you within
minutes when a pipeline service dies, isn't enabled for reboot, a nightly
job fails, or a heartbeat goes stale — the alarm that was missing during
the week of Jul 28, when four services were silently down for five days.
Full story in `patch-notes-v0_12_7.md`.

**When to do this: any time — nothing restarts.** ~10 minutes. It only
ADDS files and enables one new timer. Safe on a Sunday; safe during
market hours.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_7-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   four folders (`src`, `config`, `ops`, `tests`) and two loose `.md`
   files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** Select the
> `src`, `config`, `ops`, and `tests` folders plus the two `.md` files,
> and drag that whole selection into the upload box. The preview must
> show paths like `src/c7_watchdog/service.py` and
> `ops/systemd/c7-watchdog.timer` — with the folders in front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the four folders + the two `.md` files.
2. **All 8 files are NEW, nothing is replaced:**
   `src/c7_watchdog/__init__.py`, `src/c7_watchdog/service.py`,
   `config/watchdog.yaml`, `ops/systemd/c7-watchdog.service`,
   `ops/systemd/c7-watchdog.timer`, `tests/unit/test_watchdog.py`, plus
   the patch notes and this guide.
3. Commit message: `v0.12.7: C7 watchdog`
4. **Commit changes**, open the commit, confirm **8 changed files** —
   if the number differs, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.6"` →
   `version = "0.12.7"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.7` → title
   `v0.12.7 — C7 watchdog` → **Publish**.

## Part 4 — Pull onto the Spark and install the timer

Paste this whole block:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.7
sudo cp /opt/pipeline/ops/systemd/c7-watchdog.service /etc/systemd/system/
sudo cp /opt/pipeline/ops/systemd/c7-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now c7-watchdog.timer
```

## Part 5 — Verify

**1. The tests** (quick, ~5 seconds):

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_watchdog.py -q'
```

Expect `16 passed`.

**2. Run one pass by hand and read its verdict:**

```bash
sudo systemctl start c7-watchdog.service
sleep 3
sudo journalctl -u c7-watchdog -n 5 --no-pager
```

Look for a `watchdog pass` line with `findings=… critical=… alert=…`.

- `findings=0 … alert=none` — everything healthy, no email (correct:
  a quiet system stays quiet).
- `findings>0 … alert=NEW` — the watchdog found something real on its
  very first pass (e.g. a report timer that was never enabled). **This
  is it working, not failing.** An email arrives within ~5 minutes
  listing each finding with a fix command; paste the email text to
  Claude if anything in it is unclear.

**3. The watchdog's own heartbeat is on the health board:**

```bash
psql "$PIPELINE_DSN" -c "SELECT component, status, detail, updated_ts FROM journal.health WHERE component='watchdog';"
```

(If `psql` says password authentication failed, first run:
`export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"`)

**4. Optional but recommended fire drill (off-hours only):** prove the
end-to-end email path once, on purpose.

```bash
sudo systemctl stop c10-scanner
sudo systemctl start c7-watchdog.service
```

Within ~10 minutes (watchdog pass + next mailer pass) you should receive
`[watchdog] 1 critical / 0 warning — worst: c10-scanner`. Then:

```bash
sudo systemctl start c10-scanner
sudo systemctl start c7-watchdog.service
```

and a `RECOVERED` email follows. Two emails total; the drill is done.

## Part 6 — Reboot survival (the new standing check)

This step now ends every deploy guide, because skipping it is exactly
what caused the Jul 28 outage:

```bash
for u in c7-watchdog.timer c1-ingestion c2-dedup a1-triage c4-exec; do
  echo "$u: $(systemctl is-enabled $u 2>&1)"
done
```

Every line must say `enabled`. Any `disabled` →
`sudo systemctl enable <name>` and re-run the check.

## Rollback

```bash
sudo systemctl disable --now c7-watchdog.timer
sudo -u trader git -C /opt/pipeline checkout v0.12.6
```

Nothing else to undo — no schema change, no service was modified.
