"""Wake-on-demand for the Analyst model server (v0.5.3).

Off-hours the :8081 llama-server may be stopped (the Heavy slot owns the
memory, per baseline §2). When a chat arrives and the server is down, A13
runs a configured wake command — a passwordless-sudo systemctl start of the
`llama-analyst` unit — then polls /health until the model is loaded or the
timeout expires. The operator sees an interim "waking the analyst" note in
the chat instead of a silent pending bubble.

Safety properties:
  * probe-first: if the server is already up (market hours), no command runs;
  * the command is CONFIG, not model output — A13's LLM cannot choose or
    alter it;
  * a failed wake degrades to an ERROR chat row, never an infinite retry.

config/a13.yaml:
    wake:
      enabled: true
      command: "sudo -n /usr/bin/systemctl start llama-analyst.service"
      health_path: "/health"
      ready_timeout_secs: 240     # 32B load time on the Spark + margin
      poll_secs: 3
"""
from __future__ import annotations

import asyncio
import shlex
import time

import httpx

from common.log import get_logger, kv

log = get_logger("a13.wake")


async def _default_probe(endpoint: str, health_path: str,
                         timeout: float = 3.0) -> bool:
    """True iff the model server answers its health check as ready."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(endpoint.rstrip("/") + health_path)
            return resp.status_code == 200
    except Exception:                                     # noqa: BLE001
        return False


async def _default_run(command: str) -> tuple[int, str]:
    """Run the wake command; returns (returncode, stderr snippet)."""
    proc = await asyncio.create_subprocess_exec(
        *shlex.split(command),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    return proc.returncode or 0, (err or b"").decode(errors="replace")[:300]


async def ensure_awake(model_cfg: dict, wake_cfg: dict, on_wake_start=None,
                       probe_fn=None, run_fn=None,
                       sleep_fn=None) -> tuple[bool, str | None]:
    """Make sure the model server is ready. Returns (alive, note).

    note is None on the fast path (already up); otherwise a short human
    string for the answer's caveats or the ERROR row.
    probe_fn / run_fn / sleep_fn are injectable for tests.
    """
    if model_cfg.get("backend") == "stub":
        return True, None

    endpoint = model_cfg["endpoint"]
    health_path = wake_cfg.get("health_path", "/health")
    probe = probe_fn or _default_probe
    run = run_fn or _default_run
    sleep = sleep_fn or asyncio.sleep

    if await probe(endpoint, health_path):
        return True, None                                 # fast path

    if not wake_cfg.get("enabled", False):
        return False, ("analyst model server unreachable and wake-on-demand "
                       "is disabled (config/a13.yaml wake.enabled)")
    command = wake_cfg.get("command")
    if not command:
        return False, "wake-on-demand enabled but wake.command is not set"

    if on_wake_start is not None:
        await on_wake_start()
    log.info("waking analyst model", extra=kv(command=command))
    rc, err = await run(command)
    if rc != 0:
        log.warning("wake command failed", extra=kv(rc=rc, err=err[:120]))
        return False, f"wake command exited {rc}: {err or 'no stderr'}"

    timeout = float(wake_cfg.get("ready_timeout_secs", 240))
    poll = float(wake_cfg.get("poll_secs", 3))
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        await sleep(poll)
        if await probe(endpoint, health_path):
            waited = int(time.monotonic() - t0)
            log.info("analyst model awake", extra=kv(waited_s=waited))
            return True, f"analyst model was asleep; woken on demand ({waited}s to load)"
    return False, (f"wake command ran but the model was not ready after "
                   f"{timeout:g}s — check the llama-analyst service")
