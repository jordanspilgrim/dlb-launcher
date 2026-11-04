"""Unit tests for idle gating, input-line tracking and wake-prompt composition.

All timing is driven by a fake clock: no sleeps, no flakes.
"""

from __future__ import annotations

import re

import pytest

from dlb_launcher.wake import IdleTracker, InputLineTracker, compose_wake_prompt


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


# ── IdleTracker ───────────────────────────────────────────────────────────────


def test_idle_tracker_starts_not_idle() -> None:
    clock = FakeClock()
    t = IdleTracker(idle_threshold_s=0.5, clock=clock)
    assert t.is_idle() is False


def test_idle_tracker_becomes_idle_even_without_output() -> None:
    """A child that prints nothing is still idle once the threshold elapses."""
    clock = FakeClock()
    t = IdleTracker(idle_threshold_s=1.0, clock=clock)
    clock.t += 0.999
    assert t.is_idle() is False
    clock.t += 0.001
    assert t.is_idle() is True


def test_mark_output_resets_idle_clock() -> None:
    clock = FakeClock()
    t = IdleTracker(idle_threshold_s=1.0, clock=clock)
    clock.t += 2.0
    assert t.is_idle() is True
    t.mark_output(b"working...")
    assert t.is_idle() is False
    clock.t += 1.0
    assert t.is_idle() is True


def test_prompt_marker_makes_idle_after_short_quiet() -> None:
    clock = FakeClock()
    t = IdleTracker(idle_threshold_s=1.0, prompt_marker=re.compile(rb"> ?$"), clock=clock)
    t.mark_output(b"done.\r\n> ")
    clock.t += 0.199
    assert t.is_idle() is False
    clock.t += 0.001
    assert t.at_prompt() is True
    assert t.is_idle() is True


def test_prompt_marker_matches_through_ansi_codes() -> None:
    clock = FakeClock()
    t = IdleTracker(prompt_marker=re.compile(rb"> ?$"), clock=clock)
    t.mark_output(b"\x1b[2K\x1b[1;32m> \x1b[0m\r\n")
    assert t.at_prompt() is True


def test_prompt_marker_cleared_by_later_output() -> None:
    clock = FakeClock()
    t = IdleTracker(prompt_marker=re.compile(rb"> ?$"), clock=clock)
    t.mark_output(b"> ")
    t.mark_output(b"thinking")
    assert t.at_prompt() is False
    clock.t += 0.5
    assert t.is_idle() is False  # needs the full threshold without the marker


def test_no_marker_needs_full_threshold() -> None:
    clock = FakeClock()
    t = IdleTracker(idle_threshold_s=1.0, clock=clock)
    t.mark_output(b"> ")
    clock.t += 0.5
    assert t.is_idle() is False


# ── InputLineTracker (unsent user input) ──────────────────────────────────────


@pytest.mark.parametrize(
    ("typed", "dirty"),
    [
        (b"", False),
        (b"a", True),
        (b"abc\r", False),
        (b"abc\n", False),
        (b"ab\x7f", True),
        (b"ab\x7f\x7f", False),
        (b"a\x08", False),
        (b"\x7f\x7f", False),  # backspace on an empty line stays empty
        (b"abc\x15", False),  # Ctrl-U kills the line
        (b"abc\x03", False),  # Ctrl-C abandons it
        (b"\x1b[A\x1b[B\x1b[C\x1b[D", False),  # arrows are not text
        (b"\x1bOA", False),  # SS3 arrow form
        (b"\x1b", False),  # lone ESC (dropped)
        (b"\x1b[200~line1\nline2\x1b[201~", True),  # pasted newline does not submit
        (b"\x1b[200~x\x1b[201~\r", False),
        ("é".encode(), True),
        ("é".encode() + b"\x7f", False),  # one char, one backspace
    ],
)
def test_input_line_tracker(typed: bytes, dirty: bool) -> None:
    line = InputLineTracker()
    line.feed(typed)
    assert line.dirty is dirty


def test_input_line_tracker_split_escape_across_feeds() -> None:
    line = InputLineTracker()
    line.feed(b"\x1b[")
    line.feed(b"A")
    assert line.dirty is False


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
