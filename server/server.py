# server.py - central node v5.1
# now with config system, password-protected settings, video thumbnails
# uvicorn server:app --host 0.0.0.0 --port 8000

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

import calibration as calibration_mod
import config
import detector
import metrics
import notifier
import state_machine
import storage

# -- config --

SAVE_DIR   = os.environ.get("SAVE_DIR", str(Path.home() / "dishwasher"))
API_KEY    = os.environ.get("DISH_API_KEY", None)
STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("dishwatcher.server")

# -- init --

os.environ.setdefault("DATA_DIR", SAVE_DIR)
config.init(SAVE_DIR)
storage.configure(SAVE_DIR)
sm = state_machine.DishStateMachine()

# Calibration is a first-class object now. v1 hid it inside detector.py behind a
# guard that silently disabled detection; this one is queried on every request
# and exported as a metric.
calib = calibration_mod.Calibration(SAVE_DIR)

# Startup self-checks. Anything critical that fails here keeps /readyz red, so
# k8s and Argo will not call the app Healthy while it is quietly doing nothing.
SELF_CHECKS = {}


def run_self_checks():
    """Prove the things v1 assumed. Results drive /readyz and the metric."""
    ok, msg = calib.self_check()
    SELF_CHECKS["calibration"] = {"ok": ok, "detail": msg}
    metrics.gauge("dishwatcher_calibration_valid", 1 if calib.is_valid() else 0)
    if ok:
        log.info("self-check calibration: OK (%s)", msg)
    else:
        log.warning("self-check calibration: FAILED - %s", msg)

    try:
        detector._get_yolo()
        SELF_CHECKS["labeller"] = {"ok": True, "detail": "loaded or gracefully disabled"}
    except Exception as e:
        SELF_CHECKS["labeller"] = {"ok": False, "detail": str(e)}

    return SELF_CHECKS


run_self_checks()

# camera health, reported by the edge node on each upload
CAMERA = {"seen": False, "last_report": None, "stats": {}}


def _get_rotation():
    """get cv2 rotation constant from config"""
    m = {"CCW": cv2.ROTATE_90_COUNTERCLOCKWISE, "CW": cv2.ROTATE_90_CLOCKWISE,
         "180": cv2.ROTATE_180, "NONE": None}
    return m.get(config.get("camera_rotation", "180"), cv2.ROTATE_180)


# -- sse --

class EventBroadcaster:
    def __init__(self):
        self._subs = []
    def subscribe(self):
        q = asyncio.Queue(maxsize=50); self._subs.append(q); return q
    def unsubscribe(self, q):
        if q in self._subs: self._subs.remove(q)
    async def publish(self, event_type, data):
        payload = json.dumps(data, default=str)
        dead = []
        for q in self._subs:
            try: q.put_nowait({"event": event_type, "data": payload})
            except asyncio.QueueFull: dead.append(q)
        for q in dead: self._subs.remove(q)
    @property
    def client_count(self): return len(self._subs)

broadcaster = EventBroadcaster()


# -- app --

app = FastAPI(title="Dish Watcher", version="5.1.0")
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _check_api_key(key):
    if API_KEY and key != API_KEY:
        raise HTTPException(401, "bad api key")


def _check_admin(password):
    """check admin password from config"""
    if not config.check_password(password or ""):
        raise HTTPException(401, "wrong password")


def _decode_frame(raw):
    arr = np.frombuffer(raw, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(422, "couldnt decode image")
    rot = _get_rotation()
    if rot is not None:
        frame = cv2.rotate(frame, rot)
    return frame


# -- health --

@app.get("/healthz")
async def healthz():
    return JSONResponse({
        "status": "ok", "version": "2.0.0",
        "state": sm.state.value,
        "has_reference": calib.state().has_reference,
        "has_roi": calib.state().has_roi,
        "calibrated": calib.is_valid(),
        "sse_clients": broadcaster.client_count,
        "password_required": bool(config.get("admin_password")),
    })


# -- config / settings --

@app.get("/readyz")
async def readyz():
    """
    Readiness, and it is deliberately strict: an uncalibrated detector is NOT
    ready. v1's whole failure was a process that looked healthy while detection
    was disabled, so here that state fails the probe and keeps k8s/Argo honest.
    """
    state = calib.state()
    checks = {
        "calibration_valid": state.valid,
        "self_checks": all(c["ok"] for c in SELF_CHECKS.values()) if SELF_CHECKS else False,
        "camera_seen": CAMERA["seen"],
    }
    # camera_seen is informational: a fresh deploy has not heard from the Pi yet
    # and should still be able to come up so you can calibrate from the UI.
    ready = checks["calibration_valid"] and checks["self_checks"]
    metrics.gauge("dishwatcher_calibration_valid", 1 if state.valid else 0)
    return JSONResponse(
        {"ready": ready, "checks": checks, "calibration": state.as_dict(),
         "self_checks": SELF_CHECKS},
        status_code=200 if ready else 503,
    )


@app.get("/metrics")
async def prometheus_metrics():
    state = calib.state()
    metrics.gauge("dishwatcher_calibration_valid", 1 if state.valid else 0)
    cam = CAMERA.get("stats") or {}
    if CAMERA["seen"]:
        for key, metric in (
            ("seconds_since_last_frame", "dishwatcher_camera_seconds_since_last_frame"),
            ("reopens", "dishwatcher_camera_reopens_total"),
            ("usb_resets", "dishwatcher_camera_usb_resets_total"),
            ("flap_ratio", "dishwatcher_motion_flap_ratio"),
        ):
            if key in cam:
                metrics.gauge(metric, cam[key])
    try:
        metrics.gauge("dishwatcher_storage_bytes", storage.total_bytes())
    except Exception:
        pass
    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")


@app.get("/calibration")
async def calibration_status():
    return JSONResponse(calib.state().as_dict())


@app.post("/calibration/reference")
async def calibration_set_reference():
    """Promote the most recent capture to the clean reference."""
    frame = storage.latest_frame()
    if frame is None:
        raise HTTPException(409, "no capture available yet; wait for the camera to send one")
    state = calib.set_reference(frame)
    run_self_checks()
    await broadcaster.publish("calibration", state.as_dict())
    return JSONResponse(state.as_dict())


@app.post("/calibration/roi")
async def calibration_set_roi(request: Request):
    body = await request.json()
    sink = body.get("sink")
    if not (isinstance(sink, list) and len(sink) == 4):
        raise HTTPException(422, "sink must be [x1, y1, x2, y2]")
    state = calib.set_roi({"sink": [int(v) for v in sink]})
    run_self_checks()
    await broadcaster.publish("calibration", state.as_dict())
    return JSONResponse(state.as_dict())


@app.post("/calibration/clear")
async def calibration_clear():
    state = calib.clear()
    run_self_checks()
    await broadcaster.publish("calibration", state.as_dict())
    return JSONResponse(state.as_dict())


@app.post("/camera/report")
async def camera_report(request: Request, x_api_key: Optional[str] = Header(default=None)):
    """Edge node health. Drives the camera card and the wedge alerts."""
    _check_api_key(x_api_key)
    CAMERA["stats"] = await request.json()
    CAMERA["seen"] = True
    CAMERA["last_report"] = datetime.now().isoformat()
    return JSONResponse({"ok": True})


@app.get("/config/schema")
async def config_schema():
    """returns all settings with current values, types, and ui metadata"""
    return JSONResponse(config.get_schema())


@app.get("/config")
async def config_get():
    """returns current config values (passwords masked)"""
    return JSONResponse(config.get_schema())


@app.post("/config")
async def config_update(request: Request):
    """
    update settings. body: {"password": "...", "changes": {"key": value, ...}}
    password required if admin_password is set.
    """
    body = await request.json()
    _check_admin(body.get("password"))

    changes = body.get("changes", {})
    if not changes:
        raise HTTPException(400, "no changes provided")

    changed = config.update(changes)

    # apply runtime changes that need immediate effect
    if "discord_webhook_url" in changed:
        os.environ["DISCORD_WEBHOOK_URL"] = config.get("discord_webhook_url", "")
        notifier.DISCORD_URL = config.get("discord_webhook_url") or None
    if "discord_mention" in changed:
        notifier.DISCORD_MENTION = config.get("discord_mention", "")
    if "notify_cooldown_min" in changed:
        notifier.COOLDOWN_MIN = config.get("notify_cooldown_min", 30)
    if "grace_minutes" in changed:
        sm.grace_minutes = config.get("grace_minutes", 90)

    # push config update to all dashboard clients
    await broadcaster.publish("config", {"changed": changed, "config": config.get_schema()})

    return JSONResponse({"status": "ok", "changed": changed})


@app.post("/config/check-password")
async def check_password(request: Request):
    """check if a password is valid. body: {"password": "..."}"""
    body = await request.json()
    ok = config.check_password(body.get("password", ""))
    return JSONResponse({"valid": ok, "required": bool(config.get("admin_password"))})


# -- sse stream --

@app.get("/stream")
async def sse_stream(request: Request):
    queue = broadcaster.subscribe()

    async def generate():
        try:
            initial = json.dumps({
                "type": "init", "status": sm.get_status(), "stats": sm.get_stats(),
                "has_reference": calib.state().has_reference,
                "roi": calib.roi,
                "calibration": calib.state().as_dict(),
                "config": config.get_schema(),
                "password_required": bool(config.get("admin_password")),
            }, default=str)
            yield f"event: init\ndata: {initial}\n\n"

            while True:
                if await request.is_disconnected(): break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: {msg['event']}\ndata: {msg['data']}\n\n"
                except asyncio.TimeoutError:
                    yield f"event: heartbeat\ndata: {{}}\n\n"
        except asyncio.CancelledError: pass
        finally: broadcaster.unsubscribe(queue)

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"})


# -- status --

@app.get("/status")
async def status():
    return JSONResponse(sm.get_status())

@app.get("/status/stats")
async def status_stats():
    return JSONResponse(sm.get_stats())

@app.get("/status/history")
async def status_history(limit: int = Query(50, ge=1, le=500)):
    return JSONResponse(sm.recent_detections(limit))

@app.get("/status/events")
async def status_events(limit: int = Query(50, ge=1, le=500)):
    return JSONResponse(sm.recent_events(limit))


# -- admin --

@app.post("/admin/set-reference")
async def set_reference(
    file: Optional[UploadFile] = File(None),
    x_api_key: Optional[str] = Header(default=None),
):
    _check_api_key(x_api_key)
    if file:
        raw = await file.read()
        frame = _decode_frame(raw)
    else:
        path = storage.get_latest_image_path()
        if path is None:
            raise HTTPException(400, "no frames yet")
        frame = cv2.imread(path)
        if frame is None:
            raise HTTPException(500, "couldnt read latest frame")

    calib.set_reference(frame)

    roi = calib.roi
    if roi is None or "sink" not in roi:
        sink_bbox = detector.auto_detect_sink(frame)
        if sink_bbox:
            calib.set_roi({"sink": sink_bbox})

    await broadcaster.publish("admin", {
        "action": "reference_set", "has_reference": True, "roi": calib.roi})
    return JSONResponse({"status": "ok", "roi": calib.roi, "calibration": calib.state().as_dict()})


@app.get("/admin/reference.jpg")
async def get_reference():
    ref = calib.reference
    if ref is None:
        raise HTTPException(404, "no reference")
    _, buf = cv2.imencode(".jpg", ref, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@app.post("/admin/set-roi")
async def set_roi(request: Request):
    data = await request.json()
    if "sink" not in data:
        raise HTTPException(400, "need 'sink' roi")
    calib.set_roi(data)
    await broadcaster.publish("admin", {"action": "roi_set", "roi": data})
    return JSONResponse({"status": "ok", "roi": data})

@app.get("/admin/roi")
async def get_roi():
    return JSONResponse(calib.roi or {})

@app.post("/admin/auto-detect-sink")
async def auto_detect_sink():
    path = storage.get_latest_image_path()
    if path is None:
        raise HTTPException(400, "no frames")
    frame = cv2.imread(path)
    bbox = detector.auto_detect_sink(frame)
    if bbox is None:
        raise HTTPException(404, "no sink found")
    roi = calib.roi or {}
    roi["sink"] = bbox
    calib.set_roi(roi)
    await broadcaster.publish("admin", {"action": "roi_set", "roi": roi})
    return JSONResponse({"status": "ok", "roi": roi})

@app.post("/admin/force-state")
async def force_state(state: str = Query(...), reason: str = Query("manual override")):
    valid = [s.value for s in state_machine.DishState]
    if state not in valid:
        raise HTTPException(400, f"pick from: {valid}")
    sm.force_state(state, reason)
    await broadcaster.publish("state", {"state": state, "reason": reason, "status": sm.get_status()})
    return JSONResponse({"status": "ok", "state": state})

@app.post("/admin/test-notify")
async def test_notify():
    results = notifier.send_alert("test from dashboard", image_path=storage.get_latest_image_path())
    return JSONResponse({"status": "ok", "results": results})


# -- upload --

@app.post("/upload")
async def upload_frame(
    frame: UploadFile = File(...),
    video: Optional[UploadFile] = File(None),
    x_api_key: Optional[str] = Header(default=None),
    mode: Optional[str] = Header(default=None, alias="X-Watcher-Mode"),
):
    _check_api_key(x_api_key)

    raw = await frame.read()
    if not raw:
        raise HTTPException(400, "empty frame")

    img = _decode_frame(raw)
    capture_mode = mode or "unknown"
    log.info("frame %dx%d (%.1fKB) mode=%s", img.shape[1], img.shape[0], len(raw)/1024, capture_mode)

    # save blame clip + thumbnail
    video_filename = None
    video_thumb = None
    if video:
        video_bytes = await video.read()
        if video_bytes:
            video_filename, video_thumb = storage.save_video(
                video_bytes, video.filename or "clip.mp4",
                first_frame=img,  # use current frame as thumbnail
                rotation=_get_rotation(),
            )

    # Run detection. The uncalibrated case now RAISES rather than returning a
    # default score that reads as clean, which is the v1 bug this whole rewrite
    # exists to kill. We record it, tell the camera plainly, and stop; we do not
    # advance the state machine on a guess.
    try:
        result = detector.detect(
            img,
            calib,
            ssim_threshold=config.get("ssim_threshold", 0.82),
            yolo_enabled=config.get("yolo_enabled", True),
            confidence=config.get("confidence_threshold", 0.40),
        )
    except detector.NotCalibrated as e:
        metrics.inc("dishwatcher_detections_total", outcome="uncalibrated")
        metrics.gauge("dishwatcher_calibration_valid", 0)
        storage.save_frame(img, False, state="UNCALIBRATED",
                           quality=config.get("jpeg_quality", 90))
        log.warning("upload rejected: not calibrated (%s)", e.reason)
        await broadcaster.publish("calibration", calib.state().as_dict())
        return JSONResponse(
            {"state": "UNCALIBRATED", "calibrated": False, "reason": e.reason,
             "message": "set a clean reference and sink area from the dashboard"},
            status_code=409,
        )

    metrics.observe("dishwatcher_ssim_score", result["ssim_score"])
    metrics.observe("dishwatcher_detector_latency_seconds", result["inference_ms"] / 1000.0)
    metrics.inc("dishwatcher_detections_total",
                outcome="dirty" if result["dishes_found"] else "clean")
    if not result["labels"]:
        metrics.inc("dishwatcher_empty_labels_total")
    metrics.gauge("dishwatcher_calibration_valid", 1)

    state_label = sm.state.value
    if sm.grace_remaining is not None:
        mins = int(sm.grace_remaining.total_seconds() / 60)
        state_label += f" ({mins}m)"

    annotated = detector.annotate_frame(img, result, roi=calib.roi, state_label=state_label)
    quality = config.get("jpeg_quality", 90)
    img_filename = storage.save_frame(annotated, result["dishes_found"],
                                       state=sm.state.value, quality=quality)

    sm_result = sm.update(
        dishes_found=result["dishes_found"],
        detection_count=len(result["detections"]),
        labels=result["labels"],
        confidence_avg=result["ssim_score"],
        inference_ms=result["inference_ms"],
        image_file=img_filename)

    # sse
    sse_payload = {
        "timestamp": datetime.utcnow().isoformat(),
        "dishes_found": result["dishes_found"],
        "ssim_score": result["ssim_score"],
        "detection_count": len(result["detections"]),
        "labels": result["labels"],
        "counter_dirty": result.get("counter_dirty", False),
        "inference_ms": result["inference_ms"],
        "capture_mode": capture_mode,
        "image_file": img_filename,
        "video_file": video_filename,
        "video_thumb": video_thumb,
        "state": sm_result["state"],
        "previous_state": sm_result["previous_state"],
        "state_changed": sm_result["changed"],
        "should_alert": sm_result["should_alert"],
        "consensus": sm_result["consensus"],
        "grace_remaining": sm_result["grace_remaining"],
        "dishes_since": sm_result["dishes_since"],
        "calibration": calib.state().as_dict(),
        "ssim_tiles": result.get("ssim_tiles", []),
        "ssim_threshold": config.get("ssim_threshold", 0.82),
    }
    await broadcaster.publish("detection", sse_payload)

    if sm_result["changed"]:
        await broadcaster.publish("state", {
            "state": sm_result["state"], "previous_state": sm_result["previous_state"],
            "reason": "consensus transition", "status": sm.get_status()})

    if sm_result["should_alert"]:
        msg = f"dishes sitting in the sink for {int(sm.grace_minutes)} min"
        notifier.send_alert(msg, image_path=storage.get_image_path(img_filename))

    if (sm_result["changed"] and sm_result["state"] == "CLEAR"
            and sm_result["previous_state"] in ("CONFIRMED", "ALERTED")):
        notifier.send_clear_notification()

    log.info("ssim=%.3f | %s | state=%s | labels=%s",
             result["ssim_score"], "DIRTY" if result["dishes_found"] else "CLEAN",
             sm_result["state"], result["labels"])

    return JSONResponse({
        "dishes_found": result["dishes_found"], "ssim_score": result["ssim_score"],
        "labels": result["labels"], "detections": result["detections"],
        "inference_ms": result["inference_ms"],
        "saved_as": img_filename, "video_file": video_filename,
        "state": sm_result["state"], "state_changed": sm_result["changed"],
        "consensus": sm_result["consensus"],
        "grace_remaining": sm_result["grace_remaining"],
        "dishes_since": sm_result["dishes_since"],
        "calibration": calib.state().as_dict(),
        "ssim_tiles": result.get("ssim_tiles", []),
        "ssim_threshold": config.get("ssim_threshold", 0.82),
    })


# -- viewer --

@app.get("/", response_class=HTMLResponse)
async def root_page():
    html = STATIC_DIR / "viewer.html"
    if not html.exists(): raise HTTPException(500, "viewer.html not found")
    return FileResponse(str(html), media_type="text/html")

@app.get("/view", response_class=HTMLResponse)
async def view_page():
    return await root_page()

@app.get("/view/list")
async def list_images(limit: int = 40):
    return JSONResponse(storage.list_images(limit=limit))

@app.get("/view/videos")
async def list_videos(limit: int = 20):
    return JSONResponse(storage.list_videos(limit=limit))

@app.get("/view/latest.jpg")
async def latest_jpg():
    path = storage.get_latest_image_path()
    if path is None: raise HTTPException(404, "no images")
    return FileResponse(path, media_type="image/jpeg")

@app.get("/view/image/{filename}")
async def serve_image(filename: str):
    path = storage.get_image_path(filename)
    if not os.path.isfile(path): raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")

@app.get("/view/video/{filename}")
async def serve_video(filename: str):
    path = storage.get_video_path(filename)
    if not os.path.isfile(path): raise HTTPException(404)
    mime = "video/mp4" if filename.endswith(".mp4") else "video/x-msvideo"
    return FileResponse(path, media_type=mime)

@app.get("/view/thumb/{filename}")
async def serve_thumb(filename: str):
    path = storage.get_thumb_path(filename)
    if not os.path.isfile(path): raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")
