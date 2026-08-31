# storage.py - image + video + thumbnail persistence
# videos now get a thumbnail jpg and proper time/date labeling

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("dishwatcher.storage")

_img_dir = ""
_vid_dir = ""
_thumb_dir = ""
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="save")


def configure(base_dir):
    global _img_dir, _vid_dir, _thumb_dir
    _img_dir = os.path.join(base_dir, "images")
    _vid_dir = os.path.join(base_dir, "videos")
    _thumb_dir = os.path.join(base_dir, "thumbs")
    for d in (_img_dir, _vid_dir, _thumb_dir):
        os.makedirs(d, exist_ok=True)
    log.info("storage: images=%s videos=%s thumbs=%s", _img_dir, _vid_dir, _thumb_dir)


def _write_img(path, frame, quality=90):
    try:
        cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    except Exception as e:
        log.error("img save failed: %s", e)


def save_frame(frame, dishes_found, state="", quality=90):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "DISHES" if dishes_found else "clear"
    parts = [ts, tag]
    if state:
        parts.append(state)
    filename = "_".join(parts) + ".jpg"
    path = os.path.join(_img_dir, filename)
    _executor.submit(_write_img, path, frame.copy(), quality)
    return filename


def save_video(video_bytes, original_filename="clip.mp4",
               first_frame=None, rotation=None):
    """
    save a video clip + generate thumbnail.
    returns (video_filename, thumb_filename).
    """
    now = datetime.now()
    ts = now.strftime("%Y%m%d_%H%M%S")
    ext = os.path.splitext(original_filename)[1] or ".mp4"
    video_filename = f"{ts}_blame{ext}"
    thumb_filename = f"{ts}_blame_thumb.jpg"

    video_path = os.path.join(_vid_dir, video_filename)
    thumb_path = os.path.join(_thumb_dir, thumb_filename)

    # save video
    with open(video_path, "wb") as f:
        f.write(video_bytes)
    size_kb = len(video_bytes) / 1024
    log.info("video: %s (%.0f KB)", video_filename, size_kb)

    # generate thumbnail from first_frame or extract from video
    def _make_thumb():
        try:
            if first_frame is not None:
                thumb = first_frame.copy()
            else:
                # extract first frame from video
                cap = cv2.VideoCapture(video_path)
                ret, thumb = cap.read()
                cap.release()
                if not ret or thumb is None:
                    return

            # apply rotation to thumbnail too
            if rotation is not None:
                thumb = cv2.rotate(thumb, rotation)

            # burn timestamp into thumbnail
            h, w = thumb.shape[:2]
            time_str = now.strftime("%I:%M %p")
            date_str = now.strftime("%b %d, %Y")
            label = f"{time_str}  {date_str}"

            # dark bar at bottom
            bar_h = 28
            cv2.rectangle(thumb, (0, h - bar_h), (w, h), (0, 0, 0), -1)
            cv2.putText(thumb, label, (8, h - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

            cv2.imwrite(thumb_path, thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
            log.info("thumb: %s", thumb_filename)
        except Exception as e:
            log.warning("thumb failed: %s", e)

    _executor.submit(_make_thumb)

    return video_filename, thumb_filename


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
            dt = datetime.strptime(f[:15], "%Y%m%d_%H%M%S")
            time_str = dt.strftime("%I:%M %p")
            date_str = dt.strftime("%b %d")
            ts_display = f"{time_str}, {date_str}"
        except ValueError:
            ts_display = f
        results.append({
            "filename": f, "timestamp": ts_display,
            "dishes_found": "DISHES" in f, "url": f"/view/image/{f}",
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
            dt = datetime.strptime(f[:15], "%Y%m%d_%H%M%S")
            time_str = dt.strftime("%I:%M %p")
            date_str = dt.strftime("%b %d")
            ts_display = f"{time_str}, {date_str}"
        except ValueError:
            ts_display = f

        size = os.path.getsize(os.path.join(_vid_dir, f))

        # check for matching thumbnail
        thumb_name = f.rsplit(".", 1)[0] + "_thumb.jpg"
        # handle case where video is blame.mp4 -> blame_thumb.jpg
        if not os.path.exists(os.path.join(_thumb_dir, thumb_name)):
            base = f[:15] + "_blame_thumb.jpg"
            thumb_name = base if os.path.exists(os.path.join(_thumb_dir, base)) else None
        thumb_url = f"/view/thumb/{thumb_name}" if thumb_name else None

        results.append({
            "filename": f, "timestamp": ts_display,
            "size_kb": round(size / 1024),
            "url": f"/view/video/{f}",
            "thumb_url": thumb_url,
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

def get_thumb_path(filename):
    return os.path.join(_thumb_dir, Path(filename).name)


# -- v2 additions --

def latest_frame():
    """
    The most recent captured frame as a BGR array, or None.

    Used by the dashboard's "set clean reference" button: the reference is
    always a real frame the camera actually produced, so it cannot disagree
    with the live view's geometry.
    """
    import cv2
    path = get_latest_image_path()
    if not path:
        return None
    return cv2.imread(str(path))


def total_bytes():
    """Bytes held under the data directory, for the storage metric."""
    total = 0
    for d in (_img_dir, _vid_dir, _thumb_dir):
        if not d:
            continue
        p = Path(d)
        if not p.exists():
            continue
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return total


def oldest_clip_age_seconds():
    p = Path(_vid_dir) if _vid_dir else None
    if not p or not p.exists():
        return 0.0
    files = [f for f in p.glob("*") if f.is_file()]
    if not files:
        return 0.0
    return time.time() - min(f.stat().st_mtime for f in files)


def enforce_retention(clip_days, image_days):
    """
    Delete media past its retention window.

    v1 had no cleanup at all and its own TODO admitted it. This is blame-clip
    footage of people in a home, so unbounded retention is a real problem and
    not just a disk one. Returns a summary for logging and metrics.
    """
    now = time.time()
    removed = {"clips": 0, "images": 0, "bytes": 0}

    for directory, days, key in ((_vid_dir, clip_days, "clips"),
                                 (_img_dir, image_days, "images"),
                                 (_thumb_dir, image_days, "images")):
        if not directory or days <= 0:
            continue
        cutoff = now - days * 86400
        p = Path(directory)
        if not p.exists():
            continue
        for f in p.glob("*"):
            if not f.is_file():
                continue
            try:
                st = f.stat()
                if st.st_mtime < cutoff:
                    size = st.st_size
                    f.unlink()
                    removed[key] += 1
                    removed["bytes"] += size
            except OSError as e:
                log.warning("retention could not remove %s: %s", f, e)

    if removed["clips"] or removed["images"]:
        log.info("retention removed %d clips and %d images (%.1f MB)",
                 removed["clips"], removed["images"], removed["bytes"] / 1e6)
    return removed
