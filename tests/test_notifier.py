"""
Notifier tests.

An alert nobody receives is the exact failure this project exists to avoid, so
the delivery path is worth pinning: unconfigured must be a clean no-op, a
failure must be reported rather than swallowed, and ntfy must be attempted when
it is configured.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))


@pytest.fixture
def notif(monkeypatch):
    import importlib
    import notifier
    importlib.reload(notifier)
    return notifier


def test_unconfigured_is_a_noop_not_a_crash(notif, monkeypatch):
    monkeypatch.setattr(notif, "NTFY_URL", "")
    monkeypatch.setattr(notif, "DISCORD_URL", None)
    out = notif.send_alert("dishes")
    assert out == {"none": False}


def test_ntfy_is_attempted_when_configured(notif, monkeypatch):
    sent = {}

    class R:
        def raise_for_status(self): pass

    class S:
        def post(self, url, data=None, headers=None, timeout=None):
            sent["url"] = url
            sent["headers"] = headers
            sent["body"] = data
            return R()

    monkeypatch.setattr(notif, "NTFY_URL", "http://ntfy.test")
    monkeypatch.setattr(notif, "NTFY_TOPIC", "dishwatcher")
    monkeypatch.setattr(notif, "_sess", lambda: S())
    assert notif.send_ntfy("dishes have been sitting for 90 min") is True
    assert sent["url"] == "http://ntfy.test/dishwatcher"
    assert sent["headers"]["Title"]
    assert b"90 min" in sent["body"]


def test_ntfy_failure_is_reported_not_swallowed(notif, monkeypatch):
    class S:
        def post(self, *a, **k): raise OSError("network is down")

    monkeypatch.setattr(notif, "NTFY_URL", "http://ntfy.test")
    monkeypatch.setattr(notif, "_sess", lambda: S())
    # returns False rather than raising, so one dead channel cannot take the
    # request down, but it must not report success
    assert notif.send_ntfy("dishes") is False


def test_alert_reports_each_channel(notif, monkeypatch):
    monkeypatch.setattr(notif, "NTFY_URL", "http://ntfy.test")
    monkeypatch.setattr(notif, "DISCORD_URL", None)
    monkeypatch.setattr(notif, "send_ntfy", lambda *a, **k: True)
    assert notif.send_alert("dishes") == {"ntfy": True}
