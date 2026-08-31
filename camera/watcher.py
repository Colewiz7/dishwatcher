# watcher.py - pi edge node v5.1
# watches the sink via usb webcam. records blame clips, waits for person
# to leave, then posts frame + video to server.
# now flips frames at capture time so video clips are right-side-up too.

import io
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from enum import Enum

import cv2
import numpy as np
import requests

import capture
import motion

# -- load .env --
def _load_dotenv():
    envfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(envfile):
        with open(envfile) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

_load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dishwatcher.edge")

def _env(key, default):
    return os.environ.get(key, default)

# -- config --

SERVER_URL       = _env("DISH_SERVER_URL", "http://localhost:8000/upload")
REPORT_URL       = SERVER_URL.rsplit("/upload", 1)[0] + "/camera/report"
API_KEY          = _env("DISH_API_KEY", "")
CAMERA_INDEX     = int(_env("CAMERA_INDEX", "0"))

FRAME_W          = int(_env("FRAME_WIDTH", "1280"))
FRAME_H          = int(_env("FRAME_HEIGHT", "720"))

# camera flip: NONE, CW, CCW, 180
# set this if your camera is mounted upside down or sideways
# this flips frames BEFORE everything so video clips are correct too
_FLIP_MAP = {"CW": cv2.ROTATE_90_CLOCKWISE, "CCW": cv2.ROTATE_90_COUNTERCLOCKWISE,
             "180": cv2.ROTATE_180, "NONE": None}
CAMERA_FLIP = _FLIP_MAP.get(_env("CAMERA_FLIP", "NONE").upper(), None)

# motion detection
MOTION_W         = int(_env("MOTION_WIDTH", "320"))
MOTION_H         = int(_env("MOTION_HEIGHT", "240"))
MIN_CONTOUR_AREA = int(_env("MIN_CONTOUR_AREA", "500"))
MOTION_PERCENT   = float(_env("MOTION_PERCENT", "0.5"))
PROCESS_EVERY_N  = int(_env("PROCESS_EVERY_N", "3"))
IDLE_SLEEP_MS    = float(_env("IDLE_SLEEP_MS", "50"))

# video
VIDEO_FPS        = int(_env("VIDEO_FPS", "5"))
VIDEO_DURATION   = int(_env("VIDEO_DURATION", "15"))
BUFFER_SIZE      = VIDEO_FPS * VIDEO_DURATION
CAPTURE_DELAY    = float(_env("CAPTURE_DELAY_SEC", "10"))

# heartbeat
HEARTBEAT_SEC    = float(_env("HEARTBEAT_INTERVAL_SEC", "30"))
HEALTH_INTERVAL_SEC = float(_env("HEALTH_INTERVAL_SEC", "30"))
MONITOR_DURATION = float(_env("MONITORING_DURATION_SEC", "7200"))
CLEAR_EXIT_N     = int(_env("CLEAR_EXIT_N", "3"))

JPEG_QUALITY     = int(_env("JPEG_QUALITY", "60"))
REQUEST_TIMEOUT  = float(_env("REQUEST_TIMEOUT_SEC", "30"))

MOG2_HISTORY     = int(_env("MOG2_HISTORY", "300"))
MOG2_VAR_THRESH  = int(_env("MOG2_VAR_THRESHOLD", "40"))

MAX_BACKOFF      = 60.0


class State(Enum):
    IDLE     = "idle"
    MOTION   = "motion"
    COOLDOWN = "cooldown"
    MONITOR  = "monitor"


_shutdown = False
_session = None
_jpeg_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]


def _handle_signal(signum, _frame):
    global _shutdown
    log.info("signal %d, shutting down", signum)
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# -- video buffer --

class VideoBuffer:
    def __init__(self, maxlen, fps):
        self._buf = deque(maxlen=maxlen)
        self._fps = fps
        self._last_save = 0.0
        self._interval = 1.0 / fps

    def maybe_add(self, frame, now):
        if now - self._last_save >= self._interval:
            ok, jpeg = cv2.imencode(".jpg", frame, _jpeg_params)
            if ok:
                self._buf.append(jpeg.tobytes())
                self._last_save = now

    def encode_video(self):
        """h264 mp4 via ffmpeg for browser playback"""
        if len(self._buf) < 5:
            return None, False

        tmpdir = tempfile.mkdtemp(prefix="blame_")
        mp4_path = os.path.join(tempfile.gettempdir(), "blame_clip.mp4")

        try:
            for i, jpeg_bytes in enumerate(self._buf):
                with open(os.path.join(tmpdir, f"{i:04d}.jpg"), "wb") as f:
                    f.write(jpeg_bytes)

            cmd = [
                "ffmpeg", "-y",
                "-framerate", str(self._fps),
                "-i", os.path.join(tmpdir, "%04d.jpg"),
                "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "28", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", mp4_path,
            ]
            r = subprocess.run(cmd, capture_output=True, timeout=60)

            if r.returncode == 0 and os.path.isfile(mp4_path):
                size_kb = os.path.getsize(mp4_path) / 1024
                log.info("video: h264 mp4 (%.0f KB, %d frames)", size_kb, len(self._buf))
                return mp4_path, True
            else:
                log.warning("ffmpeg failed: %s", (r.stderr or b"")[-300:].decode(errors="replace"))
                return None, False
        except FileNotFoundError:
            log.error("ffmpeg not found. sudo apt install ffmpeg")
            return None, False
        except Exception as e:
            log.error("encode failed: %s", e)
            return None, False
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def clear(self):
        self._buf.clear()

    @property
    def count(self):
        return len(self._buf)


# -- networking --

def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        if API_KEY:
            _session.headers["X-API-Key"] = API_KEY
    return _session


def post_capture(frame, video_path=None):
    try:
        ok, buf = cv2.imencode(".jpg", frame, _jpeg_params)
        if not ok:
            return None

        files = {"frame": ("frame.jpg", io.BytesIO(buf.tobytes()), "image/jpeg")}
        if video_path and os.path.isfile(video_path):
            files["video"] = ("clip.mp4", open(video_path, "rb"), "video/mp4")

        resp = _get_session().post(
            SERVER_URL, headers={"X-Watcher-Mode": "motion_end"},
            files=files, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        if video_path and os.path.isfile(video_path):
            os.unlink(video_path)
        return resp.json()

    except requests.ConnectionError:
        log.error("cant reach %s", SERVER_URL)
    except requests.Timeout:
        log.error("upload timed out")
    except requests.HTTPError as e:
        log.error("http error: %s", e)
    except Exception as e:
        log.error("post failed: %s", e)
    return None


def post_heartbeat(frame):
    try:
        ok, buf = cv2.imencode(".jpg", frame, _jpeg_params)
        if not ok:
            return None
        resp = _get_session().post(
            SERVER_URL, headers={"X-Watcher-Mode": "heartbeat"},
            files={"frame": ("frame.jpg", io.BytesIO(buf.tobytes()), "image/jpeg")},
            timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("heartbeat failed: %s", e)
    return None


def smoke_test():
    url = SERVER_URL.rsplit("/upload", 1)[0] + "/healthz"
    try:
        r = _get_session().get(url, timeout=5)
        if r.status_code == 200:
            d = r.json()
            log.info("server ok: state=%s", d.get("state"))
            return True
    except Exception as e:
        log.warning("health check failed: %s", e)
    return False


def detect_motion(frame, bgsub, kernel, motion_thresh):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (MOTION_W, MOTION_H), interpolation=cv2.INTER_NEAREST)
    fg = bgsub.apply(small)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    area = sum(cv2.contourArea(c) for c in contours if cv2.contourArea(c) >= MIN_CONTOUR_AREA)
    return area >= motion_thresh, area


# -- main --

def post_health(camera, trigger):
    """
    Tell the server how the edge node is doing.

    v1 had no channel for this at all, so a wedged camera was invisible until
    somebody read a 160k-line log. These numbers drive the dashboard's camera
    card and the Prometheus alerts.
    """
    try:
        payload = dict(camera.stats())
        payload.update(trigger.stats())
        payload["motion_state"] = trigger.state
        _get_session().post(REPORT_URL, json=payload, timeout=10)
    except Exception as e:
        log.debug("health report failed: %s", e)


def main():
    log.info("=== dishwatcher edge v2 ===")
    log.info("server:  %s", SERVER_URL)
    log.info("camera:  index %d, requesting %dx%d %s",
             CAMERA_INDEX, FRAME_W, FRAME_H, _env("CAMERA_FOURCC", "MJPG"))
    log.info("video:   %ds @ %dfps (%d frame buffer)", VIDEO_DURATION, VIDEO_FPS, BUFFER_SIZE)

    smoke_test()

    cam = capture.Camera(
        index=CAMERA_INDEX,
        width=FRAME_W,
        height=FRAME_H,
        fps=int(_env("CAMERA_FPS", "30")),
        fourcc=_env("CAMERA_FOURCC", "MJPG"),
    )
    trigger = motion.MotionTrigger()

    video_buf = VideoBuffer(BUFFER_SIZE, VIDEO_FPS)
    frame_counter = 0
    exited_at = None
    last_heartbeat = 0.0
    last_health = 0.0
    last_frame = None
    srv_state = "CLEAR"
    consec_clear = 0
    monitoring_until = 0.0

    log.info("running")

    try:
        while not _shutdown:
            ok, frame = cam.read()
            if not ok:
                # the watchdog inside Camera handles reopening; we just pace
                # ourselves and keep reporting so the wedge is visible.
                now = time.monotonic()
                if now - last_health >= HEALTH_INTERVAL_SEC:
                    post_health(cam, trigger)
                    last_health = now
                time.sleep(0.2)
                continue

            if CAMERA_FLIP is not None:
                frame = cv2.rotate(frame, CAMERA_FLIP)

            last_frame = frame
            frame_counter += 1
            now = time.monotonic()

            # always buffer while something is happening, so the clip covers
            # the approach rather than starting when we notice
            if trigger.state == motion.MOTION or exited_at is not None:
                video_buf.maybe_add(frame, now)

            if now - last_health >= HEALTH_INTERVAL_SEC:
                post_health(cam, trigger)
                last_health = now

            if frame_counter % PROCESS_EVERY_N == 0:
                event = trigger.update(frame)

                if event == "entered":
                    video_buf.clear()
                    video_buf.maybe_add(frame, now)
                    exited_at = None

                elif event == "exited":
                    # wait for the person to clear the frame before the shot
                    exited_at = now

            # capture once the post-exit delay has elapsed
            if exited_at is not None and (now - exited_at) >= CAPTURE_DELAY:
                exited_at = None
                if trigger.in_cooldown():
                    log.info("skipping capture, still in cooldown")
                else:
                    trigger.mark_capture()
                    video_path, _ = video_buf.encode_video()
                    resp = post_capture(frame, video_path)
                    video_buf.clear()
                    if resp:
                        if resp.get("calibrated") is False:
                            # the server is telling us it cannot see. say so
                            # here too rather than looping quietly.
                            log.warning("server is NOT CALIBRATED: %s", resp.get("reason"))
                        srv_state = resp.get("state", srv_state)
                        monitoring_until = now + MONITOR_DURATION
                        consec_clear = 0
                        log.info("[%s] dishes=%s ssim=%s labels=%s",
                                 srv_state, resp.get("dishes_found"),
                                 resp.get("ssim_score"), resp.get("labels"))

            # heartbeat while the server still cares
            if now < monitoring_until and (now - last_heartbeat) >= HEARTBEAT_SEC:
                last_heartbeat = now
                resp = post_heartbeat(frame)
                if resp:
                    srv_state = resp.get("state", srv_state)
                    if resp.get("dishes_found"):
                        consec_clear = 0
                    else:
                        consec_clear += 1
                        if consec_clear >= CLEAR_EXIT_N:
                            monitoring_until = 0.0
                            log.info("clear x%d, back to idle", consec_clear)
                    log.info("hb [%s] %s", srv_state,
                             "dishes" if resp.get("dishes_found") else
                             f"clear ({consec_clear}/{CLEAR_EXIT_N})")

            if trigger.state == motion.IDLE and exited_at is None:
                time.sleep(IDLE_SLEEP_MS / 1000)

    finally:
        s = trigger.stats()
        log.info("shutting down. motion entered=%d exited=%d flap_ratio=%s | "
                 "camera reopens=%d usb_resets=%d",
                 s["entered_total"], s["exited_total"], s["flap_ratio"],
                 cam.stats()["reopens"], cam.stats()["usb_resets"])
        post_health(cam, trigger)
        cam.release()


if __name__ == "__main__":
    main()
