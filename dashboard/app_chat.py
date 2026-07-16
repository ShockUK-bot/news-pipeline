"""C6 dashboard entry point WITH the A13 chat routes (v0.5.1).

Zero-edit integration: this wrapper imports the untouched app.py, mounts the
chat router onto it, and re-exports `app`. The systemd unit points uvicorn at
`app_chat:app` instead of `app:app` — app.py and index.html are never edited.

Idempotent: if app.py was already wired manually (an earlier integration
attempt), the router is NOT mounted twice.
"""
from __future__ import annotations

from app import app, _require_user          # the existing dashboard, unchanged
from chat_api import make_chat_router

_already_mounted = any(
    getattr(r, "path", "") == "/api/chat/state" for r in app.routes)

if not _already_mounted:
    app.include_router(make_chat_router(_require_user))
