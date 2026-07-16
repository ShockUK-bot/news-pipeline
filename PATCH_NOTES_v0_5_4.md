# Patch v0.5.4 — don't yield the Analyst slot to orphaned queue messages

Observed on the Spark: chat answers delayed ~5 minutes because A13's
slot-courtesy check counted 44 READY messages on `signal.guard` — a queue
with NO consumer until A12 ships (Phase 5). Guard fan-outs for held tickers
accumulate there by design (router rule 1), so A13 yielded to a backlog that
can never drain, burned its full 90 s wait before EACH model call, and only
then proceeded.

Fix: the ready-depth query now ignores messages older than
`slot.ignore_older_than_secs` (default 900 s). A live consumer drains its
queue in seconds, so anything sitting ready for 15+ minutes is orphaned and
must not block chat. Fresh A2/A12 work still always goes first — the
capital-protection ordering is unchanged.

## Contents

| File | Action |
|---|---|
| `src/a13_chat/slot.py` | **replace** — stale-aware ready-depth |
| `config/a13.yaml` | **replace** — `slot.ignore_older_than_secs: 900` |

## Deploy

GitHub: branch `a13-chat-v0.5.4` → upload `src` and `config` folders →
PR (2 modified) → merge. Spark:

```bash
sudo -u trader git -C /opt/pipeline pull
sudo systemctl restart a13-chat
```

Verify: ask a question in the CHAT tab — the journal should show at most a
brief yield (or none) and the answer should land in roughly 30–90 s:

```bash
sudo journalctl -u a13-chat -f
```

## Note for Phase 5

The stale guard messages themselves are left in place (harmless, no longer
counted). When A12 ships, purge the pre-A12 backlog before first start so it
doesn't act on ancient news:
`DELETE FROM queue.messages WHERE queue_name='signal.guard' AND done_ts IS NULL;`
— add this to the A12 deploy notes.
