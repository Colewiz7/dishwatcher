# watcher.py - pi edge node
# watches the sink via usb webcam, posts frames to server when shit moves
# pip install opencv-python-headless requests

import io
import logging
import os
import signal
import sys
import time

import cv2
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dishwatcher.edge")

# -- loader --

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

def _env(key, default):

# -- config (env vars override these) --

def _env(key, default):
    return os.environ.get(key, default)

SERVER_URL       = _env("DISH_SERVER_URL", "http://localhost:8000/upload")
API_KEY          = _env("DISH_API_KEY", "")
CAMERA_INDEX     = int(_env("CAMERA_INDEX", "0"))

FRAME_W          = int(_env("FRAME_WIDTH", "640"))
FRAME_H          = int(_env("FRAME_HEIGHT", "480"))

# motion runs on a tiny grayscale copy so the pi doesnt explode
MOTION_W         = int(_env("MOTION_WIDTH", "320"))
MOTION_H         = int(_env("MOTION_HEIGHT", "240"))
MIN_CONTOUR_AREA = int(_env("MIN_CONTOUR_AREA", "500"))
MOTION_PERCENT   = float(_env("MOTION_PERCENT", "0.5"))

# skip frames to save cpu. 3 = only check every 3rd frame
PROCESS_EVERY_N  = int(_env("PROCESS_EVERY_N", "3"))
IDLE_SLEEP_MS    = float(_env("IDLE_SLEEP_MS", "50"))

MOTION_COOLDOWN  = float(_env("MOTION_COOLDOWN_SEC", "10"))
JPEG_QUALITY     = int(_env("JPEG_QUALITY", "60"))
REQUEST_TIMEOUT  = float(_env("REQUEST_TIMEOUT_SEC", "15"))

MOG2_HISTORY     = int(_env("MOG2_HISTORY", "300"))
MOG2_VAR_THRESH  = int(_env("MOG2_VAR_THRESHOLD", "40"))

# heartbeat: keeps checking even after motion stops (catches static dishes)
HEARTBEAT_SEC    = float(_env("HEARTBEAT_INTERVAL_SEC", "30"))
MONITOR_DURATION = float(_env("MONITORING_DURATION_SEC", "7200"))  # 2hrs max
CLEAR_EXIT_N     = int(_env("CLEAR_EXIT_COUNT", "3"))

MAX_BACKOFF      = 60.0

# -- globals --

_shutdown = False
_session = None
_encode_params = (cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY)  # pre-alloc, minor but w/e


def _handle_signal(signum, _frame):
    global _shutdown
    log.info("caught signal %d, shutting down", signum)
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# -- networking --

def _get_session():
    # reuse connection instead of tcp handshake every damn time
    global _session
    if _session is None:
        _session = requests.Session()
        if API_KEY:
            _session.headers["X-API-Key"] = API_KEY
    return _session


def post_frame(frame, mode="motion"):
    """encode + post a frame to the server. returns json or None on fail."""
    try:
        ok, buf = cv2.imencode(".jpg", frame, _encode_params)
        if not ok:
            log.error("jpeg encode failed")
            return None

        resp = _get_session().post(
            SERVER_URL,
            headers={"X-Watcher-Mode": mode},
            files={"file": ("f.jpg", io.BytesIO(buf.tobytes()), "image/jpeg")},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    except requests.ConnectionError:
        log.error("cant reach %s", SERVER_URL)
    except requests.Timeout:
        log.error("timed out (%.0fs)", REQUEST_TIMEOUT)
    except requests.HTTPError as e:
        log.error("http error: %s", e)
    except Exception as e:
        log.error("post failed: %s", e)
    return None


def smoke_test():
    """quick health check on startup, warns but doesnt bail if server is down"""
    url = SERVER_URL.rsplit("/upload", 1)[0] + "/healthz"
    try:
        r = _get_session().get(url, timeout=5)
        if r.status_code == 200:
            d = r.json()
            log.info("server ok: model=%s state=%s", d.get("model"), d.get("state"))
            return True
        log.warning("health check returned %d", r.status_code)
    except Exception as e:
        log.warning("health check failed: %s", e)
    return False


# -- main loop --

def main():
    log.info("=== dishwatcher edge v4 ===")
    log.info("server:     %s", SERVER_URL)
    log.info("camera:     %d @ %dx%d", CAMERA_INDEX, FRAME_W, FRAME_H)
    log.info("motion res: %dx%d (every %d frames)", MOTION_W, MOTION_H, PROCESS_EVERY_N)
    log.info("heartbeat:  %.0fs | monitor max: %.0fs", HEARTBEAT_SEC, MONITOR_DURATION)

    smoke_test()

    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        log.critical("cant open camera %d", CAMERA_INDEX)
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # keep buffer tiny so frames arent stale

    aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    motion_pixels = MOTION_W * MOTION_H
    motion_thresh = int(motion_pixels * MOTION_PERCENT / 100)
    log.info("actual: %dx%d | motion threshold: %d px", aw, ah, motion_thresh)

    bgsub = cv2.createBackgroundSubtractorMOG2(
        history=MOG2_HISTORY, varThreshold=MOG2_VAR_THRESH, detectShadows=False)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    # loop state
    last_motion_post = 0.0
    last_heartbeat   = 0.0
    monitor_since    = 0.0
    monitoring       = False
    consec_clear     = 0
    srv_state        = "CLEAR"
    backoff          = 1.0
    frame_counter    = 0
    last_frame       = None

    log.info("running. ctrl+c or sigterm to stop")

    try:
        while not _shutdown:
            ret, frame = cap.read()
            if not ret or frame is None:
                log.warning("camera read failed, retrying...")
                time.sleep(1.0)
                continue

            last_frame = frame
            frame_counter += 1
            now = time.monotonic()

            # skip most frames for motion detection, pi cant handle every one
            if frame_counter % PROCESS_EVERY_N != 0:
                time.sleep(0.005)  # dont busy loop on cap.read()
                # still check heartbeat on skipped frames tho
                if monitoring and (now - last_heartbeat) >= HEARTBEAT_SEC:
                    pass  # fall through to heartbeat below
                else:
                    continue

            # -- motion detection --
            # convert to grayscale + downscale. huge cpu savings
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (MOTION_W, MOTION_H), interpolation=cv2.INTER_NEAREST)

            fg = bgsub.apply(small)
            fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
            contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            # sum contour areas (only call contourArea once per contour, old code did it twice)
            motion_area = 0
            for c in contours:
                a = cv2.contourArea(c)
                if a >= MIN_CONTOUR_AREA:
                    motion_area += a

            motion = motion_area >= motion_thresh

            # -- motion triggered post --
            if motion and (now - last_motion_post) >= MOTION_COOLDOWN:
                log.info("motion detected (area=%d px)", motion_area)
                result = post_frame(frame, "motion")

                if result is not None:
                    last_motion_post = now
                    backoff = 1.0
                    srv_state = result.get("state", "CLEAR")

                    # start monitoring mode so we keep checking even after motion stops
                    if not monitoring:
                        log.info("entering monitoring mode")
                        monitoring = True
                        monitor_since = now
                        consec_clear = 0

                    labels = [d["label"] for d in result.get("detections", [])]
                    if result.get("dishes_found"):
                        log.info("[%s] dishes: %s", srv_state, labels)
                    else:
                        log.info("[%s] clear (%.0f ms)", srv_state, result.get("inference_ms", 0))
                else:
                    # failed, back off so we dont spam
                    last_motion_post = now
                    time.sleep(min(backoff, MAX_BACKOFF))
                    backoff = min(backoff * 2, MAX_BACKOFF)

                continue

            # -- heartbeat post (monitoring mode) --
            # sends a frame periodically even without motion.
            # this is how we catch dishes that are just sitting there
            if monitoring and (now - last_heartbeat) >= HEARTBEAT_SEC:
                elapsed_min = (now - monitor_since) / 60

                if elapsed_min >= (MONITOR_DURATION / 60) and srv_state == "CLEAR":
                    log.info("monitor expired (%.0f min), exiting", elapsed_min)
                    monitoring = False
                    continue

                result = post_frame(last_frame, "heartbeat")

                if result is not None:
                    last_heartbeat = now
                    backoff = 1.0
                    srv_state = result.get("state", "CLEAR")

                    if result.get("dishes_found"):
                        consec_clear = 0
                        labels = [d["label"] for d in result.get("detections", [])]
                        log.info("hb [%s] dishes: %s", srv_state, labels)
                    else:
                        consec_clear += 1
                        log.info("hb [%s] clear (%d/%d)", srv_state, consec_clear, CLEAR_EXIT_N)

                    # if its been clear N times in a row, stop monitoring
                    if consec_clear >= CLEAR_EXIT_N and srv_state == "CLEAR":
                        log.info("clear x%d, exiting monitoring", CLEAR_EXIT_N)
                        monitoring = False
                        consec_clear = 0
                else:
                    last_heartbeat = now

                continue

            # nothing happening, sleep so we dont peg the cpu
            if not motion:
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
