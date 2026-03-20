# config.py - persistent config system
# stores all settings in a json file. dashboard reads/writes via api.
# env vars set initial defaults, then config.json overrides.

import json
import logging
import os
from pathlib import Path
from threading import Lock

log = logging.getLogger("dishwatcher.config")

_lock = Lock()
_config = {}
_path = None

# every setting with its default, type, and description for the ui
SCHEMA = {
    # detection
    "ssim_threshold":       {"default": 0.82,  "type": "float", "min": 0.5, "max": 0.99, "step": 0.01, "group": "detection", "label": "SSIM threshold", "desc": "below this = dishes detected"},
    "yolo_enabled":         {"default": True,   "type": "bool",  "group": "detection", "label": "YOLO labeling", "desc": "run yolo to label detected objects"},
    "confidence_threshold": {"default": 0.40,   "type": "float", "min": 0.1, "max": 0.9, "step": 0.05, "group": "detection", "label": "YOLO confidence", "desc": "min confidence for yolo detections"},
    "counter_enabled":      {"default": False,  "type": "bool",  "group": "detection", "label": "counter detection", "desc": "track items on the counter separately"},
    "counter_ssim":         {"default": 0.80,   "type": "float", "min": 0.5, "max": 0.99, "step": 0.01, "group": "detection", "label": "counter SSIM threshold"},

    # camera
    "camera_rotation":      {"default": "180",  "type": "select", "options": ["NONE", "CW", "CCW", "180"], "group": "camera", "label": "rotation", "desc": "rotate incoming frames"},
    "jpeg_quality":         {"default": 90,     "type": "int",   "min": 30, "max": 100, "step": 5, "group": "camera", "label": "saved image quality"},

    # video
    "video_thumbnail":      {"default": True,   "type": "bool",  "group": "video", "label": "generate thumbnails", "desc": "save first frame as thumbnail for blame clips"},

    # timing
    "grace_minutes":        {"default": 90,     "type": "int",   "min": 5, "max": 480, "step": 5, "group": "timing", "label": "grace period (min)", "desc": "minutes before alert fires"},
    "consensus_window":     {"default": 7,      "type": "int",   "min": 3, "max": 15, "step": 1, "group": "timing", "label": "consensus window", "desc": "ring buffer size"},
    "consensus_threshold":  {"default": 5,      "type": "int",   "min": 2, "max": 15, "step": 1, "group": "timing", "label": "consensus threshold", "desc": "frames needed to agree"},

    # notifications
    "discord_webhook_url":  {"default": "",     "type": "string", "group": "notifications", "label": "discord webhook URL"},
    "discord_mention":      {"default": "",     "type": "string", "group": "notifications", "label": "discord mention", "desc": "e.g. <@userid> or <@&roleid>"},
    "notify_cooldown_min":  {"default": 30,     "type": "int",   "min": 5, "max": 180, "step": 5, "group": "notifications", "label": "alert cooldown (min)"},

    # ui
    "ui_show_chart":        {"default": True,   "type": "bool",  "group": "ui", "label": "show 24h chart"},
    "ui_show_consensus":    {"default": True,   "type": "bool",  "group": "ui", "label": "show consensus buffer"},
    "ui_show_timer":        {"default": True,   "type": "bool",  "group": "ui", "label": "show grace timer"},
    "ui_show_stats":        {"default": True,   "type": "bool",  "group": "ui", "label": "show stats panel"},
    "ui_show_events":       {"default": True,   "type": "bool",  "group": "ui", "label": "show event log"},

    # admin
    "admin_password":       {"default": "",     "type": "password", "group": "admin", "label": "dashboard password", "desc": "leave empty to disable"},
}

# map env var names to config keys (for initial defaults from docker-compose)
_ENV_MAP = {
    "SSIM_THRESHOLD": "ssim_threshold",
    "YOLO_ENABLED": "yolo_enabled",
    "CONFIDENCE_THRESHOLD": "confidence_threshold",
    "COUNTER_ENABLED": "counter_enabled",
    "COUNTER_SSIM_THRESHOLD": "counter_ssim",
    "CAMERA_ROTATION": "camera_rotation",
    "JPEG_QUALITY": "jpeg_quality",
    "GRACE_MINUTES": "grace_minutes",
    "CONSENSUS_WINDOW": "consensus_window",
    "CONSENSUS_THRESHOLD": "consensus_threshold",
    "DISCORD_WEBHOOK_URL": "discord_webhook_url",
    "DISCORD_MENTION": "discord_mention",
    "NOTIFY_COOLDOWN_MIN": "notify_cooldown_min",
    "ADMIN_PASSWORD": "admin_password",
}


def init(data_dir):
    """load config from disk, fill in defaults from env vars and schema"""
    global _config, _path
    _path = Path(data_dir) / "config.json"

    # start with schema defaults
    _config = {k: v["default"] for k, v in SCHEMA.items()}

    # override from env vars
    for env_key, config_key in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is not None:
            schema = SCHEMA[config_key]
            if schema["type"] == "bool":
                _config[config_key] = val.lower() in ("true", "1", "yes")
            elif schema["type"] == "int":
                _config[config_key] = int(val)
            elif schema["type"] == "float":
                _config[config_key] = float(val)
            else:
                _config[config_key] = val

    # override from saved config.json (highest priority)
    if _path.exists():
        try:
            saved = json.loads(_path.read_text())
            for k, v in saved.items():
                if k in SCHEMA:
                    _config[k] = v
            log.info("loaded config from %s", _path)
        except Exception as e:
            log.warning("bad config.json: %s", e)
    else:
        _save()
        log.info("created default config at %s", _path)


def _save():
    with _lock:
        os.makedirs(str(_path.parent), exist_ok=True)
        _path.write_text(json.dumps(_config, indent=2))


def get(key, default=None):
    return _config.get(key, default)


def get_all():
    """return all config values"""
    return dict(_config)


def get_schema():
    """return schema with current values for the settings ui"""
    result = {}
    for key, schema in SCHEMA.items():
        entry = dict(schema)
        entry["value"] = _config.get(key, schema["default"])
        # dont send actual password value to frontend
        if schema["type"] == "password":
            entry["value"] = "••••••" if entry["value"] else ""
        result[key] = entry
    return result


def update(changes):
    """update config from a dict of {key: value}. validates types. returns list of changed keys."""
    changed = []
    for key, val in changes.items():
        if key not in SCHEMA:
            continue
        schema = SCHEMA[key]

        # type coercion
        try:
            if schema["type"] == "bool":
                val = bool(val)
            elif schema["type"] == "int":
                val = int(val)
                if "min" in schema:
                    val = max(schema["min"], val)
                if "max" in schema:
                    val = min(schema["max"], val)
            elif schema["type"] == "float":
                val = float(val)
                if "min" in schema:
                    val = max(schema["min"], val)
                if "max" in schema:
                    val = min(schema["max"], val)
            elif schema["type"] == "select":
                if val not in schema.get("options", []):
                    continue
            elif schema["type"] == "password":
                # dont overwrite with the masked value
                if val == "••••••" or val == "":
                    if val == "":
                        _config[key] = ""
                        changed.append(key)
                    continue
        except (ValueError, TypeError):
            continue

        if _config.get(key) != val:
            _config[key] = val
            changed.append(key)

    if changed:
        _save()
        log.info("config updated: %s", changed)

    return changed


def check_password(password):
    """check admin password. returns True if no password set or if it matches."""
    stored = _config.get("admin_password", "")
    if not stored:
        return True
    return password == stored
