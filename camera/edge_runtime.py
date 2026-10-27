"""Bounded background work and inexpensive preview frames for the Pi."""
from concurrent.futures import ThreadPoolExecutor
import logging
import math
import uuid
from pathlib import Path

import cv2

log = logging.getLogger("dishwatcher.edge")


class ClipSession:
    """Nearby visits belong to one cooking session, with ordered clip parts."""
    def __init__(self, gap_seconds=180):
        self.gap_seconds = gap_seconds
        self.session_id = None
        self.last_activity = None
        self.part = 0

    def touch(self, now):
        if self.last_activity is None or now - self.last_activity > self.gap_seconds:
            self.session_id = uuid.uuid4().hex
            self.part = 0
        self.last_activity = now

    def metadata(self, recorded_at):
        self.part += 1
        return {"session_id": self.session_id, "part": self.part, "recorded_at": recorded_at}


class SingleJob:
    """One running job, no waiting queue and no accumulation of camera frames."""
    def __init__(self, name):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self.future = None

    @property
    def busy(self):
        return self.future is not None

    def submit(self, fn, *args):
        if self.busy:
            return False
        self.future = self.executor.submit(fn, *args)
        return True

    def take(self):
        if self.future is None or not self.future.done():
            return False, None
        future, self.future = self.future, None
        try:
            return True, future.result()
        except Exception:
            # A failed upload/encoder must not kill camera acquisition or
            # permanently occupy the single slot. The next job can recover.
            log.exception("background camera job failed")
            return True, None

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)


def temperature_c(path="/sys/class/thermal/thermal_zone0/temp"):
    try:
        value = float(Path(path).read_text().strip()) / 1000
        return value if math.isfinite(value) and 0 <= value <= 150 else None
    except (OSError, ValueError):
        return None


def preview_fps(requested, temperature):
    fps = min(2.0, max(0.5, requested))
    if temperature is None:
        return min(fps, 1.0)
    if temperature >= 75:
        return 0.0
    if temperature >= 70:
        return min(fps, 0.5)
    if temperature >= 60:
        return min(fps, 1.0)
    return fps


def small_frame(frame, max_width=640):
    """Keep the aspect ratio and even dimensions for H.264; never upscale."""
    h, w = frame.shape[:2]
    width = max(2, (min(w, max_width) // 2) * 2)
    height = max(2, (round(h * width / w) // 2) * 2)
    if (w, h) == (width, height):
        return frame
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
