# Patch notes v0.18.0 (2026-09-17, 17:07 CT): A9, the weekend review and proposal loop

The second of the three unbuilt agents. Code finds the evidence and writes the proposal, the model only narrates, the operator approves. Nothing is ever applied automatically.

## What it does (`a9-review.timer`, Saturday 09:00 CT)

1. **Evaluate**: every APPROVED proposal older than six days gets a verdict (MET, NOT_MET) against its success metric from A11's weekly rollups; status becomes EVALUATED with the verdict in `evaluation`.
2. **Evidence pack** from the last four weeks: per lane trades, winners, sum R and P&L; exit efficiency per exit layer; guard hold saves and shakeouts counted by position (so one bleeding position cannot dominate); direction adjusted veto counterfactuals per veto reason; scanner exit ladder variants; burst fade at the realistic entry; the weekly rollups.
3. **Candidate rules** (`src/a9_review/candidates.py`), each with a minimum sample below which it is a WATCH item with its count, not a proposal:
   - a lane with 10 or more trades at −3R or worse in the window: halve its risk;
   - a veto reason with 20 or more measured counterfactuals averaging +1.5 percent best and +0.5 percent to the close in its direction: shadow or loosen it;
   - stop exits with efficiency at or below −0.5 over 10 or more: review the initial stop, with the 3.0 ATR variant evidence attached;
   - guard hold shakeouts on 65 percent or more of 12 or more classified positions: lean tighten on under water positions;
   - scanner no scale out: 30 or more journaled trades and the full size trail ahead: drop the scale out;
   - burst fade: 200 realistic rows at +0.15 percent after cost: build the lane.
   At most three proposals a week; a rule with a proposal already open is skipped.
4. **Narrative**: the heavy slot (A7's slot manager, autostart on a Saturday morning, analyst fallback) writes a two to four sentence rationale and a risk line per proposal from the evidence only. Ships without it.
5. **Journal and email**: `journal.proposals` (status PROPOSED, evidence includes the rule name), a SYSTEM/A9 REVIEW decision, one email through `journal.outbox` kind `WEEKEND_REVIEW` (migration 018 widens the outbox kind check). Heartbeat `review`.

## Operator loop

The email lists each proposal by number. Reply to Claude Code "approve proposal N" or "reject proposal N". On approval Claude Code applies the change as a normal release and records it:

```
python -m a9_review.service --approve N --config-version <commit hash>
python -m a9_review.service --reject N
```

The next Saturday A9 evaluates it. `--dry-run` prints the review without journaling or calling a model.

## Files

`src/a9_review/{__init__,candidates,narrative,service}.py`, `config/a9.yaml`, `schema/migrations/018-outbox-weekend-review.sql` (applied 17:07 CT), `ops/systemd/a9-review.service` and `.timer` (installed, enabled, next run Saturday 2026-09-19 09:00 CT), `config/watchdog.yaml` (timer and `review` heartbeat), `tests/unit/test_v0_18_0.py`, `CLAUDE.md`.

## First run, 17:07 CT

No proposal: nothing crossed its evidence bar. Watch list: scanner no scale out 17 of 30 (noscale versus base +425), stop exits 7 of 10 measured, guard hold bias 10 of 12 positions, burst fade 0 of 200 realistic rows (the realistic scoring starts tomorrow), no lane at −3R over 10 trades in the window (news is −1.4R over 14, thesis −1.8R over 3), no veto reason over the money left bar. Email queued (outbox 205), sent by the mailer on its next run. Suite 868 passed.

## Rollback

Disable `a9-review.timer`. The proposals table and the outbox kind stay; nothing trades from them.
