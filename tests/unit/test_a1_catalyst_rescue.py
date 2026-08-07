"""v0.12.18 unit tests — the news-lane half of the SPCX miss (2026-08-06/07).

Two defects under test, both DB-free:
1. Prompt taxonomy: rating changes and lock-up expirations are explicit
   MATERIAL classes, and a concrete catalyst named inside a price-reaction
   story is no longer neutralized by negative category 2 ("price-action
   commentary" discarded the lockup-expiry item on unlock day).
2. Suppression asymmetry: a DISCARDed cluster's suppress window is a short
   reprint-flood shield (discard_window_hours), not the full 24h — the
   "shares trading higher after Argus upgrade" follow-up was suppressed
   into a cluster discarded long before. ESCALATE priors keep 24h.
"""
import json

from a1_triage.prompt import FEW_SHOT, SYSTEM_PROMPT, build_messages
from a1_triage.suppression import DEFAULTS, discard_cooldown_expired


# ---- prompt: catalyst classes -----------------------------------------------

def test_prompt_names_the_new_material_classes():
    assert "lock-up expirations" in SYSTEM_PROMPT
    assert "index inclusion or exclusion" in SYSTEM_PROMPT
    assert "Analyst rating changes" in SYSTEM_PROMPT
    assert "upgrade or downgrade from a named firm" in SYSTEM_PROMPT


def test_prompt_price_action_has_the_catalyst_exception():
    # The carve-out must live INSIDE negative category 2 and name the shapes
    # that were lost on 2026-08-06.
    assert "EXCEPTION" in SYSTEM_PROMPT
    assert "lock-up expiration" in SYSTEM_PROMPT
    assert "classify by THAT catalyst" in SYSTEM_PROMPT


def test_prompt_keeps_the_anti_passthrough_doctrine():
    # v0.4.7's discipline must survive the v0.12.18 edit — these phrases are
    # the guard against the 79.6%-escalate incident regressing.
    for phrase in ("Price-action commentary", "rating change",
                   "Sub-materiality", "Political/macro commentary",
                   "not a reason"):
        assert phrase in SYSTEM_PROMPT, f"doctrine phrase missing: {phrase}"


def test_few_shot_covers_lockup_reaction_and_upgrade():
    shots = json.dumps(FEW_SHOT)
    assert "lockup expired today" in shots          # price-framed catalyst -> True
    assert "Upgrades Metrix Health" in shots        # true rating change -> True
    material_flags = [out["material"] for _, out in FEW_SHOT]
    assert material_flags.count(False) >= 4          # negatives still dominate


def test_build_messages_carries_all_shots():
    msgs = build_messages({"headline": "x", "item_id": "i"}, {})
    assert len(msgs) == 1 + len(FEW_SHOT) * 2 + 1


# ---- suppression: asymmetric windows ----------------------------------------

def test_defaults_include_discard_window():
    assert DEFAULTS["discard_window_hours"] == 2
    assert DEFAULTS["window_hours"] == 24


def test_stale_discard_prior_reopens_the_story():
    # The Argus-upgrade follow-up shape: DISCARD prior ~20h old -> re-triage.
    assert discard_cooldown_expired("DISCARD", age_hours=20.0,
                                    discard_window_hours=2)


def test_fresh_discard_prior_still_flood_controls_reprints():
    # Benzinga reprints arrive minutes apart -> still suppressed.
    assert not discard_cooldown_expired("DISCARD", age_hours=0.2,
                                        discard_window_hours=2)


def test_escalate_prior_keeps_the_long_window():
    # An analyst already saw the story: repeats add nothing, at any age.
    assert not discard_cooldown_expired("ESCALATE", age_hours=20.0,
                                        discard_window_hours=2)


def test_unknown_age_fails_safe_to_suppression():
    assert not discard_cooldown_expired("DISCARD", age_hours=None,
                                        discard_window_hours=2)
