# Deploy Guide — v0.12.11 (extended-hours shadow mode)

**What this release does:** during pre-market (4:00–9:30 ET) and
after-hours (16:00–20:00 ET), the system now evaluates news at arrival
and journals "WOULD_TRADE" verdicts with the real quote and spread — but
places no orders, ever (the shadow branch has no order path in the code).
After a week or two, the counterfactual table tells us whether live
extended-hours trading is worth building. Full story in
`patch-notes-v0_12_11.md`.

> ⚠️ **Deploy v0.12.10 FIRST.** This release uses v0.12.10's code and its
> migration-010 table. If you haven't done the v0.12.10 guide yet, do it
> now, then come straight back here — the two deploys back-to-back take
> ~20 minutes total.

**When to do this: any time the market is closed.** ~10 minutes. No
migration. Three services restart (`a1-triage`, `a2-analyst`, `c3-gate`).

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_11-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   three folders (`src`, `config`, `tests`) and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show paths like `src/router/rules.py` and
> `tests/unit/test_eh_shadow.py` — with the folders in front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` + `config` + `tests` folders and the two `.md` files.
2. **Ten files are REPLACED:** `src/common/clock.py`,
   `src/router/facts.py`, `src/router/rules.py`,
   `src/a1_triage/service.py`, `src/a2_analyst/service.py`,
   `src/c3_gate/rules.py`, `src/c3_gate/service.py`,
   `src/c3_gate/counterfactual.py`, `config/gate.yaml`,
   `config/a1.yaml`. **Three are NEW:** the test file, the patch notes,
   and this guide.
3. Commit message: `v0.12.11: extended-hours shadow mode`
4. **Commit changes**, open the commit, confirm **13 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.10"` →
   `version = "0.12.11"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.11` → title
   `v0.12.11 — extended-hours shadow mode` → **Publish**.

## Part 4 — Pull and restart the three services

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.11
sudo systemctl restart a1-triage a2-analyst c3-gate
```

## Part 5 — Verify

**1. The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_eh_shadow.py tests/unit/test_triage_router.py tests/unit/test_gate_recheck.py tests/unit/test_gate_defer.py tests/unit/test_analyst_gate.py -q'
```

Expect `77 passed`.

**2. Services healthy on the new code:**

```bash
systemctl is-active a1-triage a2-analyst c3-gate
sudo journalctl -u c3-gate --since "2 minutes ago" --no-pager | tail -5
```

All three `active`; no tracebacks.

**3. No watchdog email** (the restart is also a live test of v0.12.10's
transient-state fix — the inbox stays quiet).

## Part 6 — Reboot survival (standing check)

```bash
systemctl is-enabled a1-triage a2-analyst c3-gate
```

All: `enabled`.

## Part 7 — The behavioral proof: the next pre-market session

The first evidence arrives 3:00–8:30 AM your time (pre-market). In the
morning, or any evening after:

**1. Shadow evaluations happened:**

```bash
sudo journalctl -u c3-gate --since today | grep "eh shadow" | tail -20
```

Each line is one extended-hours evaluation: ticker, session (pre/post),
outcome (WOULD_TRADE or the veto reason), and the spread it saw.

**2. The scoreboard:**

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
psql "$PIPELINE_DSN" -c "SELECT action, coalesce(veto_reason,'-') AS reason, count(*) FROM journal.decisions WHERE stage='GATE' AND payload->>'rule'='eh_shadow' AND ts > now() - interval '1 day' GROUP BY 1,2 ORDER BY 3 DESC;"
```

`WOULD_TRADE` rows are the missed-opportunity candidates; the veto mix
shows why the rest died (EH_LIQUIDITY = spread too wide is the expected
big bucket — that's the honest cost of extended hours).

**3. After a few sessions — what the would-trades actually did:**

```bash
psql "$PIPELINE_DSN" -c "SELECT ticker, veto_ts::date AS day, round(pct_move_at_veto*100,2) AS move_at_entry_pct, round(((price_eod-price_at_veto)/price_at_veto)*100,2) AS entry_to_close_pct, round(max_up_pct*100,2) AS best_pct, round(max_down_pct*100,2) AS worst_pct FROM journal.gate_counterfactuals WHERE rule='eh_shadow' AND veto_reason='WOULD_TRADE' AND complete ORDER BY veto_ts DESC LIMIT 20;"
```

Positive `entry_to_close_pct` at scale = extended hours is leaving money
on the table and the live-EH build is justified. Flat/negative = the
shadow just saved us a month of dangerous work. We review after ~2 weeks.

**Zero shadow rows after a busy pre-market?** Tell Claude — first checks
are `grep eh_shadow /opt/pipeline/config/a1.yaml` (toggle present?) and
whether any TRIAGE ESCALATE rows exist for the EH window.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.10
sudo systemctl restart a1-triage a2-analyst c3-gate
```

Or soft-off: set `eh_shadow_enabled: false` in `config/a1.yaml`, then
`sudo systemctl restart a1-triage`.
