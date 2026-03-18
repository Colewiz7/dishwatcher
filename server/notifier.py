# notifier.py - discord webhook alerts (optional)
# set DISCORD_WEBHOOK_URL env var to enable, otherwise its a no-op

import io
import json
import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger("dishwatcher.notifier")

DISCORD_URL     = os.environ.get("DISCORD_WEBHOOK_URL")
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


def send_alert(message, image_path=None):
    results = {}
    if DISCORD_URL:
        results["discord"] = send_discord(message, image_path)
    if not results:
        results["none"] = False
    return results


def send_clear_notification():
    return send_alert("dishes cleared, sink is clean")
