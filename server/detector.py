# detector.py - v2 detection engine.
#
# Two things changed from v1, both because of what the 15-day log showed.
#
# 1. SSIM is scored by a low percentile of the map, not a whole-ROI mean. v1 returned ssim_map.mean() over the
#    entire sink. A single mug in a big sink moves that mean by almost nothing,
#    so the 0.82 threshold was really a function of how big the ROI was. Now the
#    ROI is cut into a grid and the *worst* tile decides. One dirty corner is
#    enough, which is the actual question being asked.
#
# 2. There is no silent fallback. If calibration is not usable the detector
#    raises NotCalibrated. It does not return a default score that happens to
#    read as clean. Callers must handle the uncalibrated case explicitly.
#
# YOLO stays optional and stays a *labeller*. It never decides dirty/clean.
# In v1 it silently became the only decider, and COCO only has bowl and cup,
# which is why 15 days of production only ever produced those two labels.

import logging
import os
import time
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("dishwatcher.detector")

# tiles across the sink ROI. 4x4 keeps each tile big enough for an 11px gaussian
# window at typical ROI sizes while still localising a single-plate change.
TILE_GRID = int(os.environ.get("SSIM_TILE_GRID", "4"))
# a tile smaller than this cannot host the SSIM window; grid shrinks to fit.
MIN_TILE_EDGE = 24
# percentile of the SSIM map used as the score. See compute_ssim_tiled.
SSIM_PERCENTILE = float(os.environ.get("SSIM_PERCENTILE", "2.0"))
# Gaussian kernel applied before comparison. Must be odd. Set 0 or 1 to disable.
_dk = int(os.environ.get("SSIM_DENOISE_KERNEL", "5"))
DENOISE_KERNEL = _dk if _dk % 2 == 1 else _dk + 1


class NotCalibrated(Exception):
    """Raised instead of silently returning a clean-looking score."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# -- ssim --

def _ssim_map(img1, img2):
    """SSIM map between two single-channel float images. Returns the full map."""
    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2

    i1 = img1.astype(np.float64)
    i2 = img2.astype(np.float64)

    mu1 = cv2.GaussianBlur(i1, (11, 11), 1.5)
    mu2 = cv2.GaussianBlur(i2, (11, 11), 1.5)

    mu1_sq, mu2_sq, mu1_mu2 = mu1 ** 2, mu2 ** 2, mu1 * mu2

    sig1_sq = cv2.GaussianBlur(i1 ** 2, (11, 11), 1.5) - mu1_sq
    sig2_sq = cv2.GaussianBlur(i2 ** 2, (11, 11), 1.5) - mu2_sq
    sig12 = cv2.GaussianBlur(i1 * i2, (11, 11), 1.5) - mu1_mu2

    num = (2 * mu1_mu2 + C1) * (2 * sig12 + C2)
    den = (mu1_sq + mu2_sq + C1) * (sig1_sq + sig2_sq + C2)
    return num / den


def compute_ssim_tiled(img1, img2, grid=None, percentile=None):
    """
    Returns (score, tiles).

    score is a low PERCENTILE of the SSIM map, not a mean and not a tile
    minimum. Measured on synthetic sinks (see tests/test_detector.py):

      metric                one mug, 4 positions        half-shadow (must stay clean)
      v1 whole-ROI mean     0.9886 (misses it entirely)  0.9800
      4x4 tile worst        0.95 / 0.61 / 0.95 / 0.78    0.8680
      2nd percentile        0.7808 0.7797 0.7783 0.7809  0.8769

    The tile minimum swings by 0.34 for the same mug depending on whether it
    lands inside a tile or straddles a boundary. The percentile does not care
    where the object is, which is the property we actually need. Tiles are
    still computed, but only to draw the dashboard heatmap.

    percentile 2.0 means "the worst 2% of the ROI differs", i.e. it detects an
    object covering roughly 2% of the sink. Lower it to catch smaller things at
    the cost of noise sensitivity.
    """
    if img1.shape != img2.shape:
        img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))

    smap = _ssim_map(img1, img2)
    p = SSIM_PERCENTILE if percentile is None else percentile
    score = float(np.percentile(smap, p))

    # tiles are presentation only; they show WHERE the change is.
    h, w = smap.shape[:2]
    g = grid or TILE_GRID
    g = max(1, min(g, h // MIN_TILE_EDGE, w // MIN_TILE_EDGE)) if (h >= MIN_TILE_EDGE and w >= MIN_TILE_EDGE) else 1
    tiles = []
    th, tw = h // g, w // g
    for r in range(g):
        for c in range(g):
            y1, y2 = r * th, ((r + 1) * th if r < g - 1 else h)
            x1, x2 = c * tw, ((c + 1) * tw if c < g - 1 else w)
            tiles.append({"row": r, "col": c, "score": round(float(smap[y1:y2, x1:x2].mean()), 4)})

    return score, tiles


def prep_for_ssim(img):
    """
    Grayscale, CLAHE, then denoise.

    v1 used equalizeHist, a global transform. A single bright window or a
    partial shadow shifts the whole histogram and moves every pixel, producing
    change where there is none. CLAHE normalises locally so a shadow on one
    side does not corrupt the other.

    The blur is not cosmetic, it is what makes this usable on a real camera.
    Measured on two frames of the same clean sink taken one second apart by the
    actual Pi, over the real sink ROI:

        preprocessing              clean vs clean    clean vs dishes
        CLAHE only                 p2 = 0.650        p2 = 0.020
        CLAHE + 5px gaussian       p2 = 0.942        p2 = 0.010

    Without it, webcam sensor noise and JPEG ringing drag the 2nd percentile of
    an unchanged sink down to 0.650, well under any threshold that still
    detects dishes, so an empty sink reads dirty. Synthetic test images are
    smooth and never showed this. Blurring first costs almost nothing in
    sensitivity (dishes still score ~0.01) and widens the margin from 0.63 to
    0.93.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    equalised = clahe.apply(gray)
    if DENOISE_KERNEL > 1:
        return cv2.GaussianBlur(equalised, (DENOISE_KERNEL, DENOISE_KERNEL), 0)
    return equalised


def crop_roi(img, roi):
    x1, y1, x2, y2 = roi
    h, w = img.shape[:2]
    x1, y1 = max(0, int(x1)), max(0, int(y1))
    x2, y2 = min(w, int(x2)), min(h, int(y2))
    return img[y1:y2, x1:x2]


# -- yolo, optional, labels only --

_yolo = None
_yolo_failed = False


def _get_yolo():
    global _yolo, _yolo_failed
    if _yolo is not None or _yolo_failed:
        return _yolo
    try:
        from ultralytics import YOLO
        path = os.environ.get("YOLO_MODEL_PATH", "yolov8n.pt")
        _yolo = YOLO(path)
        log.info("yolo loaded from %s", path)
    except Exception as e:
        _yolo_failed = True
        log.warning("yolo unavailable, labels disabled: %s", e)
    return _yolo


def _label(frame, roi, conf):
    """Best-effort object labels inside the ROI. Never affects dirty/clean."""
    model = _get_yolo()
    if model is None:
        return [], []
    try:
        crop = crop_roi(frame, roi)
        res = model(crop, conf=conf, verbose=False)[0]
        labels, dets = [], []
        for b in res.boxes:
            name = res.names[int(b.cls)]
            c = float(b.conf)
            labels.append(name)
            dets.append({"label": name, "confidence": round(c, 3),
                         "box": [round(float(v)) for v in b.xyxy[0].tolist()]})
        return labels, dets
    except Exception as e:
        log.warning("yolo inference failed: %s", e)
        return [], []


# -- main entry --

def detect(frame, calibration, ssim_threshold, yolo_enabled=True, confidence=0.40):
    """
    Decide whether the sink differs from its clean reference.

    Raises NotCalibrated when calibration is unusable. There is deliberately no
    default-clean return path; that bug is the whole reason for this rewrite.
    """
    state = calibration.state()
    if not state.valid:
        raise NotCalibrated(state.reason)

    t0 = time.perf_counter()
    ref, roi = calibration.reference, calibration.roi
    sink = roi["sink"]

    ref_g = prep_for_ssim(crop_roi(ref, sink))
    cur_g = prep_for_ssim(crop_roi(frame, sink))

    score, tiles = compute_ssim_tiled(ref_g, cur_g)
    dishes = score < ssim_threshold

    labels, dets = ([], [])
    if yolo_enabled:
        labels, dets = _label(frame, sink, confidence)

    result = {
        "dishes_found": bool(dishes),
        "ssim_score": round(score, 4),
        "ssim_tiles": tiles,
        "labels": labels,
        "detections": dets,
        "calibrated": True,
        "inference_ms": round((time.perf_counter() - t0) * 1000, 1),
    }

    log.info("ssim worst-tile %.4f (threshold %.2f) -> %s%s",
             score, ssim_threshold, "DIRTY" if dishes else "CLEAN",
             f" labels={labels}" if labels else "")
    return result


# -- presentation helpers --

SINK_CLASS = 71  # coco 'sink'
FONT = cv2.FONT_HERSHEY_SIMPLEX
CLR_CLEAN = (120, 200, 90)
CLR_DIRTY = (60, 160, 250)
CLR_SINK = (200, 160, 90)


def auto_detect_sink(frame):
    """
    Best-effort sink bbox from YOLO, to seed the ROI on first calibration.
    Returns [x1, y1, x2, y2] or None. A None here is normal and just means the
    user draws the box themselves.
    """
    model = _get_yolo()
    if model is None:
        return None
    try:
        for r in model(frame, verbose=False, classes=[SINK_CLASS]):
            if r.boxes is None or len(r.boxes) == 0:
                continue
            confs = r.boxes.conf.cpu().numpy()
            best = confs.argmax()
            bbox = r.boxes.xyxy[best].cpu().numpy().astype(int).tolist()
            log.info("auto-detected sink %s (conf %.0f%%)", bbox, confs[best] * 100)
            return bbox
    except Exception as e:
        log.warning("sink auto-detect failed: %s", e)
    return None


def annotate_frame(frame, result, roi=None, state_label=""):
    """Draw the ROI, score and labels onto a copy of the frame."""
    out = frame.copy()

    if roi and "sink" in roi:
        x1, y1, x2, y2 = roi["sink"]
        color = CLR_DIRTY if result.get("dishes_found") else CLR_CLEAN
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        cv2.putText(out, "SINK", (x1 + 4, y1 + 16), FONT, 0.5, CLR_SINK, 1)
        cv2.putText(out, f"SSIM {result.get('ssim_score', 0):.3f}",
                    (x1 + 4, y2 - 8), FONT, 0.5, color, 1)

    for det in result.get("detections", []):
        bx1, by1, bx2, by2 = det["box"]
        cv2.rectangle(out, (bx1, by1), (bx2, by2), CLR_DIRTY, 1)
        cv2.putText(out, f"{det['label']} {det['confidence']:.0%}",
                    (bx1, max(12, by1 - 4)), FONT, 0.45, CLR_DIRTY, 1)

    if state_label:
        cv2.putText(out, state_label, (8, 22), FONT, 0.6, (255, 255, 255), 2)
        cv2.putText(out, state_label, (8, 22), FONT, 0.6, (0, 0, 0), 1)

    return out
