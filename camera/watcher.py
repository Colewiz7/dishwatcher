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
API_KEY          = _env("DISH_API_KEY", "")
CAMERA_INDEX     = int(_env("CAMERA_INDEX", "0"))

FRAME_W          = int(_env("FRAME_WIDTH", "640"))
FRAME_H          = int(_env("FRAME_HEIGHT", "480"))

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
MONITOR_DURATION = float(_env("MONITORING_DURATION_SEC", "7200"))
CLEAR_EXIT_N     = int(_env("CLEAR_EXIT_COUNT", "3"))

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

def main():
    log.info("=== dishwatcher edge v5.1 ===")
    log.info("server:     %s", SERVER_URL)
    log.info("camera:     %d @ %dx%d (flip: %s)", CAMERA_INDEX, FRAME_W, FRAME_H,
             _env("CAMERA_FLIP", "NONE"))
    log.info("video:      %ds @ %dfps (%d frame buffer)", VIDEO_DURATION, VIDEO_FPS, BUFFER_SIZE)
    log.info("capture delay: %.0fs", CAPTURE_DELAY)

    smoke_test()

    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        log.critical("cant open camera %d", CAMERA_INDEX)
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    motion_thresh = int(MOTION_W * MOTION_H * MOTION_PERCENT / 100)
    log.info("actual: %dx%d | motion threshold: %d px", aw, ah, motion_thresh)

    bgsub = cv2.createBackgroundSubtractorMOG2(
        history=MOG2_HISTORY, varThreshold=MOG2_VAR_THRESH, detectShadows=False)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    state = State.IDLE
    video_buf = VideoBuffer(BUFFER_SIZE, VIDEO_FPS)
    frame_counter = 0
    last_motion_at = cooldown_start = last_heartbeat = monitor_since = 0.0
    consec_clear = 0
    srv_state = "CLEAR"
    backoff = 1.0
    last_frame = None

    log.info("running (%s)", state.value)

    try:
        while not _shutdown:
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.5)
                continue

            # flip frame right away so everything downstream is correct
            # this means video clips, motion detection, and posted frames are all oriented
            if CAMERA_FLIP is not None:
                frame = cv2.rotate(frame, CAMERA_FLIP)

            last_frame = frame
            frame_counter += 1
            now = time.monotonic()

            if state in (State.MOTION, State.COOLDOWN):
                video_buf.maybe_add(frame, now)

            should_check = (frame_counter % PROCESS_EVERY_N == 0)
            if not should_check:
                time.sleep(0.005)
                if state == State.MONITOR and (now - last_heartbeat) >= HEARTBEAT_SEC:
                    pass
                else:
                    continue

            motion, motion_area = detect_motion(frame, bgsub, kernel, motion_thresh)

            if state == State.IDLE:
                if motion:
                    state = State.MOTION
                    video_buf.clear()
                    video_buf.maybe_add(frame, now)
                    last_motion_at = now
                    log.info("motion started, recording")
                else:
                    time.sleep(IDLE_SLEEP_MS / 1000)

            elif state == State.MOTION:
                if motion:
                    last_motion_at = now
                else:
                    state = State.COOLDOWN
                    cooldown_start = now
                    log.info("motion stopped, waiting %.0fs (%d frames buffered)",
                             CAPTURE_DELAY, video_buf.count)

            elif state == State.COOLDOWN:
                if motion:
                    state = State.MOTION
                    last_motion_at = now
                elif (now - cooldown_start) >= CAPTURE_DELAY:
                    log.info("capturing after %.0fs cooldown", now - cooldown_start)

                    video_path, video_ok = video_buf.encode_video()
                    if not video_ok:
                        log.warning("video encode failed, frame only")

                    result = post_capture(frame, video_path)

                    if result is not None:
                        srv_state = result.get("state", "CLEAR")
                        backoff = 1.0
                        log.info("[%s] dishes=%s ssim=%.3f labels=%s",
                                 srv_state, result.get("dishes_found"),
                                 result.get("ssim_score", 0), result.get("labels", []))
                        state = State.MONITOR
                        monitor_since = now
                        last_heartbeat = now
                        consec_clear = 0
                    else:
                        log.warning("post failed, backoff %.0fs", backoff)
                        time.sleep(min(backoff, MAX_BACKOFF))
                        backoff = min(backoff * 2, MAX_BACKOFF)
                        state = State.IDLE

                    video_buf.clear()

            elif state == State.MONITOR:
                if motion:
                    state = State.MOTION
                    video_buf.clear()
                    video_buf.maybe_add(frame, now)
                    last_motion_at = now
                    log.info("motion during monitoring, recording")

                elif (now - last_heartbeat) >= HEARTBEAT_SEC:
                    elapsed_min = (now - monitor_since) / 60
                    if elapsed_min >= (MONITOR_DURATION / 60) and srv_state == "CLEAR":
                        log.info("monitor expired, going idle")
                        state = State.IDLE
                        continue

                    result = post_heartbeat(last_frame)
                    if result is not None:
                        last_heartbeat = now
                        srv_state = result.get("state", "CLEAR")
                        if result.get("dishes_found"):
                            consec_clear = 0
                            log.info("hb [%s] dishes", srv_state)
                        else:
                            consec_clear += 1
                            log.info("hb [%s] clear (%d/%d)", srv_state, consec_clear, CLEAR_EXIT_N)
                        if consec_clear >= CLEAR_EXIT_N and srv_state == "CLEAR":
                            log.info("clear x%d, going idle", CLEAR_EXIT_N)
                            state = State.IDLE
                            consec_clear = 0
                    else:
                        last_heartbeat = now
                else:
                    time.sleep(IDLE_SLEEP_MS / 1000)

    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        if _session:
            _session.close()
        log.info("camera released, bye")


if __name__ == "__main__":
    main()
