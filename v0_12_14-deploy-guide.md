# Deploy Guide — v0.12.14 (scanner ETF guard + scanner-reject counterfactuals)

**What this release does:** the scanner stops wasting its daily shots on
leveraged ETFs (the PTIR/PLTU leak from Aug 4), and the analyst's scanner
rejections start getting measured overnight the same way gate vetoes are —
so in a week we'll know from evidence whether conf-0.15 rejections are
wisdom or timidity. Full story in `patch-notes-v0_12_14.md`.

> Deploy AFTER v0.12.13. Order if you're doing both tonight:
> v0.12.13 → v0.12.14.

**When to do this: any time the market is closed.** ~10 minutes. No
migration. Two services restart (`c10-scanner`, `a2-analyst`).

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_14-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   three folders (`src`, `config`, `tests`) and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show paths like `src/c10_scanner/rules.py` — with the folders in
> front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` + `config` + `tests` folders and the two `.md` files.
2. **Five files are REPLACED:** `src/c10_scanner/rules.py`,
   `src/c10_scanner/screener.py`, `src/c10_scanner/service.py`,
   `src/a2_analyst/service.py`, `config/scanner.yaml`. **Three are NEW:**
   `tests/unit/test_scanner_etf_guard.py`, the patch notes, and this
   guide.
3. Commit message: `v0.12.14: scanner ETF guard + scanner-reject counterfactuals`
4. **Commit changes**, open the commit, confirm **8 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.13"` →
   `version = "0.12.14"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.14` → title
   `v0.12.14 — scanner ETF guard` → **Publish**.

## Part 4 — Pull and restart the two services

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.14
sudo systemctl restart c10-scanner a2-analyst
```

## Part 5 — Verify

**1. The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_scanner_etf_guard.py tests/unit/test_scanner.py tests/unit/test_analyst_gate.py tests/unit/test_gate_recheck.py -q'
```

Expect `80 passed`.

**2. Services healthy on the new code:**

```bash
systemctl is-active c10-scanner a2-analyst
sudo journalctl -u c10-scanner -u a2-analyst --since "2 minutes ago" --no-pager | tail -6
```

Both `active`; no tracebacks. (The scanner outside its 09:50–15:15 ET
window just logs quiet idle lines — normal.)

**3. No watchdog email** — the restarts happen while the watchdog's
transient-state fix (v0.12.10) watches, so the inbox stays quiet.

## Part 6 — Reboot survival (standing check)

```bash
systemctl is-enabled c10-scanner a2-analyst
```

Both: `enabled`.

## Part 7 — The behavioral proof: the next session

**1. Leveraged ETFs get named and rejected at the door.** On any day a
leveraged wrapper makes the movers list:

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
psql "$PIPELINE_DSN" -c "SELECT detected_ts, ticker, reject_reason FROM journal.scanner_candidates WHERE reject_reason='ETF_EXCLUDED' AND detected_ts > now() - interval '3 days' ORDER BY detected_ts DESC LIMIT 10;"
```

A PTIR-type ticker showing `ETF_EXCLUDED` here — instead of an EMIT line
in the c3 logs — is the fix working. Zero rows just means no ETFs made
the movers list that day.

**2. Scanner rejects are being measured.** After any day the scanner
emits and the analyst says no:

```bash
psql "$PIPELINE_DSN" -c "SELECT ticker, veto_ts, round(pct_move_at_veto*100,1) AS move_at_detect_pct, complete FROM journal.gate_counterfactuals WHERE rule='scanner_reject' ORDER BY veto_ts DESC LIMIT 10;"
```

Rows appear at reject time (`complete = f`) and flip to `complete = t`
within ~30 minutes after the close — then they carry the answer to "what
did the rejected mover do next?". They also appear on the GATE LAB tab's
counterfactual panels automatically.

**3. The verdict query (after ~a week of rows):**

```bash
psql "$PIPELINE_DSN" -c "SELECT count(*), round(avg(max_up_pct)*100,2) AS avg_best_pct, round(avg((price_eod-price_at_veto)/price_at_veto)*100,2) AS avg_eod_pct FROM journal.gate_counterfactuals WHERE rule='scanner_reject' AND complete;"
```

Big positive `avg_best_pct` = the analyst is rejecting movers that kept
moving — we revisit the scanner prompt/thresholds with evidence. Near
zero or negative = the rejections are earning their keep, exactly as
designed.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.13
sudo systemctl restart c10-scanner a2-analyst
```
