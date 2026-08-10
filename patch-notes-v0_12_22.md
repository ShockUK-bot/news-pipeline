# v0.12.22 — bounded discretion actually turned on (2026-08-10)

## Found via the first live trade of the new era

SNDK opened today — an Argus upgrade caught by v0.12.18's rating-change
class, clustered cleanly under v0.12.19's symbol gate, sized, gated,
filled, and (v0.12.20 proof) both machine invalidations armed, including
`close_below_prenews` at 1228.68. The 1-share size that prompted the
operator's question was CORRECT: SNDK's ATR(14) ≈ $168 on a ~$1,250
stock → 2×ATR stop ≈ $335 → $500 risk budget buys 1.49 → 1 share, no
clip binding, risk above the min-viable floor. Not a bug.

The journal's fallback note, however, exposed a real one.

## Defect 1 — A3's discretion model never ran (config)

The SNDK SIZE decision's adjustments carried `input_value={'material':
False, ... 'stub rule: no trigger'}` — a canned STUB reply.
`config/risk.yaml` shipped with `model.backend: stub` (the dev setting;
A1 and A2 both ship `llamacpp`). So A3's bounded discretion — the model
choice of stop width k, realization fraction, and time window within
config bands — has NEVER executed on the Spark. Every sized trade fell
back to profile defaults. Defaults are sane (which is why nothing broke
visibly), but the feature was dead and every SIZE row carried a noisy
fallback error.

**Fix:** `risk.yaml` → `backend: llamacpp` (endpoint :8081, the Analyst
slot, already configured; A3 runs moments after A2 so the slot is warm).
If the model call ever fails, the fallback to profile defaults still
applies — that safety path is unchanged and now journals cleanly.

## Defect 2 — misleading fallback journaling (cosmetic but costly)

The multiline pydantic dump journaled as the adjustment reason led A13
to tell the operator the error "prevented standard sizing calculations,
leading to the minimal fill" — wrong causality. The reason is now a
single line: `fallback to profile defaults: <first line of error>`.

## Files

REPLACED (2): `config/risk.yaml`, `src/a3_risk/service.py`.
NEW (3): `tests/unit/test_a3_discretion_live.py` (pins backend=llamacpp
in config so this can't silently regress; pins the one-line fallback
reason), these patch notes, the deploy guide.
Plus the pencil edit: `pyproject.toml` version → `0.12.22`.

No migration. One service restart: `a3-risk`.

## What changes in behavior

From the next sized trade, `model_used` should be `true` in the RISK
SIZE payload and `adjustments.reason` should be the model's own
rationale (k possibly ≠ 2.0, within bands k∈[1.5,3.0] etc.). A model
failure still falls back to defaults — same numbers as every trade to
date.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.21
sudo systemctl restart a3-risk
```
