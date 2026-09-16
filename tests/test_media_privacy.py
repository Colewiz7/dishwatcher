"""Authenticated users can watch remotely; anonymous clients cannot read media."""
import asyncio
import base64
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    d = tmp_path_factory.mktemp("data")
    os.environ["SAVE_DIR"] = str(d)
    os.environ["DASHBOARD_PASSWORD"] = "pw"
    os.environ["YOLO_ENABLED"] = "false"
    import server
    return server


def request(srv, path, *, headers=None, client="10.42.0.20", method="GET", **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=srv.app, client=(client, 1234))
        async with httpx.AsyncClient(transport=transport, base_url="https://sink.colewiz.dev") as c:
            return await c.request(method, path, headers=headers, **kwargs)
    return asyncio.run(run())


BASIC = {"Authorization": "Basic " + base64.b64encode(b"cole:pw").decode()}
MEDIA = ["/people/abc/photo", "/thumbs/test.jpg", "/videos/test.mp4",
         "/view/video/test.mp4", "/view/thumb/test.jpg", "/live.jpg"]


@pytest.mark.parametrize("path", MEDIA)
def test_anonymous_media_requires_login(srv, path):
    assert request(srv, path).status_code == 401


@pytest.mark.parametrize("path", MEDIA)
def test_public_hostname_does_not_block_authenticated_media(srv, path):
    response = request(srv, path, headers={**BASIC, "cf-ray": "test"})
    assert response.status_code not in (401, 403)


def test_proxy_identity_trusted_only_from_configured_network(srv, monkeypatch):
    monkeypatch.setattr(srv, "TRUST_FORWARD_AUTH", True)
    headers = {"x-authentik-username": "cole"}
    assert request(srv, "/clips", headers=headers).status_code == 200
    assert request(srv, "/clips", headers=headers, client="100.84.21.32").status_code == 401


def test_missing_credentials_fail_closed(srv, monkeypatch):
    monkeypatch.setattr(srv, "DASHBOARD_PASSWORD", "")
    monkeypatch.setattr(srv, "TRUST_FORWARD_AUTH", False)
    assert request(srv, "/live.jpg").status_code == 503


def test_video_range_and_thumbnails_work_remotely(srv, monkeypatch, tmp_path):
    payload = bytes(range(256)) * 20
    video = tmp_path / "test.mp4"
    video.write_bytes(payload)
    monkeypatch.setattr(srv.storage, "get_video_path", lambda name: str(video))
    response = request(srv, "/videos/test.mp4", headers={**BASIC, "Range": "bytes=100-199"})
    assert response.status_code == 206
    assert response.content == payload[100:200]
    assert response.headers["content-range"] == f"bytes 100-199/{len(payload)}"
    assert response.headers["cache-control"] == "private, no-store"


def test_clip_list_keeps_storage_thumbnail_url(srv, monkeypatch):
    monkeypatch.setattr(srv.storage, "list_videos", lambda limit: [
        {"filename": "clip.mp4", "thumb_url": "/view/thumb/clip_thumb.jpg"}])
    clip = request(srv, "/clips", headers=BASIC).json()["clips"][0]
    assert clip["thumb_url"] == "/view/thumb/clip_thumb.jpg"
    assert clip["url"] == "/videos/clip.mp4"


def test_clip_filters_apply_before_pagination(srv, monkeypatch):
    monkeypatch.setattr(srv.storage, "list_videos", lambda limit: [
        {"filename": "20260916_120000_blame.mp4"},
        {"filename": "20260916_110000_blame.mp4"},
        {"filename": "20260915_120000_blame.mp4"}])
    monkeypatch.setattr(srv.people, "tag_of", lambda name: {"person_id": "a", "name": "A"} if "110000" in name else None)
    result = request(srv, "/clips?day=2026-09-16&tagged=false&limit=1", headers=BASIC).json()
    assert result["total"] == 1 and not result["has_more"]
    assert result["clips"][0]["filename"] == "20260916_120000_blame.mp4"
    result = request(srv, "/clips?person=a", headers=BASIC).json()
    assert result["total"] == 1 and "110000" in result["clips"][0]["filename"]
    result = request(srv, "/clips?limit=1&offset=1", headers=BASIC).json()
    assert result["total"] == 3 and result["has_more"]
    assert "110000" in result["clips"][0]["filename"]


def test_chunk_upload_is_authenticated_and_does_not_change_detection(srv, monkeypatch):
    import json
    monkeypatch.setattr(srv, "API_KEY", "camera-key")
    monkeypatch.setattr(srv.clip_processing, "normalize_video", lambda raw, rotation: (b"browser mp4", 59.8))
    saved = {}
    def save(raw, filename, **kwargs):
        saved.update(kwargs)
        return "new.mp4", "new.jpg"
    monkeypatch.setattr(srv.storage, "save_video", save)
    original = dict(srv.LAST_DETECTION)
    files = {"video": ("clip.avi", b"jpeg stream", "video/x-msvideo")}
    assert request(srv, "/camera/clip", method="POST", files=files).status_code == 401
    response = request(srv, "/camera/clip", method="POST", files=files, headers={"X-API-Key": "camera-key"},
                       data={"clip_metadata": json.dumps({"session_id": "test-session", "part": 2, "recorded_at": "2026-09-16T10:00:00+00:00"})})
    assert response.status_code == 200
    assert saved["metadata"]["duration_seconds"] == 59.8
    assert saved["metadata"]["part"] == 2 and saved["rotation"] is None
    assert srv.LAST_DETECTION == original
    monkeypatch.setattr(srv, "API_KEY", None)
    assert request(srv, "/camera/clip", method="POST", files=files).status_code == 503


def test_live_snapshot_lease_freshness_and_auth(srv, monkeypatch):
    live = {"wanted_until": 0, "frame": None, "frame_at": 0, "seq": 0}
    monkeypatch.setattr(srv, "LIVE", live)
    assert request(srv, "/live.jpg").status_code == 401
    assert live["wanted_until"] == 0
    assert request(srv, "/live.jpg", headers=BASIC).status_code == 503
    assert 0 < live["wanted_until"] - time.time() <= srv.LIVE_LEASE_SEC
    live.update(frame=b"jpeg bytes", frame_at=time.time(), seq=10)
    response = request(srv, "/live.jpg", headers=BASIC)
    assert response.content == b"jpeg bytes"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-frame-seq"] == "10"
    assert "no-store" in response.headers["cache-control"]
    live["frame_at"] -= 30
    assert request(srv, "/live.jpg", headers=BASIC).status_code == 503


def test_camera_upload_still_requires_api_key(srv, monkeypatch):
    monkeypatch.setattr(srv, "API_KEY", "camera-secret")
    response = request(srv, "/live/frame", method="POST", files={"frame": ("test.jpg", b"jpeg", "image/jpeg")})
    assert response.status_code == 401


@pytest.mark.parametrize("rotation", [None, 0, 1, 2])
def test_live_orientation_matches_snapshot_processing(srv, monkeypatch, rotation):
    import cv2
    import numpy as np
    live = {"wanted_until": time.time() + 45, "frame": None, "frame_at": 0, "seq": 0}
    monkeypatch.setattr(srv, "LIVE", live)
    monkeypatch.setattr(srv, "API_KEY", "test-camera")
    monkeypatch.setattr(srv, "_get_rotation", lambda: rotation)
    original = np.zeros((80, 120, 3), np.uint8)
    original[:40, :60] = (0, 0, 255)
    original[40:, :60] = (255, 0, 0)
    original[:40, 60:] = (0, 255, 0)
    ok, jpeg = cv2.imencode(".jpg", original)
    assert ok
    response = request(srv, "/live/frame", method="POST", headers={"X-API-Key": "test-camera"},
                       files={"frame": ("live.jpg", jpeg.tobytes(), "image/jpeg")})
    assert response.status_code == 200
    actual = cv2.imdecode(np.frombuffer(live["frame"], np.uint8), cv2.IMREAD_COLOR)
    expected = srv._decode_frame(jpeg.tobytes())
    assert actual.shape == expected.shape
    assert np.abs(actual.astype(float) - expected).mean() < 5
    assert live["seq"] == 1


@pytest.mark.parametrize("payload,status", [(b"", 400), (b"not a jpeg", 422), (b"x" * (2 * 1024 * 1024 + 1), 413)],
                         ids=["empty", "invalid-jpeg", "oversized"])
def test_bad_preview_never_replaces_last_good_frame(srv, monkeypatch, payload, status):
    live = {"wanted_until": 0, "frame": b"last good", "frame_at": 123, "seq": 9}
    monkeypatch.setattr(srv, "LIVE", live)
    monkeypatch.setattr(srv, "API_KEY", "test-camera")
    response = request(srv, "/live/frame", method="POST", headers={"X-API-Key": "test-camera"},
                       files={"frame": ("live.jpg", payload, "image/jpeg")})
    assert response.status_code == status
    assert live["frame"] == b"last good"
    assert live["seq"] == 9


def test_unchanged_preview_renews_lease_without_resending_jpeg(srv, monkeypatch):
    live = {"wanted_until": 0, "frame": b"jpeg", "frame_at": time.time(), "seq": 7}
    monkeypatch.setattr(srv, "LIVE", live)
    first = request(srv, "/live.jpg", headers=BASIC)
    token = first.headers["x-frame-id"]
    response = request(srv, "/live.jpg", params={"after": token}, headers=BASIC)
    assert response.status_code == 204
    assert response.content == b""
    assert live["wanted_until"] > time.time()
    assert request(srv, "/live.jpg", params={"after": token}).status_code == 401
    # Sequence numbers can repeat after a restart; timestamps must distinguish them.
    live["frame_at"] += 0.001
    assert request(srv, "/live.jpg", params={"after": token}, headers=BASIC).status_code == 200
    live["frame_at"] -= 30
    assert request(srv, "/live.jpg", params={"after": token}, headers=BASIC).status_code == 503


def test_camera_report_ages_between_reports(srv, monkeypatch):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(srv, "CAMERA", {"seen": True, "stats": {"seconds_since_last_frame": 1},
        "last_report": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()})
    cam = request(srv, "/status", headers=BASIC).json()["camera"]
    assert cam["report_age_seconds"] >= 120
    assert cam["seconds_since_last_frame"] >= 121


def test_stored_snapshot_keeps_its_age_after_restart(srv, monkeypatch, tmp_path):
    import os
    frame = tmp_path / "old.jpg"
    frame.write_bytes(b"jpeg")
    captured = time.time() - 3600
    os.utime(frame, (captured, captured))
    monkeypatch.setattr(srv, "LAST_DETECTION", {})
    monkeypatch.setattr(srv.storage, "get_latest_image_path", lambda: str(frame))
    status = request(srv, "/status", headers=BASIC).json()
    assert status["latest_frame_url"] == "/images/old.jpg"
    assert 3600 <= status["latest_frame_age_seconds"] < 3605


def test_reference_never_uses_an_annotated_dashboard_frame(srv, monkeypatch, tmp_path):
    import numpy as np
    from calibration import Calibration
    calibration = Calibration(tmp_path)
    monkeypatch.setattr(srv, "calib", calibration)
    monkeypatch.setattr(srv, "LAST_RAW_FRAME", None)
    assert request(srv, "/calibration/reference", headers=BASIC, method="POST").status_code == 409
    raw = np.full((80, 80, 3), 100, dtype=np.uint8)
    monkeypatch.setattr(srv, "LAST_RAW_FRAME", raw)
    response = request(srv, "/calibration/reference", headers=BASIC, method="POST")
    assert response.status_code == 200
    assert response.json()["has_reference"]
    assert np.array_equal(calibration.reference, raw)
