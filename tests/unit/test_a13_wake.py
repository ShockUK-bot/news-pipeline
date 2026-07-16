"""v0.5.3 wake-on-demand unit tests — DB-free, injected probe/run/sleep."""
from __future__ import annotations

import pytest

from a13_chat.wake import ensure_awake

MODEL = {"backend": "llamacpp", "endpoint": "http://127.0.0.1:8081"}
WAKE = {"enabled": True, "command": "sudo -n /usr/bin/systemctl start llama-analyst.service",
        "ready_timeout_secs": 30, "poll_secs": 1}


def make_probe(results: list[bool]):
    async def probe(endpoint, path, timeout=3.0):
        return results.pop(0) if results else False
    return probe


async def no_sleep(_secs):
    return None


async def test_fast_path_no_command_when_alive():
    ran = []

    async def run(cmd):
        ran.append(cmd)
        return 0, ""

    alive, note = await ensure_awake(MODEL, WAKE, probe_fn=make_probe([True]),
                                     run_fn=run, sleep_fn=no_sleep)
    assert alive and note is None
    assert ran == []                       # probe-first: never woke


async def test_stub_backend_skips_everything():
    alive, note = await ensure_awake({"backend": "stub"}, WAKE)
    assert alive and note is None


async def test_wake_disabled_dead_server():
    alive, note = await ensure_awake(MODEL, {"enabled": False},
                                     probe_fn=make_probe([False]),
                                     sleep_fn=no_sleep)
    assert not alive and "disabled" in note


async def test_wake_success_reports_note_and_announces():
    announced = []

    async def announce():
        announced.append(True)

    async def run(cmd):
        assert "llama-analyst" in cmd
        return 0, ""

    # dead on first probe, ready on third poll
    alive, note = await ensure_awake(
        MODEL, WAKE, on_wake_start=announce,
        probe_fn=make_probe([False, False, False, True]),
        run_fn=run, sleep_fn=no_sleep)
    assert alive
    assert "woken on demand" in note
    assert announced == [True]


async def test_wake_command_failure():
    async def run(cmd):
        return 1, "a password is required"

    alive, note = await ensure_awake(MODEL, WAKE,
                                     probe_fn=make_probe([False]),
                                     run_fn=run, sleep_fn=no_sleep)
    assert not alive and "exited 1" in note


async def test_wake_timeout_when_model_never_ready():
    async def run(cmd):
        return 0, ""

    cfg = {**WAKE, "ready_timeout_secs": 0.01, "poll_secs": 0.005}
    alive, note = await ensure_awake(MODEL, cfg,
                                     probe_fn=make_probe([False, False, False]),
                                     run_fn=run)
    assert not alive and "not ready after" in note
