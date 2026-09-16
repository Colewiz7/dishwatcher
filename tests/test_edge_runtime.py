import sys
import threading
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "camera"))
from edge_runtime import SingleJob, preview_fps, small_frame, temperature_c


@pytest.mark.parametrize("temp,fps", [(50, 2), (60, 1), (70, .5), (75, 0), (None, 1)])
def test_preview_backs_off_with_temperature(temp, fps):
    assert preview_fps(4, temp) == fps


def test_small_preview_preserves_full_detection_frame():
    original = np.zeros((720, 1280, 3), dtype=np.uint8)
    preview = small_frame(original)
    assert preview.shape == (360, 640, 3)
    assert original.shape == (720, 1280, 3)
    assert small_frame(preview) is preview


def test_temperature_sensor_absent_or_malformed(tmp_path):
    path = tmp_path / "temp"
    assert temperature_c(path) is None
    path.write_text("60148")
    assert temperature_c(path) == 60.148
    path.write_text("not ready")
    assert temperature_c(path) is None


def test_background_work_never_accumulates_frames():
    gate = threading.Event()
    worker = SingleJob("test")
    try:
        assert worker.submit(lambda: gate.wait(2))
        assert not worker.submit(lambda: "must not queue")
        assert worker.take() == (False, None)
        gate.set()
        worker.future.result(timeout=2)
        assert worker.take() == (True, True)
        assert not worker.busy
        assert worker.submit(lambda: 42)
        worker.future.result(timeout=2)
        assert worker.take() == (True, 42)
    finally:
        gate.set()
        worker.close()
