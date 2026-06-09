"""Unit tests for idle gating + wake-prompt composition."""

from __future__ import annotations

import time

from dlb_launcher.wake import IdleTracker, compose_wake_prompt

# ── IdleTracker ───────────────────────────────────────────────────────────────


def test_idle_tracker_starts_not_idle() -> None:
    """Within the threshold of construction → not idle yet."""
    t = IdleTracker(idle_threshold_s=0.5)
    assert t.is_idle() is False


def test_idle_tracker_becomes_idle_even_without_output() -> None:
    """A child that prints nothing should still be considered idle after
    the threshold elapses — otherwise we'd never inject into a quiet CLI."""
    t = IdleTracker(idle_threshold_s=0.05)
    time.sleep(0.1)
    assert t.is_idle() is True


def test_idle_tracker_becomes_idle_after_threshold() -> None:
    t = IdleTracker(idle_threshold_s=0.05)
    t.mark_output()
    assert t.is_idle() is False
    time.sleep(0.1)
    assert t.is_idle() is True


def test_mark_output_resets_idle_clock() -> None:
    """A burst of output mid-wait should re-arm the idle gate."""
    t = IdleTracker(idle_threshold_s=0.05)
    t.mark_output()
    time.sleep(0.1)
    assert t.is_idle() is True
    t.mark_output()
    assert t.is_idle() is False


# ── compose_wake_prompt ───────────────────────────────────────────────────────


def test_wake_prompt_includes_name_and_count() -> None:
    p = compose_wake_prompt("alpha", 2, ["bob", "carol"])
    assert "[alpha]" in p
    assert "2 unread" in p
    assert "bob" in p
    assert "carol" in p


def test_wake_prompt_dedupes_senders() -> None:
    """Bob sending 3 times = "from bob", not "from bob, bob, bob"."""
    p = compose_wake_prompt("alpha", 3, ["bob", "bob", "bob"])
    # Exactly one occurrence of bob in the rendered prompt
    assert p.count("bob") == 1
    assert "3 unread" in p  # count still reflects total


def test_wake_prompt_caps_sender_list_with_overflow_marker() -> None:
    senders = [f"sender{i}" for i in range(8)]
    p = compose_wake_prompt("alpha", 8, senders)
    # Cap at 5 distinct senders, show "+3 more"
    assert "sender0" in p
    assert "sender4" in p
    assert "sender5" not in p
    assert "+3 more" in p


def test_wake_prompt_ends_with_newline_to_submit() -> None:
    """The trailing \\n is what causes the child CLI to actually submit
    the synthetic prompt as a turn (vs. leaving it sitting at the input
    line waiting for the user to press Enter)."""
    p = compose_wake_prompt("alpha", 1, ["bob"])
    assert p.endswith("\n")


def test_wake_prompt_instructs_llm_to_call_mcp_tool() -> None:
    """The LLM needs to know what to DO with the wake — call mcp__dlb__read
    and surface to the user. Without that hint, naive LLMs might just
    acknowledge the wake and proceed."""
    p = compose_wake_prompt("alpha", 1, ["bob"])
    assert "mcp__dlb__read" in p
    assert "surface" in p.lower()
