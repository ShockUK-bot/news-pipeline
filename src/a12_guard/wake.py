"""Analyst-slot probe + wake for A12 — the same wake-on-demand discipline the
deployed A13 uses (a13-chat-agent-design §2, v0.5.3): probe /health first;
only if the server is down run the wake command (passwordless sudo limited to
exactly `systemctl start llama-a2.service` via /etc/sudoers.d/a13-wake — A12
runs as the same user and reuses that rule; no new sudoers entry).

Probe-first means the wake command never fires while the server is up, and
the command comes from config, never from model output.
"""
from __future__ import annotations

import asyncio
import shlex

import httpx

from common.log import get_logger, kv

log = get_logger("a12.wake")

DEFAULTS = {
    "enabled": True,
    "probe_timeout_secs": 3.0,
    "command": "sudo systemctl start llama-a2.service",
    "ready_timeout_secs": 240,
    "poll_secs": 5.0,
}


async def probe_health(endpoint: str, timeout: float = 3.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{endpoint.rstrip('/')}/health")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def ensure_model_up(endpoint: str, wake_cfg: dict | None) -> bool:
    """True when the analyst server answers /health, waking it if allowed.
    False means alert-only mode (baseline §11.2: analyst slot down ->
    position-touching news degrades to operator notification)."""
    cfg = {**DEFAULTS, **(wake_cfg or {})}
    if await probe_health(endpoint, float(cfg["probe_timeout_secs"])):
        return True
    if not cfg.get("enabled", True):
        return False

    log.warning("analyst slot down — waking", extra=kv(endpoint=endpoint))
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(str(cfg["command"])),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=30)
    except (OSError, asyncio.TimeoutError) as e:
        log.error("wake command failed", extra=kv(error=repr(e)[:200]))
        return False

    waited = 0.0
    ready_timeout = float(cfg["ready_timeout_secs"])
    poll = float(cfg["poll_secs"])
    while waited < ready_timeout:
        if await probe_health(endpoint, float(cfg["probe_timeout_secs"])):
            log.info("analyst slot awake", extra=kv(after_secs=round(waited, 1)))
            return True
        await asyncio.sleep(poll)
        waited += poll
    log.error("analyst slot did not come up",
              extra=kv(timeout_secs=ready_timeout))
    return False
