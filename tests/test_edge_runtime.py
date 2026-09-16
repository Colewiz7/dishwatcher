import sys
import threading
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "camera"))
from edge_runtime import ClipSession, SingleJob, preview_fps, small_frame, temperature_c


def test_cooking_visits_group_until_the_quiet_gap():
    session = ClipSession(gap_seconds=180)
    session.touch(100)
    first = session.metadata("2026-09-16T10:00:00+00:00")
    session.touch(200)
    second = session.metadata("2026-09-16T10:01:00+00:00")
    assert first["session_id"] == second["session_id"]
    assert (first["part"], second["part"]) == (1, 2)
    session.touch(381)
    third = session.metadata("2026-09-16T10:04:00+00:00")
    assert third["session_id"] != first["session_id"]
    assert third["part"] == 1


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


@pytest.mark.parametrize("bad", ["nan", "inf", "-1000", "999999"])
def test_invalid_temperature_uses_safe_fallback(tmp_path, bad):
    path = tmp_path / "temp"
    path.write_text(bad)
    assert temperature_c(path) is None
    assert preview_fps(2, temperature_c(path)) == 1


def test_odd_camera_dimensions_remain_encodable():
    frame = np.zeros((359, 639, 3), dtype=np.uint8)
    output = small_frame(frame)
    assert output.shape == (358, 638, 3)
    assert frame.shape == (359, 639, 3)


def test_failed_background_job_releases_slot(caplog):
    def fail():
        raise RuntimeError("encoder failed")
    worker = SingleJob("test-error")
    try:
        worker.submit(fail)
        with pytest.raises(RuntimeError):
            worker.future.result(timeout=2)
        assert worker.take() == (True, None)
        assert "background camera job failed" in caplog.text
        assert worker.submit(lambda: "recovered")
        worker.future.result(timeout=2)
        assert worker.take() == (True, "recovered")
    finally:
        worker.close()


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
