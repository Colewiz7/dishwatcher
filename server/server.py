# server.py - central node
# fastapi app that takes frames from the pi, runs yolo, manages state,
# pushes updates to the web dashboard via sse
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

import detector
import notifier
import state_machine
import storage

# -- config --

API_KEY         = os.environ.get("DISH_API_KEY", None)
MODEL_PATH      = os.environ.get("YOLO_MODEL_PATH", "yolov8n.pt")
CONFIDENCE      = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.40"))
SAVE_DIR        = os.environ.get("IMAGE_SAVE_DIR",
                      str(Path.home() / "dishwasher" / "images"))

_ROTATION_MAP = {
    "CCW": cv2.ROTATE_90_COUNTERCLOCKWISE, "CW": cv2.ROTATE_90_CLOCKWISE,
    "180": cv2.ROTATE_180, "NONE": None,
}
CAMERA_ROTATION = _ROTATION_MAP.get(
    os.environ.get("CAMERA_ROTATION", "180").upper(), cv2.ROTATE_180)

STATIC_DIR = Path(__file__).parent / "static"
ICON_PATH  = Path(__file__).parent / "icon.png"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("dishwatcher.server")

# -- init --

detector.load_model(MODEL_PATH)
storage.configure(SAVE_DIR)
sm = state_machine.DishStateMachine()


# -- sse broadcaster --
# simple pub/sub so the dashboard gets real-time updates
# each connected browser tab subscribes to a queue

class EventBroadcaster:
    def __init__(self):
        self._subs = []

    def subscribe(self):
        q = asyncio.Queue(maxsize=50)
        self._subs.append(q)
        return q

    def unsubscribe(self, q):
        if q in self._subs:
            self._subs.remove(q)

    async def publish(self, event_type, data):
        payload = json.dumps(data, default=str)
        dead = []
        for q in self._subs:
            try:
                q.put_nowait({"event": event_type, "data": payload})
            except asyncio.QueueFull:
                dead.append(q)  # client too slow, drop them
        for q in dead:
            self._subs.remove(q)

    @property
    def client_count(self):
        return len(self._subs)

broadcaster = EventBroadcaster()


# -- app --

app = FastAPI(title="Dish Watcher", version="4.0.0")
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def verify_api_key(key):
    if API_KEY is None:
        return  # auth disabled
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")


# -- health / ops --

@app.get("/healthz")
async def healthz():
    return JSONResponse({
        "status": "ok", "model": MODEL_PATH,
        "state": sm.state.value, "version": "4.0.0",
        "sse_clients": broadcaster.client_count,
    })

@app.get("/icon.png")
async def icon():
    if not ICON_PATH.exists():
        raise HTTPException(404)
    return FileResponse(str(ICON_PATH), media_type="image/png")


# -- sse stream --
# dashboard connects here, gets every detection event + state change in real time

@app.get("/stream")
async def sse_stream(request: Request):
    queue = broadcaster.subscribe()

    async def generate():
        try:
            # send current state on connect so the dashboard is immediately up to date
            initial = json.dumps({
                "type": "init", "status": sm.get_status(), "stats": sm.get_stats(),
            }, default=str)
            yield f"event: init\ndata: {initial}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: {msg['event']}\ndata: {msg['data']}\n\n"
                except asyncio.TimeoutError:
                    # keepalive so proxies dont kill the connection
                    hb = json.dumps({"ts": datetime.utcnow().isoformat()})
                    yield f"event: heartbeat\ndata: {hb}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


# -- status endpoints (dashboard polls some of these too) --

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

@app.get("/status/alerts")
async def status_alerts(limit: int = Query(20, ge=1, le=100)):
    return JSONResponse(sm.recent_alerts(limit))


# -- admin --

@app.get("/admin/sink-status")
async def sink_status():
    return JSONResponse(detector.get_sink_status())

@app.post("/admin/reset-sink")
async def reset_sink():
    detector.reset_sink_cache()
    await broadcaster.publish("admin", {"action": "sink_reset"})
    return JSONResponse({"status": "ok", "message": "sink cache cleared"})

@app.post("/admin/force-state")
async def force_state(state: str = Query(...), reason: str = Query("manual override")):
    valid = [s.value for s in state_machine.DishState]
    if state not in valid:
        raise HTTPException(400, f"invalid state, pick from: {valid}")
    sm.force_state(state, reason)
    await broadcaster.publish("state", {
        "state": state, "reason": reason, "status": sm.get_status()})
    return JSONResponse({"status": "ok", "state": state, "reason": reason})

@app.post("/admin/test-notify")
async def test_notify():
    results = notifier.send_alert("test notification from dashboard",
                                   image_path=storage.get_latest_path())
    return JSONResponse({"status": "ok", "results": results})


# -- main upload endpoint --
# pi posts frames here. we decode, rotate, run yolo, update state machine,
# save annotated image, broadcast to dashboard, and fire notifications if needed

@app.post("/upload")
async def upload_frame(
    file: UploadFile = File(...),
    x_api_key: Optional[str] = Header(default=None),
    mode: Optional[str] = Header(default=None, alias="X-Watcher-Mode"),
):
    verify_api_key(x_api_key)

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")

    arr = np.frombuffer(raw, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(422, "couldnt decode image")

    if CAMERA_ROTATION is not None:
        frame = cv2.rotate(frame, CAMERA_ROTATION)

    capture_mode = mode or "unknown"
    log.info("frame %dx%d (%.1fKB) mode=%s",
             frame.shape[1], frame.shape[0], len(raw) / 1024, capture_mode)

    # run yolo
    detections, inference_ms, meta = detector.run_inference(frame, CONFIDENCE)
    dishes_found = len(detections) > 0

    labels = [d["label"] for d in detections]
    avg_conf = (sum(d["confidence"] for d in detections) / len(detections)
                if detections else 0.0)

    # build state label for the image annotation
    state_label = sm.state.value
    if sm.grace_remaining is not None:
        mins = int(sm.grace_remaining.total_seconds() / 60)
        state_label += f" ({mins}m left)"

    annotated = detector.annotate_frame(
        frame, detections, sink_bbox=meta["sink_bbox"], state_label=state_label)
    filename = storage.save_frame(annotated, dishes_found, state=sm.state.value)

    # update state machine
    sm_result = sm.update(
        dishes_found=dishes_found, detection_count=len(detections),
        labels=labels, confidence_avg=avg_conf,
        inference_ms=inference_ms, image_file=filename)

    # push to dashboard
    sse_payload = {
        "timestamp": datetime.utcnow().isoformat(),
        "dishes_found": dishes_found, "detection_count": len(detections),
        "detections": detections, "labels": labels,
        "inference_ms": inference_ms, "capture_mode": capture_mode,
        "image_file": filename,
        "state": sm_result["state"], "previous_state": sm_result["previous_state"],
        "state_changed": sm_result["changed"], "should_alert": sm_result["should_alert"],
        "consensus": sm_result["consensus"],
        "grace_remaining": sm_result["grace_remaining"],
        "dishes_since": sm_result["dishes_since"],
    }
    await broadcaster.publish("detection", sse_payload)

    if sm_result["changed"]:
        await broadcaster.publish("state", {
            "state": sm_result["state"], "previous_state": sm_result["previous_state"],
            "reason": "consensus transition", "status": sm.get_status()})

    # discord alerts (if configured)
    if sm_result["should_alert"]:
        msg = f"dishes sitting in the sink for {int(sm.grace_minutes)} min, go wash them"
        results = notifier.send_alert(msg, image_path=os.path.join(SAVE_DIR, filename))
        for ch, ok in results.items():
            sm.log_alert(ch, ok, msg, filename)

    if (sm_result["changed"] and sm_result["state"] == "CLEAR"
            and sm_result["previous_state"] in ("CONFIRMED", "ALERTED")):
        results = notifier.send_clear_notification()
        for ch, ok in results.items():
            sm.log_alert(ch, ok, "dishes cleared", filename)

    log.info("%.1f ms | state=%s | dishes=%s | %s | sse=%d",
             inference_ms, sm_result["state"], dishes_found,
             labels, broadcaster.client_count)

    return JSONResponse({
        "dishes_found": dishes_found, "detection_count": len(detections),
        "detections": detections, "inference_ms": inference_ms,
        "saved_as": filename, "capture_mode": capture_mode,
        "state": sm_result["state"], "state_changed": sm_result["changed"],
        "consensus": sm_result["consensus"],
        "grace_remaining": sm_result["grace_remaining"],
        "dishes_since": sm_result["dishes_since"],
        "sink_bbox": meta["sink_bbox"], "sink_cached": meta["sink_cached"],
    })


# -- dashboard + image serving --

@app.get("/", response_class=HTMLResponse)
async def root_page():
    html_path = STATIC_DIR / "viewer.html"
    if not html_path.exists():
        raise HTTPException(500, "viewer.html not found")
    return FileResponse(str(html_path), media_type="text/html")

@app.get("/view", response_class=HTMLResponse)
async def view_page():
    html_path = STATIC_DIR / "viewer.html"
    if not html_path.exists():
        raise HTTPException(500, "viewer.html not found")
    return FileResponse(str(html_path), media_type="text/html")

@app.get("/view/list")
async def list_images(limit: int = 40):
    return JSONResponse(storage.list_images(limit=limit))

@app.get("/view/latest.jpg")
async def latest_jpg():
    path = storage.get_latest_path()
    if path is None:
        raise HTTPException(404, "no images yet")
    with open(path, "rb") as f:
        return Response(content=f.read(), media_type="image/jpeg")

@app.get("/view/image/{filename}")
async def serve_image(filename: str):
    path = storage.get_image_path(filename)
    if not os.path.isfile(path):
        raise HTTPException(404)
    with open(path, "rb") as f:
        return Response(content=f.read(), media_type="image/jpeg")
