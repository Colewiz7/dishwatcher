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
