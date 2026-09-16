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


def test_camera_report_ages_between_reports(srv, monkeypatch):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(srv, "CAMERA", {"seen": True, "stats": {"seconds_since_last_frame": 1},
        "last_report": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()})
    cam = request(srv, "/status", headers=BASIC).json()["camera"]
    assert cam["report_age_seconds"] >= 120
    assert cam["seconds_since_last_frame"] >= 121


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
