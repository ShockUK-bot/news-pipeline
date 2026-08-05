# v0.12.14 — scanner ETF guard + scanner-reject counterfactuals (2026-08-04)

## The two findings this fixes (both from the 2026-08-04 review)

**1. The ETF exclusion leaked.** `scanner.exclude_etfs: true` was a
best-effort ticker set, and PTIR + PLTU — 2× leveraged PLTR ETFs, exactly
the "index plumbing, not a story" the spec excludes — slipped through it
on the morning PLTR gapped. They burned 2 of the scanner's 6 daily
emission slots and 2 ~30-second analyst calls, and they dodged the
earnings blackout too (PLTR itself was correctly blacklisted; its
wrappers aren't in the earnings calendar).

**2. Scanner rejects were unmeasurable.** All three 2026-08-04 scanner
candidates died at the analyst with confidence 0.15. Arguably correct —
the scanner prompt treats rejecting exhausted movers as success, and
Monday's FCUV reject was validated by the tape — but there was zero
evidence either way: gate vetoes get counterfactual rows (v0.12.10),
analyst rejects got nothing. The lane's judge was unjudged.

## What it does

**ETF exclusion becomes three layers** (any hit → ETF_EXCLUDED, all
governed by the existing `exclude_etfs` switch):

1. **Official-name check (new, primary):** C10 now fetches each
   candidate's official asset name from the broker's assets API (cached
   per day, one call per new ticker) and runs `looks_like_etf()` — a pure,
   word-bounded detector for "ETF"/"ETN", leverage multipliers ("2X",
   "1.5X", "-1X"), fund words, and known fund-issuer prefixes
   (GraniteShares, Direxion, ProShares, …). "GraniteShares 2x Long PLTR
   Daily ETF" cannot slip past this; "Build-A-Bear Workshop" is untouched
   (word-bounded — no false positive on "Bear").
2. **Extended ticker set (fallback):** the built-in list now includes the
   common single-stock leveraged wrappers (PTIR, PLTU, TSLL, NVDL, CONL,
   MSTU, …) for when the name lookup fails.
3. **Operator denylist (new):** `scanner.etf_denylist: []` in
   `config/scanner.yaml` — your override for anything that ever slips
   again, no release needed.

**Scanner-lane analyst rejects now write counterfactual rows.** An A2
no-trade verdict on a scanner signal records a `journal.gate_counterfactuals`
row (`rule='scanner_reject'`, `veto_reason='ANALYST_REJECT'`) using the
scanner's detection snapshot for the entry-price baseline. The existing
v0.12.10 post-close sweep fills in what the stock did afterwards — no new
table, no migration, and the GATE LAB tab's counterfactual panels pick the
rows up automatically. Bounded by the scanner's own `max_per_day` cap
(≤ 6 rows/day). Best-effort by construction: the recorder never raises,
so measurement can never break the reject path. News-lane rejects stay
unmeasured for now (~150/day would swamp the sweep) — noted as a
follow-up, not smuggled in.

After ~a week, one query (or the GATE LAB tab) answers "is the scanner's
judge too timid?" the same way it answers it for the gate:

```sql
SELECT count(*), round(avg(max_up_pct)*100,2)  AS avg_best_pct,
       round(avg((price_eod-price_at_veto)/price_at_veto)*100,2) AS avg_eod_pct
FROM journal.gate_counterfactuals
WHERE rule='scanner_reject' AND complete;
```

## What it deliberately does NOT change

No thresholds, no prompts, no gate/risk rules. The scanner's judge keeps
its standards; it just gets a scorecard. Same philosophy as v0.12.10/13:
fix the plumbing, measure the judgment, tune from evidence.

## Files

REPLACED (5): `src/c10_scanner/rules.py`, `src/c10_scanner/screener.py`,
`src/c10_scanner/service.py`, `src/a2_analyst/service.py`,
`config/scanner.yaml`.
NEW (3): `tests/unit/test_scanner_etf_guard.py`, these patch notes, the
deploy guide.

Release test set: 80 green (test_scanner_etf_guard, test_scanner,
test_analyst_gate, test_gate_recheck). No migration. Restart:
`c10-scanner` + `a2-analyst`.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.13
sudo systemctl restart c10-scanner a2-analyst
```

(Any scanner_reject counterfactual rows already written are additive and
harmless.)
