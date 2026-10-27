# motion.py - motion triggering that does not chatter.
#
# v1's log: 735 "motion started" against 17,390 "motion stopped", a 23.7:1
# ratio, and 4,250 captures for 735 real approaches to the sink. One threshold,
# no hysteresis, no debounce, so every lighting flicker and compression
# artefact toggled the state and each toggle cost an mp4 encode on a Pi 3B+.
#
# The fix is a Schmitt trigger plus a dwell requirement:
#   - enter MOTION only above T_HIGH, leave only below T_LOW (T_LOW < T_HIGH),
#     so a signal hovering near one threshold cannot oscillate
#   - a state change must persist for N consecutive frames before it commits
#   - after a capture, a cooldown suppresses retriggering on the same event

import logging
import os
import time
from collections import deque

import cv2
import numpy as np

log = logging.getLogger("dishwatcher.motion")

# fraction of ROI pixels that must be foreground. two thresholds, not one.
T_HIGH = float(os.environ.get("MOTION_ENTER_FRACTION", "0.020"))
T_LOW = float(os.environ.get("MOTION_EXIT_FRACTION", "0.008"))
# consecutive frames a new state must hold before it is believed
ENTER_FRAMES = int(os.environ.get("MOTION_ENTER_FRAMES", "3"))
EXIT_FRAMES = int(os.environ.get("MOTION_EXIT_FRAMES", "8"))
# minimum blob size in pixels, kills speckle
# in pixels at the DOWNSCALED working size, not the capture size
MIN_BLOB_AREA = int(os.environ.get("MOTION_MIN_BLOB_AREA", "120"))
COOLDOWN_SEC = float(os.environ.get("MOTION_COOLDOWN_SEC", "20"))
# Downscale before MOG2. Frames are 1280x720 now, and running background
# subtraction at that size saturated the Pi 3B+ (measured 64% of a core,
# enough to touch the soft thermal limit). Motion does not need the detail:
# v1 used 320x240 for exactly this reason.
MOTION_W = int(os.environ.get("MOTION_WIDTH", "320"))
MOTION_H = int(os.environ.get("MOTION_HEIGHT", "240"))

IDLE, MOTION = "idle", "motion"


class MotionTrigger:
    def __init__(self, roi=None):
        self.roi = roi
        self._bg = cv2.createBackgroundSubtractorMOG2(
            history=500,        # a long history keeps the background stable
            varThreshold=25,
            detectShadows=True,  # shadows are a top false-positive source
        )
        self.state = IDLE
        self._pending = None
        self._pending_count = 0
        self._entered_at = 0.0
        self._last_capture_at = 0.0
        self._recent = deque(maxlen=60)
        self.transitions = {"idle->motion": 0, "motion->idle": 0}

    def _foreground_fraction(self, frame):
        img = frame
        if self.roi:
            x1, y1, x2, y2 = self.roi
            img = frame[max(0, y1):y2, max(0, x1):x2]

        # downscale first; this is the difference between idling and pinning a core
        if img.shape[1] > MOTION_W:
            img = cv2.resize(img, (MOTION_W, MOTION_H), interpolation=cv2.INTER_NEAREST)

        mask = self._bg.apply(img)
        # MOG2 marks shadows as 127; only 255 is real foreground.
        mask = (mask == 255).astype(np.uint8) * 255

        # open then close: drop speckle, then consolidate what survives
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)

        # ignore blobs too small to be a person
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        area = sum(cv2.contourArea(c) for c in contours if cv2.contourArea(c) >= MIN_BLOB_AREA)

        total = img.shape[0] * img.shape[1]
        return (area / total) if total else 0.0

    def update(self, frame):
        """
        Feed a frame. Returns one of: None, "entered", "exited".
        Only a committed transition returns non-None, so callers cannot see
        the chatter that v1 acted on.
        """
        frac = self._foreground_fraction(frame)
        self._recent.append(frac)

        # Schmitt trigger: which state does this frame argue for?
        if self.state == IDLE:
            wants = MOTION if frac >= T_HIGH else IDLE
            need = ENTER_FRAMES
        else:
            wants = IDLE if frac <= T_LOW else MOTION
            need = EXIT_FRAMES

        if wants == self.state:
            self._pending, self._pending_count = None, 0
            return None

        if self._pending == wants:
            self._pending_count += 1
        else:
            self._pending, self._pending_count = wants, 1

        if self._pending_count < need:
            return None

        # commit
        old, self.state = self.state, wants
        self._pending, self._pending_count = None, 0
        self.transitions[f"{old}->{wants}"] += 1

        if wants == MOTION:
            self._entered_at = time.monotonic()
            log.info("motion entered (fg %.3f >= %.3f for %d frames)", frac, T_HIGH, need)
            return "entered"

        log.info("motion exited after %.1fs (fg %.3f <= %.3f for %d frames)",
                 time.monotonic() - self._entered_at, frac, T_LOW, need)
        return "exited"

    def in_cooldown(self):
        return (time.monotonic() - self._last_capture_at) < COOLDOWN_SEC

    def mark_capture(self):
        self._last_capture_at = time.monotonic()

    def stats(self):
        started = self.transitions["idle->motion"]
        stopped = self.transitions["motion->idle"]
        return {
            "state": self.state,
            "entered_total": started,
            "exited_total": stopped,
            # v1 sat at 23.7. Anything far from 1.0 means it is flapping again.
            "flap_ratio": round(stopped / started, 2) if started else 0.0,
            "recent_fg_mean": round(float(np.mean(self._recent)), 4) if self._recent else 0.0,
        }
