# capture.py - a camera that notices when it has wedged and fixes itself.
#
# 73% of v1's log (116,713 of 159,574 lines) was:
#   cap_v4l.cpp:1136 tryIoctl VIDEOIO(V4L2:/dev/video0): select() timeout
#
# The Sonix L01 on a Pi 3B+ shares one USB 2.0 bus with ethernet and wedges
# under bandwidth pressure. v1's only mitigation was an ExecStartPre that
# de-authorised and re-authorised the USB device, which runs once at service
# start and therefore does nothing about a mid-run wedge. The process spun
# uselessly until somebody noticed, which took until the next reboot.
#
# Two changes:
#   - MJPG. The camera enumerates MJPG at 1280x720@30 but YUYV 720p only at
#     10fps, because YUYV is uncompressed and does not fit the bus. v1 never
#     set a FOURCC and got YUYV by default.
#   - A watchdog. Every read is timestamped. If frames stop arriving we reopen
#     the device, and only if that fails repeatedly do we escalate to the USB
#     re-authorise trick.

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import cv2

log = logging.getLogger("dishwatcher.capture")

STALL_SECONDS = float(os.environ.get("CAMERA_STALL_SECONDS", "5.0"))
REOPEN_BACKOFF = float(os.environ.get("CAMERA_REOPEN_BACKOFF", "2.0"))
REOPENS_BEFORE_USB_RESET = int(os.environ.get("CAMERA_REOPENS_BEFORE_USB_RESET", "3"))
USB_DEVICE_PATH = os.environ.get("CAMERA_USB_DEVICE", "")  # e.g. 1-1.3


class Camera:
    """
    A self-healing capture source.

    read() returns (ok, frame). It never raises for a wedged device; it reports
    failure and the watchdog repairs underneath. Health is observable via
    seconds_since_last_frame(), which is exported as a metric so a wedge is
    loud instead of silent.
    """

    def __init__(self, index=0, width=1280, height=720, fps=30, fourcc="MJPG"):
        self.index = index
        self.width, self.height, self.fps = width, height, fps
        self.fourcc = fourcc

        self._cap = None
        self._lock = threading.Lock()
        self._last_frame_at = 0.0
        self._opened_at = 0.0
        self._reopens = 0
        self._usb_resets = 0
        self._consecutive_failures = 0
        self._open()

    # -- lifecycle --

    def _open(self):
        with self._lock:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None

            cap = cv2.VideoCapture(self.index, cv2.CAP_V4L2)
            # order matters: FOURCC before the size, or the driver may negotiate
            # an uncompressed format first and refuse the resolution.
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
            # a shallow buffer means a stall shows up now, not after the
            # backlog drains and hands us stale frames.
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

            self._cap = cap
            self._opened_at = time.monotonic()
            self._last_frame_at = time.monotonic()

        if cap.isOpened():
            aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            log.info("camera %s open at %dx%d %s", self.index, aw, ah, self.fourcc)
            if (aw, ah) != (self.width, self.height):
                log.warning("camera negotiated %dx%d, not the requested %dx%d",
                            aw, ah, self.width, self.height)
            return True

        log.error("camera %s failed to open", self.index)
        return False

    def _usb_reset(self):
        """Last resort: bounce the USB device. Needs the path and permission."""
        if not USB_DEVICE_PATH:
            log.warning("camera wedged but CAMERA_USB_DEVICE is unset, cannot USB-reset")
            return False
        p = Path("/sys/bus/usb/devices") / USB_DEVICE_PATH / "authorized"
        if not p.exists():
            log.warning("USB device path %s does not exist", p)
            return False
        try:
            for value in ("0", "1"):
                subprocess.run(["tee", str(p)], input=value.encode(),
                               check=True, capture_output=True, timeout=5)
                time.sleep(1.0)
            self._usb_resets += 1
            log.warning("USB reset of %s complete (reset #%d)", USB_DEVICE_PATH, self._usb_resets)
            time.sleep(2.0)
            return True
        except Exception as e:
            log.error("USB reset failed: %s", e)
            return False

    # -- reading --

    def read(self):
        with self._lock:
            cap = self._cap
        if cap is None:
            return False, None

        ok, frame = cap.read()
        now = time.monotonic()

        if ok and frame is not None:
            self._last_frame_at = now
            self._consecutive_failures = 0
            return True, frame

        self._consecutive_failures += 1
        if self.seconds_since_last_frame() > STALL_SECONDS:
            self._recover()
        return False, None

    def _recover(self):
        stalled = self.seconds_since_last_frame()
        self._reopens += 1
        log.warning("camera stalled %.1fs, reopening (attempt %d)", stalled, self._reopens)

        if self._reopens % REOPENS_BEFORE_USB_RESET == 0:
            log.warning("%d reopens have not helped, escalating to USB reset",
                        self._reopens)
            self._usb_reset()

        time.sleep(REOPEN_BACKOFF)
        self._open()

    # -- health, for metrics and /readyz --

    def seconds_since_last_frame(self):
        return time.monotonic() - self._last_frame_at

    def is_healthy(self):
        return self.seconds_since_last_frame() <= STALL_SECONDS

    def stats(self):
        return {
            "seconds_since_last_frame": round(self.seconds_since_last_frame(), 2),
            "reopens": self._reopens,
            "usb_resets": self._usb_resets,
            "consecutive_failures": self._consecutive_failures,
            "healthy": self.is_healthy(),
            "uptime_seconds": round(time.monotonic() - self._opened_at, 1),
        }

    def release(self):
        with self._lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None
