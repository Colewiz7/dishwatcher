import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from clip_processing import normalize_video


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_camera_mjpeg_becomes_upright_browser_h264(tmp_path):
    source = tmp_path / "camera.avi"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"MJPG"), 5, (160, 96))
    assert writer.isOpened()
    frame = np.zeros((96, 160, 3), np.uint8)
    frame[:48] = (0, 0, 255)
    frame[48:] = (255, 0, 0)
    for _ in range(10):
        writer.write(frame)
    writer.release()
    result, duration = normalize_video(source.read_bytes(), cv2.ROTATE_180)
    assert 1.9 <= duration <= 2.1
    output = tmp_path / "browser.mp4"
    output.write_bytes(result)
    cap = cv2.VideoCapture(str(output))
    ok, decoded = cap.read()
    cap.release()
    assert ok and decoded.shape == (384, 640, 3)
    assert decoded[40, 40, 0] > 230  # Rotation puts blue at the top.
    assert decoded[300, 40, 2] > 230


def test_clip_payload_is_bounded():
    with pytest.raises(ValueError):
        normalize_video(b"")
    with pytest.raises(ValueError):
        normalize_video(b"x" * (32 * 1024 * 1024 + 1))
