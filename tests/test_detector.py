"""
Detection tests.

The v1 bug these exist to prevent: detect() returned a default ssim of 1.0
whenever calibration was missing, which reads as "clean". It ran that way for
15 days and 4250 events. So the first test here is not about accuracy at all,
it is that an uncalibrated detector REFUSES rather than guessing.
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from calibration import Calibration          # noqa: E402
from detector import (                        # noqa: E402
    NotCalibrated, compute_ssim_tiled, detect, prep_for_ssim, _ssim_map,
)

THRESHOLD = 0.82


def _clean(seed=42, size=480):
    rng = np.random.default_rng(seed)
    return cv2.GaussianBlur(rng.integers(90, 140, (size, size), dtype=np.uint8), (9, 9), 3)


def _with_object(base, radius, pos):
    d = base.copy()
    cv2.circle(d, pos, radius, 235, -1)
    return d


def _bgr(gray):
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


# -- the regression that matters most --

def test_uncalibrated_raises_instead_of_returning_clean(tmp_path):
    cal = Calibration(tmp_path)
    assert not cal.is_valid()
    with pytest.raises(NotCalibrated):
        detect(_bgr(_clean()), cal, THRESHOLD, yolo_enabled=False)


def test_reference_without_roi_is_still_invalid(tmp_path):
    cal = Calibration(tmp_path)
    cal.set_reference(_bgr(_clean()))
    assert not cal.is_valid(), "a reference alone must not count as calibrated"
    assert "roi" in cal.state().reason.lower()
    with pytest.raises(NotCalibrated):
        detect(_bgr(_clean()), cal, THRESHOLD, yolo_enabled=False)


def test_roi_outside_reference_is_rejected(tmp_path):
    cal = Calibration(tmp_path)
    cal.set_reference(_bgr(_clean(size=200)))
    cal.set_roi({"sink": [0, 0, 900, 900]})
    assert not cal.is_valid()
    assert "outside" in cal.state().reason


def test_degenerate_roi_is_rejected(tmp_path):
    cal = Calibration(tmp_path)
    cal.set_reference(_bgr(_clean()))
    cal.set_roi({"sink": [10, 10, 20, 20]})
    assert not cal.is_valid()
    assert "too small" in cal.state().reason


def test_self_check_catches_a_broken_metric(tmp_path):
    cal = Calibration(tmp_path)
    cal.set_reference(_bgr(_clean()))
    cal.set_roi({"sink": [0, 0, 480, 480]})
    ok, msg = cal.self_check()
    assert ok, msg


# -- scoring behaviour --

def test_identical_images_score_one():
    c = _clean()
    score, _ = compute_ssim_tiled(c, c)
    assert score == pytest.approx(1.0, abs=1e-3)


@pytest.mark.parametrize("pos", [(120, 120), (240, 240), (60, 400), (390, 90)])
def test_small_object_detected_regardless_of_position(pos):
    """
    A mug covering ~2% of the sink must read dirty wherever it sits.
    v1's whole-ROI mean scored this 0.9886 and missed it completely.
    """
    c = _clean()
    score, _ = compute_ssim_tiled(c, _with_object(c, 38, pos))
    assert score < THRESHOLD, f"mug at {pos} scored {score:.4f}, not detected"


def test_position_independence_is_tight():
    """The score for the same object must barely move as it changes position."""
    c = _clean()
    scores = [compute_ssim_tiled(c, _with_object(c, 38, p))[0]
              for p in [(120, 120), (240, 240), (60, 400), (390, 90)]]
    assert max(scores) - min(scores) < 0.05, f"position spread too wide: {scores}"


def test_whole_roi_mean_would_have_missed_it():
    """Documents precisely why v1 failed, so nobody reintroduces the mean."""
    c = _clean()
    dirty = _with_object(c, 38, (120, 120))
    assert float(_ssim_map(c, dirty).mean()) > THRESHOLD   # v1: missed
    assert compute_ssim_tiled(c, dirty)[0] < THRESHOLD     # v2: caught


@pytest.mark.parametrize("radius,label", [(38, "mug"), (60, "bowl"), (110, "pile")])
def test_larger_objects_also_detected(radius, label):
    c = _clean()
    score, _ = compute_ssim_tiled(c, _with_object(c, radius, (200, 200)))
    assert score < THRESHOLD, f"{label} scored {score:.4f}"


# -- lighting robustness: these must NOT trip --

def test_uniform_brightness_shift_is_not_dishes():
    c = _clean()
    brighter = np.clip(c.astype(np.int16) + 28, 0, 255).astype(np.uint8)
    score, _ = compute_ssim_tiled(prep_for_ssim(c), prep_for_ssim(brighter))
    assert score >= THRESHOLD, f"brightness shift false-positived at {score:.4f}"


def test_partial_shadow_is_not_dishes():
    """Half the sink in shadow. CLAHE is what makes this survivable."""
    c = _clean()
    shadow = c.copy()
    shadow[:, :240] = np.clip(shadow[:, :240].astype(np.int16) - 45, 0, 255).astype(np.uint8)
    score, _ = compute_ssim_tiled(prep_for_ssim(c), prep_for_ssim(shadow))
    assert score >= THRESHOLD, f"shadow false-positived at {score:.4f}"


# -- end to end --

def test_calibrated_detect_reports_clean_then_dirty(tmp_path):
    c = _clean()
    cal = Calibration(tmp_path)
    cal.set_reference(_bgr(c))
    cal.set_roi({"sink": [0, 0, 480, 480]})
    assert cal.is_valid()

    clean_res = detect(_bgr(c), cal, THRESHOLD, yolo_enabled=False)
    assert clean_res["dishes_found"] is False
    assert clean_res["calibrated"] is True

    dirty_res = detect(_bgr(_with_object(c, 60, (200, 200))), cal, THRESHOLD, yolo_enabled=False)
    assert dirty_res["dishes_found"] is True
    assert dirty_res["ssim_score"] < clean_res["ssim_score"]


def test_scores_are_not_constant(tmp_path):
    """
    v1's tell was that every one of 4250 logged scores was exactly 1.000.
    A detector whose output never varies is broken regardless of the value.
    """
    c = _clean()
    cal = Calibration(tmp_path)
    cal.set_reference(_bgr(c))
    cal.set_roi({"sink": [0, 0, 480, 480]})
    scores = {detect(_bgr(_with_object(c, r, (200, 200))), cal, THRESHOLD,
                     yolo_enabled=False)["ssim_score"]
              for r in (0o0 or 30, 50, 70, 90)}
    assert len(scores) > 1, f"detector returned a constant score: {scores}"
