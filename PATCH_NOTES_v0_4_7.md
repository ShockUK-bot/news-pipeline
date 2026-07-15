# v0.4.7 — A1 pass-through fix: materiality taxonomy, repeat suppression, confidence

**Problem (journal evidence, 2026-07-15, 6 trading hours):** A1 pass-through
79.6% (588 ESCALATE / 151 DISCARD) vs the 15–25% target; A2 (Qwen3-32B,
~3 min/analysis) saturated all day; analyst-queue lag p50 40 min / p95 108 min
vs the 30–60 s intraday budget. One story ("Apple Stock Hits 52-Week High")
re-analyzed ~14 times over 3 h. `confidence` NULL on all TRIAGE rows.
A temporary cron expiring analyst-queue items older than 15 min is in place
and MUST BE REMOVED in this deploy.

## Changes

1. **Tightened A1 materiality prompt** (`src/a1_triage/prompt.py`) — explicit
   Path A catalyst taxonomy (7 classes) and explicit negative categories
   (rating maintenance / PT-only moves / initiations; price-action commentary;
   distant-future scheduled events; sub-materiality micro deals; political/
   macro commentary; routine PR). Four few-shot negatives drawn verbatim from
   the 2026-07-15 evidence. "Can influence investor sentiment" reasoning is
   explicitly banned; recall bias re-scoped to catalyst classes only
   (baseline §4 reconciliation).
2. **Story-level repeat suppression, A1-side** (`src/a1_triage/suppression.py`,
   `service.py`, `config/a1.yaml [suppression]`) — deterministic pre-model
   check: a cluster already triaged inside the 24 h window journals
   `action='SUPPRESS'` (model_id NULL, prior decision referenced) and routes
   nowhere. Bypasses: `is_correction`; held-ticker touch (union of incoming
   symbols and prior verdict tickers vs open positions — A12 mandate);
   independent-outlet count crossing the re-escalate threshold (3).
3. **C2 revision policy** (`src/c2_dedup/cluster.py`, `service.py`) — a
   revision ≥0.90 similar to its own predecessor (same threshold as inter-item
   dedup) is a cosmetic edit: membership/corroboration recorded, NOT forwarded
   to triage — unless a feed-tagged symbol is a held ticker (forwarded for
   A12). Semantically changed revisions forward as before.
4. **Confidence populated** (`schema.py`, `service.py`) — `TriageOutput.confidence`
   (0–1) is REQUIRED (grammar-enforced) and journaled on every TRIAGE row.
   Router lever `router.min_confidence` in `a1.yaml` ships at **0.0 = inactive**;
   set only after observing a day of real distributions.
5. Decision payloads now carry `cluster` state (id, is_new_story,
   independent_outlets) — read back by the corroboration bypass.

## Validation

224/224 tests green against `trading_test` (conftest guard enforced), incl.
new `tests/unit/test_triage_v047.py` (16) and
`tests/integration/test_repeat_suppression.py` (9: suppression, bypasses,
C2 cosmetic-revision policy, confidence journaling, min_confidence lever).

## Deploy (after 16:00 ET, git-native loop)

```bash
cd /opt/pipeline
# 1. apply: unzip the patch over the working tree (or git pull if pushed from dev)
unzip -o news-pipeline-v0_4_7-patch.zip
# 2. test against trading_test (never trading):
export PYTHONPATH=src PIPELINE_DSN=postgresql://trader:<pw>@127.0.0.1:5432/trading_test \
       EMBEDDER=hash MARKETDATA=fake BROKER=fake
.venv/bin/python -m pytest -q          # expect 224 passed
# 3. commit + push (git identity pipeline@local / "Pipeline Build"):
git add -A && git commit -m "v0.4.7: A1 materiality taxonomy, story-level repeat suppression, C2 revision policy, triage confidence" && git push
# 4. restart the two touched services:
sudo systemctl restart a1-triage c2-dedup
# 5. REMOVE THE STOPGAP: delete the temporary 15-min analyst-queue expiry cron
crontab -l | grep -v analyst   # inspect first
crontab -e                     # remove the expiry line added 2026-07-15
# 6. config_version check: journal.config_versions gains the new git SHA on
#    first decision after restart.
```

## Success criteria (next trading day)

- Pass-through in the 15–25% band:
  `SELECT action, count(*) FROM journal.decisions WHERE stage='TRIAGE' AND ts::date=current_date GROUP BY action;`
  (SUPPRESS excluded from the ratio: pass-through = ESCALATE/(ESCALATE+DISCARD).)
- Analyst queue p95 lag < 60 s during RTH.
- No repeat-escalation of unchanged stories (spot-check SUPPRESS rows and
  duplicate-headline ESCALATEs).
- No lost Path A catalysts: spot-check DISCARDs for false negatives, esp.
  rating CHANGES misread as maintenance and small-cap deals where $6M is NOT
  sub-materiality.
- Observe `confidence` distribution before touching `router.min_confidence`.

## Rollback

`git revert` the commit, restart a1-triage + c2-dedup. If suppression alone
misbehaves, `suppression.enabled: false` in a1.yaml is a config-only kill.
