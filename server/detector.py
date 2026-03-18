# detector.py - yolov8 inference + sink roi filtering
# runs dual pass: full frame first, then cropped sink region upscaled
# to catch small stuff (forks/spoons are like 20px in a full frame lol)

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from ultralytics import YOLO

log = logging.getLogger("dishwatcher.detector")

# coco class ids we care about
SINK_CLASS_ID = 71
DISH_CLASS_IDS = {41: "cup", 42: "fork", 43: "knife", 44: "spoon", 45: "bowl"}
ALL_KITCHENWARE = {**DISH_CLASS_IDS, SINK_CLASS_ID: "sink"}
YOLO_CLASSES = list(ALL_KITCHENWARE.keys())  # pass to model() so it skips the other 74 classes

# dual pass config
DUAL_PASS  = os.environ.get("DUAL_PASS_ENABLED", "true").lower() == "true"
DUAL_PAD   = int(os.environ.get("DUAL_PASS_PADDING", "40"))
DUAL_MIN_W = int(os.environ.get("DUAL_PASS_MIN_WIDTH", "200"))
IOU_THRESH = float(os.environ.get("IOU_DEDUP_THRESHOLD", "0.4"))

# annotation colors (rgb for pillow)
CLR_DISH = (34, 197, 94)
CLR_TEXT = (0, 0, 0)
STATE_COLORS = {
    "CLEAR": (34, 197, 94), "DETECTED": (234, 179, 8),
    "CONFIRMED": (249, 115, 22), "ALERTED": (239, 68, 68),
}

# font stuff
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]

def _load_font(size=15):
    for p in _FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    log.warning("no ttf font found, using default")
    return ImageFont.load_default()

FONT = _load_font(15)

# sink bbox gets cached to disk so we dont need to re-detect it every frame
_CACHE_PATH = Path(__file__).parent / "sink_location.json"
_sink_bbox = None
_model = None


def load_model(path):
    global _model
    log.info("loading yolo: %s", path)
    _model = YOLO(path)

    # warmup with a dummy frame so the first real one isnt slow
    dummy = np.zeros((480, 640, 3), dtype=np.uint8)
    _model(dummy, verbose=False, classes=YOLO_CLASSES)
    log.info("model ready (warmed up). dual pass: %s", DUAL_PASS)

    _load_sink_cache()


def get_model():
    if _model is None:
        raise RuntimeError("model not loaded, call load_model() first")
    return _model


# -- sink cache --
# saves the sink bounding box to json so we dont lose it on restart.
# if you move the camera, hit POST /admin/reset-sink

def _load_sink_cache():
    global _sink_bbox
    if _CACHE_PATH.exists():
        try:
            _sink_bbox = json.loads(_CACHE_PATH.read_text())["bbox"]
            log.info("loaded sink cache: %s", _sink_bbox)
        except Exception as e:
            log.warning("bad sink cache: %s", e)

def _save_sink_cache(bbox):
    try:
        _CACHE_PATH.write_text(json.dumps({"bbox": bbox}, indent=2))
    except Exception as e:
        log.warning("couldnt save sink cache: %s", e)

def reset_sink_cache():
    global _sink_bbox
    _sink_bbox = None
    if _CACHE_PATH.exists():
        _CACHE_PATH.unlink()
    log.info("sink cache cleared")

def get_sink_status():
    return {"cached": _sink_bbox is not None, "bbox": _sink_bbox,
            "cache_file": str(_CACHE_PATH)}


# -- geometry helpers --

def _centre_in(dbox, sbox):
    """check if center of dish bbox is inside sink bbox"""
    cx = (dbox[0] + dbox[2]) * 0.5
    cy = (dbox[1] + dbox[3]) * 0.5
    return sbox[0] <= cx <= sbox[2] and sbox[1] <= cy <= sbox[3]

def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    return inter / union if union > 0 else 0.0

def _dedup(dets, thresh):
    """remove overlapping detections of same class, keep higher confidence"""
    if len(dets) <= 1:
        return dets
    dets = sorted(dets, key=lambda d: d["confidence"], reverse=True)
    keep = []
    for d in dets:
        if not any(d["class_id"] == k["class_id"] and _iou(d["bbox"], k["bbox"]) >= thresh
                   for k in keep):
            keep.append(d)
    return keep


# -- extract detections from yolo results --

def _extract(results, conf_thresh, ox=0, oy=0):
    """pull detections out of yolo results as dicts. ox/oy offset for crop remapping."""
    dishes, sinks = [], []

    for r in results:
        if r.boxes is None or len(r.boxes) == 0:
            continue

        # batch numpy extraction, way faster than per-box .item() calls
        xyxy = r.boxes.xyxy.cpu().numpy().astype(int)
        confs = r.boxes.conf.cpu().numpy()
        clss = r.boxes.cls.cpu().numpy().astype(int)

        for i in range(len(clss)):
            cid = clss[i]
            conf = float(confs[i])
            if conf < conf_thresh or cid not in ALL_KITCHENWARE:
                continue

            bbox = [int(xyxy[i][0]) + ox, int(xyxy[i][1]) + oy,
                    int(xyxy[i][2]) + ox, int(xyxy[i][3]) + oy]
            entry = {"label": ALL_KITCHENWARE[cid], "class_id": cid,
                     "confidence": round(conf, 4), "bbox": bbox}
            (sinks if cid == SINK_CLASS_ID else dishes).append(entry)

    return dishes, sinks


# -- inference --

def run_inference(frame, conf_thresh):
    """
    dual pass yolo inference.
    pass 1: full frame (finds sink, bowls, cups)
    pass 2: cropped sink region upscaled to 640px (catches forks, spoons, knives)
    returns (detections, total_ms, meta)
    """
    global _sink_bbox
    model = get_model()
    h, w = frame.shape[:2]

    # pass 1: full frame
    t0 = time.perf_counter()
    res1 = model(frame, verbose=False, classes=YOLO_CLASSES)
    t1 = time.perf_counter()
    p1_ms = (t1 - t0) * 1000

    dishes1, sinks1 = _extract(res1, conf_thresh)

    # learn sink location on first detection
    sink_new = False
    if _sink_bbox is None and sinks1:
        best = max(sinks1, key=lambda s: s["confidence"])
        _sink_bbox = best["bbox"]
        _save_sink_cache(_sink_bbox)
        sink_new = True
        log.info("sink learned: %s (%.0f%%)", _sink_bbox, best["confidence"] * 100)

    # pass 2: cropped sink region, upscaled so small objects are actually visible
    dishes2 = []
    p2_ms = 0.0

    if DUAL_PASS and _sink_bbox is not None:
        sx1, sy1, sx2, sy2 = _sink_bbox
        cx1, cy1 = max(0, sx1 - DUAL_PAD), max(0, sy1 - DUAL_PAD)
        cx2, cy2 = min(w, sx2 + DUAL_PAD), min(h, sy2 + DUAL_PAD)
        cw = cx2 - cx1

        if cw >= DUAL_MIN_W:
            crop = frame[cy1:cy2, cx1:cx2]
            scale = 640.0 / cw
            up = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)

            t2 = time.perf_counter()
            res2 = model(up, verbose=False, classes=YOLO_CLASSES)
            t3 = time.perf_counter()
            p2_ms = (t3 - t2) * 1000

            raw2, _ = _extract(res2, conf_thresh)

            # map upscaled coords back to full frame coords
            inv = 1.0 / scale
            for d in raw2:
                bx1, by1, bx2, by2 = d["bbox"]
                d["bbox"] = [int(bx1 * inv) + cx1, int(by1 * inv) + cy1,
                             int(bx2 * inv) + cx1, int(by2 * inv) + cy1]
            dishes2 = raw2

    total_ms = round(p1_ms + p2_ms, 2)

    # merge both passes, dedup overlaps, only keep stuff inside the sink
    merged = _dedup(dishes1 + dishes2, IOU_THRESH)

    if _sink_bbox is None:
        filtered = []
    else:
        filtered = [d for d in merged if _centre_in(d["bbox"], _sink_bbox)]

    meta = {
        "sink_bbox": _sink_bbox, "sink_newly_found": sink_new,
        "sink_cached": _sink_bbox is not None,
        "pass1_ms": round(p1_ms, 2), "pass2_ms": round(p2_ms, 2),
        "pass2_detections": len(dishes2),
    }
    return filtered, total_ms, meta


# -- annotation --
# draws bounding boxes + labels on the frame using pillow (crisp text)

def annotate_frame(frame, detections, sink_bbox=None, state_label=""):
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(rgb)
    draw = ImageDraw.Draw(img, "RGBA")

    # sink roi overlay
    if sink_bbox:
        sx1, sy1, sx2, sy2 = sink_bbox
        draw.rectangle([sx1, sy1, sx2, sy2],
                       fill=(59, 130, 246, 25), outline=(59, 130, 246, 180), width=2)
        lbl = "SINK ZONE"
        bb = draw.textbbox((0, 0), lbl, font=FONT)
        lw, lh = bb[2] - bb[0], bb[3] - bb[1]
        draw.text((sx1 + (sx2 - sx1 - lw) // 2, sy2 - lh - 6),
                  lbl, fill=(59, 130, 246, 160), font=FONT)

    # dish bounding boxes
    for det in detections:
        x1, y1, x2, y2 = det["bbox"]
        label = f"{det['label']}  {det['confidence']:.0%}"
        draw.rectangle([x1, y1, x2, y2], outline=CLR_DISH + (255,), width=2)

        bb = draw.textbbox((0, 0), label, font=FONT)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        pad = 5
        ly = y1 - th - pad * 2
        if ly < 0:
            ly = y2  # label below box if theres no room above
        draw.rectangle([x1, ly, x1 + tw + pad * 2, ly + th + pad * 2],
                       fill=CLR_DISH + (255,))
        draw.text((x1 + pad, ly + pad // 2), label, fill=CLR_TEXT, font=FONT)

    # state overlay top left corner
    if state_label:
        color = STATE_COLORS.get(state_label.split()[0], (148, 163, 184))
        bb = draw.textbbox((0, 0), state_label, font=FONT)
        sw, sh = bb[2] - bb[0], bb[3] - bb[1]
        draw.rectangle([8, 8, 18 + sw + 8, 16 + sh], fill=color + (220,))
        draw.text((14, 10), state_label, fill=(255, 255, 255), font=FONT)

    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
