# notifier.py - alerts to ntfy and/or discord.
#
# ntfy is the one that matters here: the estate already runs it, alertmanager
# already pushes to it, and it is what actually reaches a phone. Discord stays
# because it was here first and costs nothing to keep.
#
# Both are optional and independently no-ops when unconfigured. An alert that
# cannot be delivered is logged at WARNING rather than swallowed, because the
# whole point of this project was that a silent failure looked like success.

import io
import json
import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger("dishwatcher.notifier")

DISCORD_URL     = os.environ.get("DISCORD_WEBHOOK_URL")

# ntfy. NTFY_URL is the base, NTFY_TOPIC the topic to publish to.
NTFY_URL       = os.environ.get("NTFY_URL", "").rstrip("/")
NTFY_TOPIC     = os.environ.get("NTFY_TOPIC", "dishwatcher")
NTFY_PRIORITY  = os.environ.get("NTFY_PRIORITY", "default")
# Where the notification should take you when tapped. Deliberately the public
# hostname, since that is what resolves from a phone off the home network.
NTFY_CLICK     = os.environ.get("NTFY_CLICK", "")
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "")
COOLDOWN_MIN    = float(os.environ.get("NOTIFY_COOLDOWN_MIN", "30"))

_last_notify = 0.0
_session = None


def _sess():
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def send_discord(message, image_path=None, color=0xFF6B6B):
    if not DISCORD_URL:
        return False

    # dont spam
    global _last_notify
    elapsed = (time.monotonic() - _last_notify) / 60
    if _last_notify > 0 and elapsed < COOLDOWN_MIN:
        log.debug("notification suppressed (cooldown)")
        return False

    content = f"{DISCORD_MENTION} {message}".strip() if DISCORD_MENTION else message
    payload = {
        "content": content,
        "embeds": [{"title": "Dish Watcher", "description": message,
                     "color": color, "footer": {"text": "dishwatcher v4"}}],
    }

    files_dict = {}
    if image_path and os.path.isfile(image_path):
        payload["embeds"][0]["image"] = {"url": "attachment://frame.jpg"}
        with open(image_path, "rb") as f:
            files_dict["file"] = ("frame.jpg", io.BytesIO(f.read()), "image/jpeg")

    try:
        if files_dict:
            r = _sess().post(DISCORD_URL,
                             data={"payload_json": json.dumps(payload)},
                             files=files_dict, timeout=15)
        else:
            r = _sess().post(DISCORD_URL, json=payload, timeout=15)

        if r.status_code in (200, 204):
            _last_notify = time.monotonic()
            log.info("discord sent")
            return True
        log.error("discord http %d: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("discord failed: %s", e)
    return False


def send_ntfy(message, image_path=None, title="Dishes in the sink",
              priority=None, tags="warning"):
    """
    Publish to ntfy. Returns True if it was accepted.

    The captured frame goes as the notification body when there is one, so the
    phone shows the sink rather than just a sentence about it. ntfy takes the
    image as a raw PUT body with the metadata in headers.
    """
    if not NTFY_URL:
        return False

    url = f"{NTFY_URL}/{NTFY_TOPIC}"
    headers = {
        "Title": title,
        "Priority": priority or NTFY_PRIORITY,
        "Tags": tags,
    }
    if NTFY_CLICK:
        headers["Click"] = NTFY_CLICK

    try:
        if image_path and os.path.isfile(image_path):
            # image as the body; the text rides along in a header
            headers["Filename"] = os.path.basename(image_path)
            headers["Message"] = message
            with open(image_path, "rb") as f:
                r = _sess().put(url, data=f, headers=headers, timeout=20)
        else:
            r = _sess().post(url, data=message.encode("utf-8"),
                             headers=headers, timeout=15)
        r.raise_for_status()
        log.info("ntfy: %s", message)
        return True
    except Exception as e:
        # loud, not swallowed: an alert nobody receives is the failure mode
        # this whole project exists to avoid
        log.warning("ntfy delivery failed (%s): %s", url, e)
        return False


def send_alert(message, image_path=None):
    results = {}
    if NTFY_URL:
        results["ntfy"] = send_ntfy(message, image_path)
    if DISCORD_URL:
        results["discord"] = send_discord(message, image_path)
    if not results:
        # not an error, but worth saying once: the grace period expired and
        # nothing was configured to tell anybody
        log.warning("dishes alert fired with no notifier configured: %s", message)
        results["none"] = False
    return results


def send_clear_notification():
    return send_alert("dishes cleared, sink is clean")


def send_clear(message="Sink is clear"):
    """The all-clear, quietly. Priority low so it does not buzz a phone."""
    out = {}
    if NTFY_URL:
        out["ntfy"] = send_ntfy(message, title="Sink is clear",
                                priority="low", tags="white_check_mark")
    return out