"""Exercise edge encoder failures without a camera, network, or ffmpeg process."""
import importlib
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "camera"))


@pytest.fixture
def watcher(monkeypatch, tmp_path):
    # Importing the daemon must not replace pytest's process signal handlers.
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    module = importlib.import_module("watcher")
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))
    return module


@pytest.mark.parametrize("result", ["error", "empty", "timeout"])
def test_failed_encoder_leaves_no_temporary_files(watcher, monkeypatch, tmp_path, result):
    def run(*args, **kwargs):
        if result == "timeout":
            raise watcher.subprocess.TimeoutExpired("ffmpeg", 60)
        return SimpleNamespace(returncode=1 if result == "error" else 0, stderr=b"test")
    monkeypatch.setattr(watcher.subprocess, "run", run)
    buffer = watcher.VideoBuffer(5, 5)
    buffer._buf.extend([b"jpeg"] * 5)
    assert buffer.encode_video() == (None, False)
    assert list(tmp_path.iterdir()) == []


def test_encoder_owns_unique_outputs_and_uses_one_thread(watcher, monkeypatch, tmp_path):
    def run(cmd, **kwargs):
        assert cmd[cmd.index("-threads") + 1] == "1"
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")
    monkeypatch.setattr(watcher.subprocess, "run", run)
    buffer = watcher.VideoBuffer(5, 5)
    buffer._buf.extend([b"jpeg"] * 5)
    first, ok = buffer.encode_video()
    second, ok2 = buffer.encode_video()
    assert ok and ok2 and first != second
    assert set(tmp_path.iterdir()) == {Path(first), Path(second)}


def test_failed_upload_releases_owned_clip(watcher, monkeypatch, tmp_path):
    path = tmp_path / "own-clip.mp4"
    path.write_bytes(b"mp4")
    buffer = SimpleNamespace(encode_video=lambda: (str(path), True))
    monkeypatch.setattr(watcher, "post_capture", lambda *args: None)
    assert watcher.send_visit(None, buffer) is None
    assert not path.exists()


def test_buffer_keeps_five_fps_phase_on_six_fps_camera(watcher):
    import numpy as np
    buffer = watcher.VideoBuffer(300, 5)
    frame = np.zeros((80, 120, 3), np.uint8)
    for i in range(360):
        buffer.maybe_add(frame, 100 + i / 6)
    assert 298 <= buffer.count <= 300
    assert 59 <= buffer.duration_seconds <= 61


def test_slow_camera_chunk_duration_uses_elapsed_time(watcher, monkeypatch):
    import numpy as np
    buffer = watcher.VideoBuffer(300, 5)
    frame = np.zeros((80, 120, 3), np.uint8)
    for i in range(240):
        buffer.maybe_add(frame, 100 + i / 4)
    assert buffer.count == 240
    assert 59 <= buffer.duration_seconds <= 61
    def run(cmd, **kwargs):
        assert cmd[cmd.index("-c:v") + 1] == "copy"
        assert 3.9 <= float(cmd[cmd.index("-framerate") + 1]) <= 4.1
        Path(cmd[-1]).write_bytes(b"avi")
        return SimpleNamespace(returncode=0, stderr=b"")
    monkeypatch.setattr(watcher.subprocess, "run", run)
    path, ok = buffer.encode_video()
    assert ok and path.endswith(".avi")


def test_long_activity_flushes_chunks_before_any_exit(watcher, monkeypatch):
    import numpy as np
    clock = SimpleNamespace(now=100.0)
    chunks = []
    frame = np.zeros((80, 120, 3), np.uint8)
    class Camera:
        def __init__(self, **kwargs): pass
        def read(self):
            clock.now += .2
            if clock.now >= 225:
                watcher._shutdown = True
            return True, frame
        def stats(self): return {"reopens": 0, "usb_resets": 0}
        def release(self): pass
    class Trigger:
        state = watcher.motion.IDLE
        def update(self, frame):
            if self.state == watcher.motion.IDLE:
                self.state = watcher.motion.MOTION
                return "entered"
        def stats(self): return {"entered_total": 1, "exited_total": 0, "flap_ratio": 0}
    class Job:
        def __init__(self, name): self.busy = False
        def submit(self, fn, *args):
            self.result, self.busy = fn(*args), True
        def take(self):
            if not self.busy: return False, None
            self.busy = False
            return True, self.result
        def close(self): pass
    monkeypatch.setattr(watcher, "_shutdown", False)
    monkeypatch.setattr(watcher, "SingleJob", Job)
    monkeypatch.setattr(watcher, "PROCESS_EVERY_N", 1)
    monkeypatch.setattr(watcher, "VIDEO_DURATION", 60)
    monkeypatch.setattr(watcher, "VIDEO_FPS", 5)
    monkeypatch.setattr(watcher, "BUFFER_SIZE", 300)
    monkeypatch.setattr(watcher.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(watcher.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(watcher.capture, "Camera", Camera)
    monkeypatch.setattr(watcher.motion, "MotionTrigger", Trigger)
    monkeypatch.setattr(watcher, "smoke_test", lambda: True)
    monkeypatch.setattr(watcher, "post_health", lambda *args: {})
    monkeypatch.setattr(watcher, "send_chunk", lambda buffer, metadata: chunks.append((buffer.count, buffer.duration_seconds, metadata)) or {"ok": True})
    watcher.main()
    assert len(chunks) == 2
    assert all(59 <= chunk[1] <= 61 for chunk in chunks)
    assert [chunk[2]["part"] for chunk in chunks] == [1, 2]
    assert chunks[0][2]["session_id"] == chunks[1][2]["session_id"]
