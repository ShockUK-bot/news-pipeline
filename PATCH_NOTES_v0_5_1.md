# Patch v0.5.1 — zero-edit dashboard integration for A13 chat

Replaces the manual app.py / index.html wiring from v0.5.0 (B5) with files:
the chat UI is now its own page at **`/chat`** on the dashboard, mounted by a
wrapper module. No hand-editing of any existing file.

## Contents (all under `dashboard/`)

| File | New/Replaces | Purpose |
|---|---|---|
| `chat_api.py` | **replaces** v0.5.0 file | adds `GET /chat` serving the standalone page (API routes unchanged) |
| `chat_page.html` | new | complete self-contained chat page (auth'd, tailnet-only, links back to `/`) |
| `app_chat.py` | new | imports the untouched `app.py`, mounts the chat router (idempotent — safe even if app.py was partially hand-wired), re-exports `app` |

`chat_tab.html` stays in the repo as the optional in-page-tab integration for
anyone who wants it later; it is no longer required.

## Upload via GitHub browser

1. repo → branch selector → create `a13-chat-v0.5.1` from `main`.
2. **Add file → Upload files** → drag the `dashboard` folder (3 files) → commit.
3. PR → verify **2 new + 1 modified**, all under `dashboard/` → merge.
   (Optional: tag `v0.5.1`.)

## Spark deploy (3 commands, no editors)

```bash
# 1. pull as the pipeline user
sudo -u trader git -C /opt/pipeline pull

# 2. point uvicorn at the wrapper (app:app -> app_chat:app) and reload units
sudo sed -i 's|uvicorn app:app|uvicorn app_chat:app|' /etc/systemd/system/c6-dashboard.service
sudo systemctl daemon-reload

# 3. restart and check
sudo systemctl restart c6-dashboard
sudo journalctl -u c6-dashboard -n 20
```

Verify: `curl -s -u "$DASH_USER:$DASH_PASS" http://127.0.0.1:8000/api/chat/state`
returns `{"session_id":null,"messages":[]}` (or your history), then open
**`http://<dashboard-host>:8000/chat`** in the browser (same Basic-auth
session; via tailscale serve use the same hostname as the console, path
`/chat`). Main console at `/` is untouched.

## Rollback

```bash
sudo sed -i 's|uvicorn app_chat:app|uvicorn app:app|' /etc/systemd/system/c6-dashboard.service
sudo systemctl daemon-reload && sudo systemctl restart c6-dashboard
```
