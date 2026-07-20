# v0.11.6b — Fold the A13 chat-router mount into app.py

Small follow-up to v0.11.6. **One file: `dashboard/app.py`.**

## Why

v0.11.6 shipped the pipeline-load panel and the queue-prune job. Its `app.py`
did not include the two lines that mount the A13 chat router
(`from chat_api import make_chat_router` + `app.include_router(...)`) — those
existed only as an **uncommitted local edit** on the Spark, which is why the
v0.11.6 checkout was blocked ("local changes to dashboard/app.py would be
overwritten").

Chat was never actually at risk: `app_chat.py` (the service's entry point,
`uvicorn app_chat:app`) mounts the same router idempotently, so chat works with
or without these lines. This release simply folds the local edit into git so
`app.py` is self-contained and the working tree is clean — no more dangling
local change.

## Change

`dashboard/app.py` (REPLACED) — v0.11.6's `app.py` plus, verbatim, the two
lines from the Spark's local edit:

```python
from chat_api import make_chat_router          # with the other imports
...
app.include_router(make_chat_router(_require_user))   # at end of file
```

`app_chat.py` guards on `/api/chat/state`, so when it imports this `app.py`
(which now already mounts chat) it skips its own mount — no double-registration.

## Validation

Diffed against GitHub `v0.11.6`'s `app.py`: the only difference is those two
lines (+ a comment). `app.py` compiles; the dashboard suite passes 9/9,
including the load-panel test.

## What this does NOT touch

Nothing else — `index.html`, the prune job, tests, DB, other services are all
unchanged from v0.11.6.

## Rollback

`git checkout v0.11.6` on the Spark + `sudo systemctl restart c6-dashboard`.
Chat continues to work via `app_chat.py`.
