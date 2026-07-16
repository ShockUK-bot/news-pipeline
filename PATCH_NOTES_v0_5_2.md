# Patch v0.5.2 — CHAT tab on the console (still zero-edit)

Adds the CHAT tab to the C6 console tab bar (LIVE | HISTORY | CHAT) without
editing index.html: `app_chat.py` now re-serves `/` with a small script
appended that creates the tab and embeds the /chat page in place of the
LIVE/HISTORY panels. Local index.html edits (extra buttons/stat cards) are
preserved — the file is read from disk per request, never modified.
`/chat` continues to work standalone; `chat_api.py` is unchanged from v0.5.1.

## Contents

| File | Action |
|---|---|
| `dashboard/app_chat.py` | **replace** — chat-tab injection added |
| `dashboard/chat_page.html` | **replace** — hides its own header link / tightens layout when embedded in the tab |

## Upload via GitHub browser

1. Branch `a13-chat-v0.5.2` from `main` → **Add file → Upload files** → drag
   the `dashboard` folder (2 files) → commit.
2. PR shows **2 modified files**, both under `dashboard/` → merge.

## Spark deploy

```bash
sudo -u trader git -C /opt/pipeline pull
sudo systemctl restart c6-dashboard
```

(No unit changes — ExecStart already runs `app_chat:app`.)

## Verify

Hard-refresh the console (Ctrl+Shift+R). The tab bar shows LIVE | HISTORY |
CHAT; clicking CHAT swaps the panels for the chat, amber-underlined like the
others; LIVE/HISTORY bring the console panels back. Filing still prompts for
the kill token. If the tab ever fails to appear (markup drift), the console
renders exactly as before and chat stays available at `/chat`.

## Rollback

Re-upload the v0.5.1 `app_chat.py` (or `git revert` the merge) and restart —
`/chat` standalone remains the fallback integration.
