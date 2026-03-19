# storage.py - image + video persistence
# images saved in a thread pool so we dont block the request
# videos saved directly (already in a file from the upload)

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("dishwatcher.storage")

_img_dir = ""
_vid_dir = ""
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="save")
_QUALITY = (cv2.IMWRITE_JPEG_QUALITY, 90)


def configure(base_dir):
    global _img_dir, _vid_dir
    _img_dir = os.path.join(base_dir, "images")
    _vid_dir = os.path.join(base_dir, "videos")
    os.makedirs(_img_dir, exist_ok=True)
    os.makedirs(_vid_dir, exist_ok=True)
    log.info("images: %s | videos: %s", _img_dir, _vid_dir)


def _write_img(path, frame):
    try:
        cv2.imwrite(path, frame, _QUALITY)
    except Exception as e:
        log.error("img save failed: %s", e)


def save_frame(frame, dishes_found, state=""):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "DISHES" if dishes_found else "clear"
    parts = [ts, tag]
    if state:
        parts.append(state)
    filename = "_".join(parts) + ".jpg"
    path = os.path.join(_img_dir, filename)
    _executor.submit(_write_img, path, frame.copy())
    return filename


def save_video(video_bytes, original_filename="clip.mp4"):
    """save an uploaded video clip. returns the filename."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = os.path.splitext(original_filename)[1] or ".mp4"
    filename = f"{ts}_blame{ext}"
    path = os.path.join(_vid_dir, filename)
    with open(path, "wb") as f:
        f.write(video_bytes)
    size_kb = len(video_bytes) / 1024
    log.info("video saved: %s (%.0f KB)", filename, size_kb)
    return filename


def list_images(limit=40):
    try:
        files = sorted(
            (f for f in os.listdir(_img_dir) if f.lower().endswith(".jpg")),
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
            "dishes_found": "DISHES" in f,
            "url": f"/view/image/{f}",
        })
    return results


def list_videos(limit=20):
    try:
        files = sorted(
            (f for f in os.listdir(_vid_dir)
             if f.lower().endswith((".mp4", ".avi"))),
            reverse=True)
    except FileNotFoundError:
        return []

    results = []
    for f in files[:limit]:
        try:
            ts = datetime.strptime(f[:15], "%Y%m%d_%H%M%S").strftime("%b %d  %H:%M:%S")
        except ValueError:
            ts = f
        size = os.path.getsize(os.path.join(_vid_dir, f))
        results.append({
            "filename": f, "timestamp": ts,
            "size_kb": round(size / 1024),
            "url": f"/view/video/{f}",
        })
    return results


def get_latest_image_path():
    try:
        files = sorted(
            (f for f in os.listdir(_img_dir) if f.lower().endswith(".jpg")),
            reverse=True)
    except FileNotFoundError:
        return None
    return os.path.join(_img_dir, files[0]) if files else None


def get_image_path(filename):
    return os.path.join(_img_dir, Path(filename).name)


def get_video_path(filename):
    return os.path.join(_vid_dir, Path(filename).name)
