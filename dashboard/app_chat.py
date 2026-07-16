"""C6 dashboard entry point WITH the A13 chat routes + CHAT tab (v0.5.2).

Zero-edit integration: imports the untouched app.py, mounts the chat router,
and re-serves `/` with a small script appended that adds a CHAT tab to the
console's tab bar (next to LIVE | HISTORY). The tab embeds the /chat page in
place of the LIVE/HISTORY panels; clicking LIVE or HISTORY returns to them.

index.html is read from disk per request and never modified — local edits to
it (extra buttons, stat cards) survive untouched. If the index route or file
can't be found, the console is served exactly as before and chat remains
available at /chat.

The systemd unit points uvicorn at `app_chat:app`.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends
from fastapi.responses import HTMLResponse

from app import app, _require_user          # the existing dashboard, unchanged
from chat_api import make_chat_router

# -- 1. mount the chat API + /chat page (idempotent) --------------------------

if not any(getattr(r, "path", "") == "/api/chat/state" for r in app.routes):
    app.include_router(make_chat_router(_require_user))

# -- 2. CHAT tab injection ----------------------------------------------------

_INDEX = Path(__file__).parent / "index.html"

# Matches the reference index.html mechanics exactly:
#   tabs bar  = <div class="tabs"> with #tabLive / #tabHist ('on' = active)
#   panels    = #live / #hist toggled via style.display
# Defensive: bails silently if any of those are missing or already injected.
_CHAT_TAB_SNIPPET = """
<script>
(function(){
  try{
    var tabs=document.querySelector('.tabs');
    var live=document.getElementById('live'), hist=document.getElementById('hist');
    var tabLive=document.getElementById('tabLive'), tabHist=document.getElementById('tabHist');
    if(!tabs||!live||!hist||!tabLive||!tabHist||document.getElementById('tabChat'))return;

    var btn=document.createElement('button');
    btn.id='tabChat'; btn.textContent='CHAT';
    tabs.appendChild(btn);

    var panel=document.createElement('div');
    panel.id='chatPanel'; panel.style.display='none';
    hist.parentNode.insertBefore(panel, hist.nextSibling);
    var frame=null;   // created on first open, so the console loads untouched

    function showChat(){
      if(!frame){
        frame=document.createElement('iframe');
        frame.src='/chat';
        frame.style.cssText='width:100%;height:calc(100vh - 230px);min-height:480px;'+
          'border:1px solid var(--line);border-radius:8px;background:var(--bg)';
        panel.appendChild(frame);
      }
      live.style.display='none'; hist.style.display='none'; panel.style.display='';
      btn.classList.add('on'); tabLive.classList.remove('on'); tabHist.classList.remove('on');
    }
    function hideChat(){
      panel.style.display='none'; btn.classList.remove('on');
    }
    btn.onclick=showChat;
    tabLive.addEventListener('click',hideChat);
    tabHist.addEventListener('click',hideChat);
  }catch(e){/* console still fully usable; chat remains at /chat */}
})();
</script>
"""


def _find_index_route():
    for r in list(app.router.routes):
        if getattr(r, "path", None) == "/" and "GET" in (getattr(r, "methods", None) or set()):
            return r
    return None


_orig_index = _find_index_route()

if _orig_index is not None and _INDEX.exists():
    app.router.routes.remove(_orig_index)

    @app.get("/", response_class=HTMLResponse)
    async def index_with_chat_tab(user: str = Depends(_require_user)) -> str:
        html = _INDEX.read_text(encoding="utf-8")
        if "</body>" in html:
            return html.replace("</body>", _CHAT_TAB_SNIPPET + "</body>", 1)
        return html + _CHAT_TAB_SNIPPET
