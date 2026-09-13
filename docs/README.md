# docs/

Handoff documents written by Claude Code at the end of each release or working session,
for the operator to upload to his separate design chat.

- `claude_patch-notes-vX_Y_Z.md`: one per release. What changed and why, files touched,
  services restarted, verification results, rollback path.
- `claude_current-state-YYYY-MM-DD.md`: one per working session. Version live, what was done,
  open items, next steps. Older files are never overwritten; a new dated file is added each time.

See the Handoff section of `CLAUDE.md` for the full procedure.

Review documents (`claude_scanner-review-*.md`, `claude_scanner-counterfactual-*.md`) are
one off analyses and follow the same dated naming.

## Replaying a scalp trade

`ops/tools/scalp_replay.py` (v0.14.6) replays the `scalp_v1` exit ladder on the same
1 minute Alpaca bars C4 uses live, for a different stop multiple or a later entry.
Read only. From `/opt/pipeline` with the env sourced:

    export PYTHONPATH=src && set -a && source /etc/pipeline/pipeline.env && set +a
    .venv/bin/python ops/tools/scalp_replay.py --position-id 43
    .venv/bin/python ops/tools/scalp_replay.py --ticker OKLO --date 2026-09-11 \
        --side SHORT --entry-time 08:54 --atr-k 2.0,3.0 --entry-times 09:15,09:30

`--position-id` takes entry, size, ATR and magnitude from the journal; `--ticker` mode
takes them from flags (ATR measured from the bars before entry if not given). Output is
one line per alternative in the same shape as the Part A tables in the counterfactual
review. An alternative entry earlier than the real entry is flagged as possible look ahead.
