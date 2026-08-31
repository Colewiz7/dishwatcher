# calibration.py - the reference image + ROI, and whether they are actually usable.
#
# v1 kept the reference in detector.py and guarded the whole SSIM path behind
# "if _reference is not None and _roi and 'sink' in _roi". When that guard was
# false the detector silently returned ssim=1.0, which reads as "identical to
# clean", which reads as "no dishes". It ran that way for 15 days and 4250
# events without a single warning above DEBUG.
#
# So calibration is its own module now, it has exactly one public question
# (is this usable?), and the answer is a structured reason rather than a bool
# that callers can accidentally treat as "fine".

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("dishwatcher.calibration")

# a reference smaller than this in either axis is almost certainly a mistake
MIN_REF_EDGE = 64
# an ROI smaller than this many pixels cannot hold a meaningful SSIM window
MIN_ROI_EDGE = 32


@dataclass
class CalibrationState:
    """Why calibration is or is not usable. Never just a bool."""
    valid: bool
    reason: str
    has_reference: bool = False
    has_roi: bool = False
    reference_shape: Optional[tuple] = None
    roi: Optional[dict] = field(default=None)

    def as_dict(self):
        return {
            "valid": self.valid,
            "reason": self.reason,
            "has_reference": self.has_reference,
            "has_roi": self.has_roi,
            "reference_shape": list(self.reference_shape) if self.reference_shape else None,
            "roi": self.roi,
        }


class Calibration:
    def __init__(self, data_dir):
        self._dir = Path(data_dir)
        self._ref_path = self._dir / "reference.jpg"
        self._roi_path = self._dir / "roi.json"
        self._lock = threading.Lock()
        self._reference = None
        self._roi = None
        self.load()

    # -- loading --

    def load(self):
        import json
        with self._lock:
            self._reference = None
            self._roi = None

            if self._ref_path.exists():
                img = cv2.imread(str(self._ref_path))
                if img is None:
                    log.error("reference.jpg exists but opencv cannot decode it: %s", self._ref_path)
                else:
                    self._reference = img
                    log.info("loaded reference %s %dx%d", self._ref_path, img.shape[1], img.shape[0])

            if self._roi_path.exists():
                try:
                    self._roi = json.loads(self._roi_path.read_text())
                    log.info("loaded roi %s", self._roi)
                except Exception as e:
                    log.error("roi.json is unreadable: %s", e)

        state = self.state()
        # this is the line whose absence cost 15 days. INFO, not DEBUG, and it
        # says what to do about it.
        if state.valid:
            log.info("calibration OK: reference %s, sink roi %s",
                     state.reference_shape, (state.roi or {}).get("sink"))
        else:
            log.warning("CALIBRATION INVALID (%s) - detection is disabled until "
                        "a reference and sink ROI are set from the dashboard",
                        state.reason)

    # -- the one question that matters --

    def state(self) -> CalibrationState:
        with self._lock:
            ref, roi = self._reference, self._roi

        has_ref = ref is not None
        has_roi = bool(roi and "sink" in roi)

        if not has_ref and not has_roi:
            return CalibrationState(False, "no reference image and no sink ROI", False, False)
        if not has_ref:
            return CalibrationState(False, "no reference image", False, has_roi, roi=roi)
        if not has_roi:
            return CalibrationState(False, "no sink ROI defined", True, False,
                                    reference_shape=ref.shape)

        h, w = ref.shape[:2]
        if h < MIN_REF_EDGE or w < MIN_REF_EDGE:
            return CalibrationState(False, f"reference is too small ({w}x{h})",
                                    True, True, ref.shape, roi)

        x1, y1, x2, y2 = roi["sink"]
        if (x2 - x1) < MIN_ROI_EDGE or (y2 - y1) < MIN_ROI_EDGE:
            return CalibrationState(False,
                                    f"sink ROI is too small ({x2-x1}x{y2-y1}, need >= {MIN_ROI_EDGE})",
                                    True, True, ref.shape, roi)
        if x2 > w or y2 > h or x1 < 0 or y1 < 0:
            return CalibrationState(False,
                                    f"sink ROI {roi['sink']} falls outside the reference ({w}x{h})",
                                    True, True, ref.shape, roi)

        return CalibrationState(True, "ok", True, True, ref.shape, roi)

    def is_valid(self) -> bool:
        return self.state().valid

    # -- accessors, only meaningful when state().valid --

    @property
    def reference(self):
        with self._lock:
            return self._reference

    @property
    def roi(self):
        with self._lock:
            return dict(self._roi) if self._roi else None

    # -- mutation --

    def set_reference(self, frame):
        """Save a new clean-sink reference. Returns the resulting CalibrationState."""
        self._dir.mkdir(parents=True, exist_ok=True)
        ok = cv2.imwrite(str(self._ref_path), frame)
        if not ok:
            raise IOError(f"could not write reference to {self._ref_path}")
        with self._lock:
            self._reference = frame.copy()
        state = self.state()
        log.info("reference set (%dx%d) -> calibration %s (%s)",
                 frame.shape[1], frame.shape[0],
                 "VALID" if state.valid else "STILL INVALID", state.reason)
        return state

    def set_roi(self, roi: dict):
        import json
        self._dir.mkdir(parents=True, exist_ok=True)
        self._roi_path.write_text(json.dumps(roi, indent=2))
        with self._lock:
            self._roi = dict(roi)
        state = self.state()
        log.info("roi set %s -> calibration %s (%s)", roi,
                 "VALID" if state.valid else "STILL INVALID", state.reason)
        return state

    def clear(self):
        for p in (self._ref_path, self._roi_path):
            if p.exists():
                p.unlink()
        with self._lock:
            self._reference = None
            self._roi = None
        log.warning("calibration cleared")
        return self.state()

    # -- startup self-check --

    def self_check(self) -> tuple:
        """
        Prove SSIM actually computes on this reference rather than assuming it.
        Returns (ok, message). Called at boot; result drives /readyz.
        """
        state = self.state()
        if not state.valid:
            return False, f"calibration invalid: {state.reason}"

        try:
            from detector import compute_ssim_tiled, prep_for_ssim, crop_roi
            ref = self.reference
            crop = crop_roi(ref, self.roi["sink"])
            g = prep_for_ssim(crop)
            # identical inputs must score ~1.0; if this does not hold the SSIM
            # implementation itself is broken and every later score is garbage.
            score, _ = compute_ssim_tiled(g, g)
            if not (0.98 <= score <= 1.0001):
                return False, f"SSIM self-check failed: identical crops scored {score:.4f}, expected ~1.0"

            # and a deliberately corrupted copy must score meaningfully lower,
            # which is what proves the metric actually discriminates.
            noisy = g.copy()
            h, w = noisy.shape[:2]
            noisy[h // 4:h // 2, w // 4:w // 2] = 0
            score2, _ = compute_ssim_tiled(g, noisy)
            if score2 >= score:
                return False, (f"SSIM self-check failed: corrupted crop scored {score2:.4f} "
                               f">= identical {score:.4f}; metric is not discriminating")

            return True, f"ok (identical={score:.4f}, corrupted={score2:.4f})"
        except Exception as e:
            return False, f"SSIM self-check raised: {e}"
