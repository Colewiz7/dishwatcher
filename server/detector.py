# detector.py - v5 detection engine
# primary detection: compare current frame against a "clean" reference using SSIM
# secondary (optional): run yolo to label what's actually there
# counter detection (optional): separate ROI for counter area
#
# way more reliable than yolo-only because SSIM catches everything:
# pots, pans, cutting boards, random shit piled up. yolo only knows
# specific coco classes. SSIM just asks "does this look different from clean?"

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("dishwatcher.detector")

# -- config --
YOLO_ENABLED     = os.environ.get("YOLO_ENABLED", "true").lower() == "true"
SSIM_THRESHOLD   = float(os.environ.get("SSIM_THRESHOLD", "0.82"))
COUNTER_ENABLED  = os.environ.get("COUNTER_ENABLED", "false").lower() == "true"
COUNTER_SSIM     = float(os.environ.get("COUNTER_SSIM_THRESHOLD", "0.80"))

# paths
_DATA_DIR    = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
_REF_PATH    = _DATA_DIR / "reference.jpg"
_ROI_PATH    = _DATA_DIR / "roi.json"
_MODEL_PATH  = os.environ.get("YOLO_MODEL_PATH", "yolov8n.pt")
_CONFIDENCE  = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.40"))

# coco classes for yolo labeling
DISH_CLASSES = {41: "cup", 42: "fork", 43: "knife", 44: "spoon", 45: "bowl"}
SINK_CLASS   = 71
ALL_CLASSES  = {**DISH_CLASSES, SINK_CLASS: "sink"}
YOLO_CLASS_IDS = list(ALL_CLASSES.keys())

# annotation colors (rgb for pillow, bgr for opencv)
CLR_DIRTY  = (0, 0, 255)     # red
CLR_CLEAN  = (0, 200, 0)     # green
CLR_SINK   = (246, 130, 59)  # blue
CLR_COUNTER = (255, 165, 0)  # orange

# font
FONT = cv2.FONT_HERSHEY_SIMPLEX

# state
_model = None
_reference = None  # the "clean sink" reference image
_roi = None        # {"sink": [x1,y1,x2,y2], "counter": [x1,y1,x2,y2] (optional)}


# -- ssim implementation --
# no extra deps needed, just numpy + opencv

def compute_ssim(img1, img2):
    """structural similarity between two grayscale images. returns 0-1 (1=identical)"""
    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2

    i1 = img1.astype(np.float64)
    i2 = img2.astype(np.float64)

    mu1 = cv2.GaussianBlur(i1, (11, 11), 1.5)
    mu2 = cv2.GaussianBlur(i2, (11, 11), 1.5)

    mu1_sq = mu1 ** 2
    mu2_sq = mu2 ** 2
    mu1_mu2 = mu1 * mu2

    sig1_sq = cv2.GaussianBlur(i1 ** 2, (11, 11), 1.5) - mu1_sq
    sig2_sq = cv2.GaussianBlur(i2 ** 2, (11, 11), 1.5) - mu2_sq
    sig12   = cv2.GaussianBlur(i1 * i2, (11, 11), 1.5) - mu1_mu2

    num = (2 * mu1_mu2 + C1) * (2 * sig12 + C2)
    den = (mu1_sq + mu2_sq + C1) * (sig1_sq + sig2_sq + C2)

    ssim_map = num / den
    return float(ssim_map.mean())


def _prep_for_ssim(img):
    """grayscale + histogram equalize to handle lighting changes"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    return cv2.equalizeHist(gray)


def _crop_roi(img, roi):
    """crop image to roi [x1, y1, x2, y2]"""
    x1, y1, x2, y2 = roi
    h, w = img.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    return img[y1:y2, x1:x2]


# -- reference frame --

def load_reference():
    global _reference
    if _REF_PATH.exists():
        _reference = cv2.imread(str(_REF_PATH))
        if _reference is not None:
            log.info("loaded reference image: %s", _REF_PATH)
        else:
            log.warning("reference file exists but couldnt read it")
    else:
        log.info("no reference image yet, set one via dashboard")


def save_reference(frame):
    """save a frame as the clean reference"""
    global _reference
    os.makedirs(str(_DATA_DIR), exist_ok=True)
    cv2.imwrite(str(_REF_PATH), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    _reference = frame.copy()
    log.info("reference saved: %s", _REF_PATH)


def get_reference():
    return _reference


def has_reference():
    return _reference is not None


# -- roi --

def load_roi():
    global _roi
    if _ROI_PATH.exists():
        try:
            _roi = json.loads(_ROI_PATH.read_text())
            log.info("loaded roi: %s", _roi)
        except Exception as e:
            log.warning("bad roi file: %s", e)


def save_roi(roi_data):
    """save roi. expects {"sink": [x1,y1,x2,y2], "counter": [x1,y1,x2,y2] (optional)}"""
    global _roi
    os.makedirs(str(_DATA_DIR), exist_ok=True)
    _ROI_PATH.write_text(json.dumps(roi_data, indent=2))
    _roi = roi_data
    log.info("roi saved: %s", _roi)


def get_roi():
    return _roi


def auto_detect_sink(frame):
    """use yolo to find the sink bbox. returns [x1,y1,x2,y2] or None"""
    if _model is None:
        return None

    results = _model(frame, verbose=False, classes=[SINK_CLASS])
    for r in results:
        if r.boxes is None or len(r.boxes) == 0:
            continue
        # pick highest confidence sink
        confs = r.boxes.conf.cpu().numpy()
        best = confs.argmax()
        bbox = r.boxes.xyxy[best].cpu().numpy().astype(int).tolist()
        log.info("auto-detected sink: %s (conf %.0f%%)", bbox, confs[best] * 100)
        return bbox

    return None


# -- yolo (secondary, for labeling) --

def load_model(path=None):
    global _model
    path = path or _MODEL_PATH
    if YOLO_ENABLED:
        log.info("loading yolo: %s", path)
        from ultralytics import YOLO
        _model = YOLO(path)
        # warmup
        dummy = np.zeros((480, 640, 3), dtype=np.uint8)
        _model(dummy, verbose=False, classes=YOLO_CLASS_IDS)
        log.info("yolo ready")
    else:
        log.info("yolo disabled")

    load_reference()
    load_roi()


def _run_yolo(frame):
    """run yolo and return list of detection dicts"""
    if _model is None:
        return []

    results = _model(frame, verbose=False, classes=YOLO_CLASS_IDS)
    dets = []

    for r in results:
        if r.boxes is None or len(r.boxes) == 0:
            continue
        xyxy = r.boxes.xyxy.cpu().numpy().astype(int)
        confs = r.boxes.conf.cpu().numpy()
        clss = r.boxes.cls.cpu().numpy().astype(int)

        for i in range(len(clss)):
            cid = clss[i]
            if cid == SINK_CLASS or cid not in ALL_CLASSES:
                continue
            if confs[i] < _CONFIDENCE:
                continue
            dets.append({
                "label": ALL_CLASSES[cid],
                "class_id": int(cid),
                "confidence": round(float(confs[i]), 4),
                "bbox": xyxy[i].tolist(),
            })

    return dets


# -- main detection --

def detect(frame):
    """
    main detection function. returns dict with:
        dishes_found, ssim_score, labels, detections,
        counter_dirty, counter_ssim (if enabled),
        has_reference, has_roi, inference_ms
    """
    t0 = time.perf_counter()
    result = {
        "dishes_found": False,
        "ssim_score": 1.0,
        "labels": [],
        "detections": [],
        "counter_dirty": False,
        "counter_ssim": 1.0,
        "has_reference": has_reference(),
        "has_roi": _roi is not None and "sink" in (_roi or {}),
    }

    # -- ssim comparison against reference --
    if _reference is not None and _roi and "sink" in _roi:
        ref_crop = _crop_roi(_reference, _roi["sink"])
        cur_crop = _crop_roi(frame, _roi["sink"])

        # make sure they're the same size (in case frame size changed)
        if ref_crop.shape != cur_crop.shape:
            cur_crop = cv2.resize(cur_crop, (ref_crop.shape[1], ref_crop.shape[0]))

        ref_gray = _prep_for_ssim(ref_crop)
        cur_gray = _prep_for_ssim(cur_crop)

        score = compute_ssim(ref_gray, cur_gray)
        result["ssim_score"] = round(score, 4)
        result["dishes_found"] = score < SSIM_THRESHOLD

        log.debug("ssim: %.4f (threshold: %.2f) -> %s",
                  score, SSIM_THRESHOLD,
                  "DIRTY" if result["dishes_found"] else "CLEAN")

    elif _reference is None:
        # no reference yet, cant compare. fall back to yolo-only
        log.debug("no reference image, using yolo only")

    # -- counter detection (optional) --
    if COUNTER_ENABLED and _reference is not None and _roi and "counter" in _roi:
        ref_crop = _crop_roi(_reference, _roi["counter"])
        cur_crop = _crop_roi(frame, _roi["counter"])

        if ref_crop.shape != cur_crop.shape:
            cur_crop = cv2.resize(cur_crop, (ref_crop.shape[1], ref_crop.shape[0]))

        ref_gray = _prep_for_ssim(ref_crop)
        cur_gray = _prep_for_ssim(cur_crop)

        counter_score = compute_ssim(ref_gray, cur_gray)
        result["counter_ssim"] = round(counter_score, 4)
        result["counter_dirty"] = counter_score < COUNTER_SSIM

    # -- yolo labeling (secondary) --
    if YOLO_ENABLED and _model is not None:
        dets = _run_yolo(frame)

        # filter to sink roi if we have one
        if _roi and "sink" in _roi:
            sink_box = _roi["sink"]
            in_sink = []
            for d in dets:
                cx = (d["bbox"][0] + d["bbox"][2]) / 2
                cy = (d["bbox"][1] + d["bbox"][3]) / 2
                if sink_box[0] <= cx <= sink_box[2] and sink_box[1] <= cy <= sink_box[3]:
                    d["location"] = "sink"
                    in_sink.append(d)
                elif COUNTER_ENABLED and _roi and "counter" in _roi:
                    cbox = _roi["counter"]
                    if cbox[0] <= cx <= cbox[2] and cbox[1] <= cy <= cbox[3]:
                        d["location"] = "counter"
                        in_sink.append(d)
            dets = in_sink

        result["detections"] = dets
        result["labels"] = [d["label"] for d in dets]

        # if no reference, use yolo as primary detection
        if not has_reference():
            result["dishes_found"] = len([d for d in dets
                                           if d.get("location") != "counter"]) > 0

    result["inference_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return result


# -- annotation --

def annotate_frame(frame, result, state_label=""):
    """draw detection info on the frame"""
    out = frame.copy()
    h, w = out.shape[:2]

    # sink roi
    if _roi and "sink" in _roi:
        x1, y1, x2, y2 = _roi["sink"]
        color = CLR_DIRTY if result["dishes_found"] else CLR_CLEAN
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)

        # ssim score label
        ssim_txt = f"SSIM: {result['ssim_score']:.3f}"
        cv2.putText(out, ssim_txt, (x1 + 4, y2 - 8), FONT, 0.5, color, 1)

        # "SINK" label
        cv2.putText(out, "SINK", (x1 + 4, y1 + 16), FONT, 0.5, CLR_SINK, 1)

    # counter roi (if enabled)
    if COUNTER_ENABLED and _roi and "counter" in _roi:
        cx1, cy1, cx2, cy2 = _roi["counter"]
        ccolor = CLR_COUNTER if result.get("counter_dirty") else CLR_CLEAN
        cv2.rectangle(out, (cx1, cy1), (cx2, cy2), ccolor, 2)
        cv2.putText(out, "COUNTER", (cx1 + 4, cy1 + 16), FONT, 0.5, CLR_COUNTER, 1)
        ctxt = f"SSIM: {result.get('counter_ssim', 0):.3f}"
        cv2.putText(out, ctxt, (cx1 + 4, cy2 - 8), FONT, 0.5, ccolor, 1)

    # yolo detections
    for det in result.get("detections", []):
        bx1, by1, bx2, by2 = det["bbox"]
        label = f"{det['label']} {det['confidence']:.0%}"
        cv2.rectangle(out, (bx1, by1), (bx2, by2), (0, 200, 0), 2)
        cv2.putText(out, label, (bx1, by1 - 6), FONT, 0.45, (0, 200, 0), 1)

    # state label (top left)
    if state_label:
        # background bar
        cv2.rectangle(out, (0, 0), (w, 28), (0, 0, 0), -1)
        status = "DIRTY" if result["dishes_found"] else "CLEAN"
        color = CLR_DIRTY if result["dishes_found"] else CLR_CLEAN
        txt = f"{state_label} | {status} | SSIM {result['ssim_score']:.3f}"
        if not result.get("has_reference"):
            txt = f"{state_label} | NO REFERENCE SET"
            color = (0, 165, 255)
        cv2.putText(out, txt, (6, 20), FONT, 0.55, color, 1)

    return out
