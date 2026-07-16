# Patch a13-chat — A13 Operator Chat (proposed v0.5.0)

New agent: dashboard chat over the journal (trades placed/closed, veto
explanations, decision traces) + advisory ticker reviews against news, with an
operator-confirmed, token-gated file-for-evaluation action that enters the
existing synthetic lane. Full design: `a13-chat-agent-design-v1_0.md`;
dashboard spec: `c6-dashboard-spec-v1_3.md` (both in the project).

## Contents

```
schema/migrations/002-chat.sql     journal v2: chat tables, CHAT stage, dash_chat view
config/a13.yaml                    model / slot-yield / retrieval / filing config
src/a13_chat/                      schema, prompt, retrieval, slot, filing, service
dashboard/chat_api.py              FastAPI router (final-build Postgres dashboard)
dashboard/chat_tab.html            CHAT tab paste-in (markup + CSS + JS)
ops/systemd/a13-chat.service       systemd unit
tests/unit/test_a13_chat.py        15 unit tests (DB-free) — all passing
```

## Deploy (after 16:00 ET, git-native loop)

```bash
# 1. apply: unzip over the working tree (or git pull if pushed from dev)
# 2. migrate + test against trading_test (never trading):
psql "$TEST_DSN" -f schema/migrations/002-chat.sql
PIPELINE_DSN="$TEST_DSN" pytest tests/            # expect 177 + 15
# 3. migrate production journal (brief ACCESS EXCLUSIVE on decisions — RTH-unsafe):
psql "$PIPELINE_DSN_ADMIN" -f schema/migrations/002-chat.sql
# 4. commit + push (pipeline@local / "Pipeline Build")
# 5. install + start the new service; restart the dashboard:
sudo cp ops/systemd/a13-chat.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now a13-chat
sudo systemctl restart c6-dashboard   # after wiring chat_api router + chat tab
# 6. config_version check: journal.config_versions gains the new SHA on the
#    first CHAT decision / service startup.
# 7. smoke: ask "what positions are open?"; file a test ticker on trading_test.
```

## Why not during trading hours

- Migration 002 swaps a CHECK constraint on `journal.decisions` — a brief
  ACCESS EXCLUSIVE lock on the table every stage writes during RTH.
- First-day chat load on the shared Analyst slot (:8081) is unmeasured; the
  yield protocol should protect A2/A12, but prove it on the paper soak, not
  live-session first contact.
- Dashboard-only pieces (router + tab) are intraday-safe in isolation — the
  console is read-only and kill enforcement lives in C4 via the DB flag — but
  they are useless before the migration + service land, so ship it all
  after the close.

## Rollback

`systemctl stop a13-chat`; remove the chat router include + tab from the
dashboard. Chat tables are inert without their writers; no other component
reads them. (Constraint widening can stay — additive.)
