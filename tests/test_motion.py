"""
Motion trigger tests.

v1 produced 735 "motion started" against 17,390 "motion stopped" (23.7:1) and
4,250 captures for 735 real approaches. These tests pin the hysteresis so that
regression is caught by CI rather than by reading a 160k-line log.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "camera"))
import motion as M  # noqa: E402


class FakeTrigger(M.MotionTrigger):
    """Drives the state machine from a scripted foreground fraction."""
    def __init__(self):
        super().__init__()
        self._script = 0.0

    def _foreground_fraction(self, frame):
        return self._script

    def feed(self, value, n=1):
        out = []
        self._script = value
        for _ in range(n):
            out.append(self.update(None))
        return [o for o in out if o]


def test_single_spike_does_not_trigger():
    """One noisy frame above the threshold must not commit a transition."""
    t = FakeTrigger()
    assert t.feed(0.9, n=1) == []
    assert t.state == M.IDLE


def test_sustained_motion_triggers_once():
    t = FakeTrigger()
    events = t.feed(0.9, n=20)
    assert events == ["entered"]
    assert t.state == M.MOTION


def test_signal_hovering_at_threshold_does_not_flap():
    """
    The v1 failure, reproduced. A signal oscillating around a single threshold
    made v1 toggle every frame. With T_LOW < T_HIGH it must commit at most one
    transition.
    """
    t = FakeTrigger()
    mid = (M.T_HIGH + M.T_LOW) / 2
    events = []
    for _ in range(200):
        events += t.feed(mid * 1.05, n=1)
        events += t.feed(mid * 0.95, n=1)
    assert len(events) == 0, f"hovering signal produced {len(events)} transitions"


def test_noisy_real_world_signal_stays_balanced():
    """
    Replay a plausible noisy trace: long idle, a real approach, long idle.
    Entered and exited counts must match, and the flap ratio must be near 1.0
    rather than v1's 23.7.
    """
    rng = np.random.default_rng(7)
    t = FakeTrigger()
    for _ in range(300):                       # quiet, with sensor noise
        t.feed(abs(rng.normal(0.001, 0.004)), n=1)
    for _ in range(60):                        # somebody at the sink
        t.feed(abs(rng.normal(0.08, 0.02)), n=1)
    for _ in range(300):                       # quiet again
        t.feed(abs(rng.normal(0.001, 0.004)), n=1)

    s = t.stats()
    assert s["entered_total"] == 1, s
    assert s["exited_total"] == 1, s
    assert s["flap_ratio"] == 1.0, s


def test_exit_requires_more_evidence_than_entry():
    """Leaving is the expensive decision (it triggers a capture), so it is slower."""
    assert M.EXIT_FRAMES > M.ENTER_FRAMES


def test_thresholds_form_a_real_hysteresis_band():
    assert M.T_LOW < M.T_HIGH, "a single threshold is exactly the v1 bug"


def test_cooldown_suppresses_retrigger():
    t = FakeTrigger()
    assert not t.in_cooldown()
    t.mark_capture()
    assert t.in_cooldown()
