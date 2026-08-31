"""
Roommate photos and blame clips must never be served over the internet.

These are pictures and video of people who live in the flat and did not sign up
to be on a public host. "It is behind SSO" is a weaker promise than "it never
leaves the local network", and this is the second one. The roster itself (names
and counts) is fine over the tunnel; the media is not.
"""
import sys
from pathlib import Path

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


PHOTO = "/people/abc123def456/photo"
CLIP = "/videos/20260101_000000_blame.mp4"
THUMB = "/thumbs/20260101_000000.jpg"


@pytest.mark.parametrize("path", [PHOTO, CLIP, THUMB])
def test_media_paths_are_marked_local_only(srv, path):
    assert srv._is_local_only_path(path), f"{path} must be local-only"


@pytest.mark.parametrize("path", ["/people", "/status", "/clips", "/metrics", "/"])
def test_non_media_paths_are_not_restricted(srv, path):
    """The roster and the dashboard itself still work remotely."""
    assert not srv._is_local_only_path(path), f"{path} should not be restricted"


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_authentik_proxied_requests_count_as_internet(srv):
    assert srv._came_from_the_internet(_Req({"x-authentik-username": "cole"}))


def test_public_hostname_counts_as_internet(srv):
    assert srv._came_from_the_internet(_Req({"host": "sink.colewiz.dev"}))
    assert srv._came_from_the_internet(_Req({"host": "sink.colewiz.dev:443"}))


def test_cloudflare_edge_headers_count_as_internet(srv):
    assert srv._came_from_the_internet(_Req({"cf-ray": "abc"}))
    assert srv._came_from_the_internet(_Req({"cf-connecting-ip": "1.2.3.4"}))


def test_a_tailnet_request_is_not_the_internet(srv):
    assert not srv._came_from_the_internet(_Req({"host": "100.103.249.113:30820"}))
    assert not srv._came_from_the_internet(_Req({"host": "localhost:8000"}))


def test_detection_needs_only_one_signal(srv):
    """
    Any single signal is enough. Something inside the cluster could strip one,
    so they must not be required together.
    """
    for h in ({"x-authentik-username": "cole"},
              {"host": "sink.colewiz.dev"},
              {"cf-ray": "x"}):
        assert srv._came_from_the_internet(_Req(h)), h
