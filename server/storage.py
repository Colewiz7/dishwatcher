# storage.py - saves annotated frames to disk
# uses a thread pool so imwrite doesnt block the request handler

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("dishwatcher.storage")

_save_dir = str(Path.home() / "dishwasher" / "images")
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="img-save")
_QUALITY = (cv2.IMWRITE_JPEG_QUALITY, 90)


def configure(save_dir):
    global _save_dir
    _save_dir = save_dir
    os.makedirs(_save_dir, exist_ok=True)
    log.info("image dir: %s", _save_dir)


def _write(path, frame):
    try:
        cv2.imwrite(path, frame, _QUALITY)
    except Exception as e:
        log.error("save failed %s: %s", path, e)


def save_frame(frame, dishes_found, state="", event=""):
    """queue a frame save. returns the filename."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "DISHES" if dishes_found else "clear"
    parts = [ts, tag]
    if state:
        parts.append(state)
    if event:
        parts.append(event)
    filename = "_".join(parts) + ".jpg"
    path = os.path.join(_save_dir, filename)

    # non-blocking, copy the frame so it doesnt get mutated
    _executor.submit(_write, path, frame.copy())
    return filename


def list_images(limit=40):
    try:
        files = sorted(
            (f for f in os.listdir(_save_dir) if f.lower().endswith(".jpg")),
            reverse=True)
    except FileNotFoundError:
        return []

    results = []
    for f in files[:limit]:
        try:
            ts = datetime.strptime(f[:15], "%Y%m%d_%H%M%S").strftime("%b %d  %H:%M:%S")
        except ValueError:
            ts = f
        results.append({
            "filename": f, "timestamp": ts,
            "dishes_found": "DISHES" in f, "url": f"/view/image/{f}",
        })
    return results


def get_latest_path():
    try:
        files = sorted(
            (f for f in os.listdir(_save_dir) if f.lower().endswith(".jpg")),
            reverse=True)
    except FileNotFoundError:
        return None
    return os.path.join(_save_dir, files[0]) if files else None


def get_image_path(filename):
    return os.path.join(_save_dir, Path(filename).name)
