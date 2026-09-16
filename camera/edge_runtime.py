"""Bounded background work and inexpensive preview frames for the Pi."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2


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
        return True, future.result()

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)


def temperature_c(path="/sys/class/thermal/thermal_zone0/temp"):
    try:
        return float(Path(path).read_text().strip()) / 1000
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
    if w <= max_width:
        return frame
    width = max(2, (max_width // 2) * 2)
    height = max(2, (round(h * width / w) // 2) * 2)
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
