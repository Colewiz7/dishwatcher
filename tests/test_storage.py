"""Thumbnail orientation and sizing must agree with detection snapshots."""
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import storage


@pytest.mark.parametrize("from_video", [False, True])
def test_thumbnail_rotates_only_unprocessed_video_frame(tmp_path, monkeypatch, from_video):
    monkeypatch.setattr(storage, "_vid_dir", str(tmp_path))
    monkeypatch.setattr(storage, "_thumb_dir", str(tmp_path))
    monkeypatch.setattr(storage, "_executor", SimpleNamespace(submit=lambda fn: fn()))
    oriented = np.zeros((300, 600, 3), np.uint8)
    oriented[:150] = (0, 0, 255)
    oriented[150:] = (255, 0, 0)
    if from_video:
        raw = cv2.rotate(oriented, cv2.ROTATE_180)
        monkeypatch.setattr(cv2, "VideoCapture", lambda _: SimpleNamespace(read=lambda: (True, raw), release=lambda: None))
    _, filename = storage.save_video(b"test", first_frame=None if from_video else oriented,
                                     rotation=cv2.ROTATE_180)
    thumb = cv2.imread(str(tmp_path / filename))
    assert thumb.shape == (240, 480, 3)
    assert thumb[40, 40, 2] > 240  # Red remains at the top.
    assert thumb[180, 40, 0] > 240  # Blue remains at the bottom, above the label.


def test_two_week_retention_preserves_settings_and_recent_media(tmp_path, monkeypatch):
    import os
    import time
    for name, attr in [("videos", "_vid_dir"), ("images", "_img_dir"), ("thumbs", "_thumb_dir")]:
        folder = tmp_path / name
        folder.mkdir()
        monkeypatch.setattr(storage, attr, str(folder))
        old = folder / ("old.mp4" if name == "videos" else "old.jpg")
        old.write_bytes(b"old")
        os.utime(old, (time.time() - 15 * 86400,) * 2)
        (folder / "recent.jpg").write_bytes(b"new")
    protected = tmp_path / "reference.jpg"
    protected.write_bytes(b"calibration")
    os.utime(protected, (time.time() - 100 * 86400,) * 2)
    (tmp_path / "images" / "linked.jpg").symlink_to(protected)
    result = storage.enforce_retention(14, 14)
    assert result["clips"] == 1 and result["images"] == 2
    assert result["removed_clips"] == ["old.mp4"]
    assert protected.read_bytes() == b"calibration"
    assert (tmp_path / "images" / "linked.jpg").is_symlink()
    assert all((tmp_path / name / "recent.jpg").exists() for name in ("videos", "images", "thumbs"))


def test_clip_listing_includes_session_metadata_and_handles_old_names(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(storage, "_vid_dir", str(tmp_path))
    monkeypatch.setattr(storage, "_thumb_dir", str(tmp_path))
    name = "20260916_120000_123456_blame.mp4"
    (tmp_path / name).write_bytes(b"video")
    (tmp_path / (name + ".json")).write_text(json.dumps({"session_id": "s1", "part": 2, "duration_seconds": 59.8}))
    (tmp_path / "legacy.mp4").write_bytes(b"old")
    clips = storage.list_videos(limit=None)
    clip = next(c for c in clips if c["filename"] == name)
    assert clip["session_id"] == "s1" and clip["part"] == 2
    assert clip["duration_seconds"] == 59.8
    assert clip["recorded_at"] == "2026-09-16T12:00:00"
    assert next(c for c in clips if c["filename"] == "legacy.mp4")["recorded_at"] is None
