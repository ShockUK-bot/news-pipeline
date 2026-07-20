# v0.11.1 — RSS hotfix: prnewswire 404s painting the whole feed yellow (2026-07-20)

Found from the C6 dashboard showing `ingestion:rss` DEGRADED with
`prnewswire-news: HTTPStatusError("Client error '404 Not Found' ...")`.
Ops/code fix only — one file changed, no schema changes, no new env vars,
no new sudoers.

## Symptom

The `prnewswire-news` feed (`https://www.prnewswire.com/rss/news-releases-list.rss`)
started returning HTTP 404 on every poll. That single feed's failure was
enough to flip the entire `ingestion:rss` health row to DEGRADED (yellow),
even though the other two configured feeds — `globenewswire-public` and
`businesswire-all` — kept working the whole time. Nothing downstream was
actually broken (RSS is Tier 3, lowest priority, and EDGAR/Alpaca were
unaffected), but the dashboard gave no way to tell "one feed is annoyed"
from "this source is actually down."

## Fix

Two independent changes in `src/c1_ingestion/sources/rss.py`:

1. **Realistic User-Agent.** The poller identified itself as the literal
   string `news-pipeline/0.1`. Some wire services quietly 404/403 anything
   that looks like a bot instead of answering honestly — this is the most
   likely explanation for a 404 on a URL that PR Newswire's own site still
   lists as current. The default User-Agent is now a normal browser string.
   No config change needed to pick this up; it's still overridable per
   deployment via a new optional `rss.user_agent` key in `sources.yaml` if
   a specific publisher objects to this one too.
2. **Per-feed health.** Each feed now reports its own status under
   `ingestion:rss:<name>` (e.g. `ingestion:rss:prnewswire-news`) in
   addition to the existing aggregate `ingestion:rss` row. The aggregate
   only goes DEGRADED once **every** configured feed is failing at the
   same time — one dead feed among healthy siblings no longer paints the
   whole component yellow, and the dashboard now shows exactly which named
   feed is unhappy instead of you having to read the truncated error
   detail to figure it out. The existing gap-threshold silence detection
   (`heartbeat.GapMonitor`, which feeds the dead-man ladder) is untouched —
   this only changes the informational per-source dot, not any
   safety-relevant logic.

**Honesty note on the User-Agent fix:** I can't confirm from here whether
PR Newswire's block is actually User-Agent based — it may just be a
retired endpoint. If `ingestion:rss:prnewswire-news` is still showing
DEGRADED a few days after this deploys, the next step is simply dropping
that one feed from `config/sources.yaml` (`rss.feeds`) and relying on the
two working ones — nothing else in the pipeline depends on prnewswire
specifically.

## Changed files

- `src/c1_ingestion/sources/rss.py` (full replacement)

## Tests

No test changes in this patch — this is an operational hotfix on a single
already-covered code path (`normalize_rss` and its tests are untouched).
Verification is manual: watch the dashboard after restart (see deploy
guide Part 4).

## Rollback

`sudo -u trader git -C /opt/pipeline checkout v0.11.0` then
`sudo systemctl restart c1-ingestion`. The old shared-status behavior
comes back; nothing else to undo.
