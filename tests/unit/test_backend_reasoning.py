"""v0.11.9 — regression cover for the heavy-slot reasoning_content routing.

The heavy off-hours model (Qwen3.5-122B-A10B on :8084) returns the
schema-constrained answer in `reasoning_content` with an empty `content`.
`_extract_text` must fall back to `reasoning_content` so A4/A5 stop dropping
to deterministic fallback. See claude/patch-notes-v0_11_9.md.
"""
from a1_triage.backends import _extract_text


def test_content_used_when_present():
    assert _extract_text({"content": '{"ok": true}',
                          "reasoning_content": ""}) == '{"ok": true}'


def test_falls_back_to_reasoning_content_when_content_empty():
    # exact shape the heavy slot returns: content == "" , JSON in reasoning
    assert _extract_text({"content": "",
                          "reasoning_content": '{"ok": true}'}) == '{"ok": true}'


def test_content_null_falls_back():
    assert _extract_text({"content": None,
                          "reasoning_content": '{"a": 1}'}) == '{"a": 1}'


def test_whitespace_only_content_falls_back():
    assert _extract_text({"content": "   \n",
                          "reasoning_content": '{"a": 1}'}) == '{"a": 1}'


def test_content_wins_when_both_present():
    # a normal (non-thinking) slot: real answer in content, ignore reasoning
    assert _extract_text({"content": '{"real": 1}',
                          "reasoning_content": '{"scratch": 1}'}) == '{"real": 1}'


def test_both_empty_returns_empty_string():
    # preserves old behaviour: empty -> validate_* fails -> retry/fallback
    assert _extract_text({"content": "", "reasoning_content": ""}) == ""


def test_missing_reasoning_key_returns_empty():
    assert _extract_text({"content": ""}) == ""


def test_missing_both_keys_returns_empty():
    assert _extract_text({}) == ""
