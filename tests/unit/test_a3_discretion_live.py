"""v0.12.22 unit tests — the dead-discretion config bug (found 2026-08-10).

The SNDK trade's journal showed the A3 discretion model returning a
triage-shaped STUB reply ('stub rule: no trigger'): config/risk.yaml
shipped with model.backend: stub — the dev setting — so bounded
discretion never ran on the Spark; every sized trade silently used
profile defaults. Also: the multiline pydantic dump journaled as the
fallback reason misled the operator (A13 narrated it as the cause of the
1-share size, which was actually correct ATR math).
"""
import os

import yaml

from a3_risk.service import A3Service


CFG = os.path.join(os.path.dirname(__file__), "..", "..", "config",
                   "risk.yaml")


def test_risk_config_uses_the_real_backend():
    with open(CFG) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["backend"] == "llamacpp", (
        "risk.yaml model.backend must be 'llamacpp' — 'stub' is the dev "
        "setting that silently disabled bounded discretion (2026-08-10)")


async def test_fallback_reason_is_single_line(monkeypatch):
    class ExplodingBackend:
        async def complete(self, messages, schema):
            raise ValueError("9 validation errors for RiskAdjustments\nk\n"
                             "  Field required [type=missing, ...]")

    svc = A3Service.__new__(A3Service)          # no ctor: wire only what we use
    svc.backend = ExplodingBackend()
    svc.bands = {"k": [1.5, 3.0], "realization_fraction": [0.5, 0.9],
                 "time_window_sessions": [1, 5]}
    profile = {"initial_stop": {"k": 2.0},
               "realization": {"target_fraction": 0.7}}
    thesis = {"expected_move_window": "2_sessions"}

    adj, model_used = await svc.discretion(thesis, {}, profile)
    assert model_used is False
    assert adj.k == 2.0                          # profile defaults survived
    assert "\n" not in adj.reason                # journal-friendly one-liner
    assert adj.reason.startswith("fallback to profile defaults: "
                                 "9 validation errors")
