# v0.12.25 — BLS feed fix: env-sourced User-Agent + correct URL (2026-08-11)

## The bug (v0.12.24, found on deploy night)

The BLS feed never worked: health DEGRADED, 403 Forbidden. Operator-run
probes from the Spark pinned everything precisely:

- Browser-impersonation UA → **403.** BLS blocks fake browsers — the
  exact opposite policy of the PR wires, which reject honest bots (the
  reason the RSS poller impersonates Chrome at all, v0.11.1).
- Honest UA on the configured URL → **404.** `feed/news_release.rss`
  does not exist. A sweep found the real feeds: `bls_latest`, `empsit`,
  `cpi`, `ppi`, `eci`, `ximpim`, `realer` all answer 200.
- Second probe round, on `bls_latest.rss`: plain product string → 403,
  repo-URL contact → 403, **email contact → 200.** BLS admits only
  clients identifying themselves with real contact info.

## The fix

1. **Per-feed User-Agent, sourced from the environment**
   (`sources/rss.py`): header assembly factored into a pure
   `poll_headers()`. A feed may carry `user_agent` (literal) or
   `user_agent_env` (the NAME of an environment variable; takes
   precedence). The working BLS string contains the operator's email and
   `sources.yaml` sits in a public repo — so the string itself lives in
   `/etc/pipeline/pipeline.env` as `BLS_USER_AGENT`, the same rule-22
   pattern every API key already follows. If the env var is missing, the
   feed sends no override, gets the browser default, and degrades VISIBLY
   on its own health row — loud and harmless. Feeds with neither key
   behave byte-for-byte as before.
2. **Feed corrected** (`config/sources.yaml`): dead `bls-releases`
   replaced by `bls-latest` → `https://www.bls.gov/feed/bls_latest.rss`,
   BLS's all-releases wire (jobs report, CPI, PPI, ECI, import prices —
   one entry per release as published). One feed, full coverage, no
   duplicate entries for dedup to chew. Tier 1, tags `[macro, econ-data]`.

## Files

REPLACED (3): `src/c1_ingestion/sources/rss.py`, `config/sources.yaml`,
`tests/unit/test_macro.py`
NEW (2): `patch-notes-v0_12_25.md`, `v0_12_25-deploy-guide.md`

**5 changed files**, pyproject pencil edit to `0.12.25`. No migration, no
new timers. ONE new env var (`BLS_USER_AGENT` in pipeline.env). One
restart: `c1-ingestion`, outside market hours.

## Tests

`test_macro.py` updated: the feed-registration pin now requires
`bls-latest` to use `user_agent_env: BLS_USER_AGENT`, forbids any email
(`@`) in a git-tracked UA literal, and forbids the three PR wires from
growing any UA key (the regression that would silently re-break them).
`poll_headers()` pinned directly with a monkeypatched environment: env
value wins; unset env + no literal sends NO UA header; unset env falls
through to a literal; literal-only works; keyless feeds emit `{}` plus
conditional-GET headers preserved.

Sandbox: full unit suite **505 passed**, same 2 pre-existing sandbox-only
failures as before the change (identical set).

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.24
sudo systemctl restart c1-ingestion
```

(The BLS_USER_AGENT line in pipeline.env is inert under v0.12.24 — no
need to remove it.)
