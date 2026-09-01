# people.py - the roommate roster, and who a clip gets attributed to.
#
# The clips were already being recorded and there was no way to look at them or
# say who was in one, which makes them evidence nobody can read. This stores a
# small roster (name + photo) and a mapping from clip filename to person.
#
# Attribution is a deliberate choice, not a guess. Nothing here runs face
# recognition: on this estate that would mean InsightFace on the GPU, and the
# useful property of a shared flat is that a wrong accusation is much worse
# than a missing one. So a human tags the clip, and the roster is the enrollment
# step that automatic matching could later build on.

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("dishwatcher.people")

_lock = threading.Lock()
_dir = None
_roster_path = None
_tags_path = None
_photos_dir = None
_roster = {}
_tags = {}

MAX_PHOTO_BYTES = 4 * 1024 * 1024


def configure(data_dir):
    global _dir, _roster_path, _tags_path, _photos_dir, _roster, _tags
    _dir = Path(data_dir)
    _roster_path = _dir / "people.json"
    _tags_path = _dir / "clip_tags.json"
    _photos_dir = _dir / "people"
    _photos_dir.mkdir(parents=True, exist_ok=True)

    _roster = _read(_roster_path, {})
    _tags = _read(_tags_path, {})
    log.info("roster: %d people, %d tagged clips", len(_roster), len(_tags))


def _read(path, default):
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            log.warning("could not read %s: %s", path, e)
    return dict(default)


def _write(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)          # atomic, so a crash cannot leave half a roster


# -- roster --

def list_people():
    with _lock:
        return [
            {"id": pid, "name": p["name"], "has_photo": bool(p.get("photo")),
             "photo_url": f"/people/{pid}/photo" if p.get("photo") else None,
             "added": p.get("added")}
            for pid, p in sorted(_roster.items(), key=lambda kv: kv[1]["name"].lower())
        ]


def add_person(name):
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > 60:
        raise ValueError("name is too long")
    with _lock:
        for p in _roster.values():
            if p["name"].lower() == name.lower():
                raise ValueError(f"{name} is already on the roster")
        pid = uuid.uuid4().hex[:12]
        _roster[pid] = {"name": name, "photo": None,
                        "added": datetime.now(timezone.utc).isoformat()}
        _write(_roster_path, _roster)
    log.info("added %s to the roster", name)
    return pid


def set_photo(pid, raw, content_type="image/jpeg"):
    if pid not in _roster:
        raise KeyError(pid)
    if not raw:
        raise ValueError("empty photo")
    if len(raw) > MAX_PHOTO_BYTES:
        raise ValueError("photo is larger than 4MB")
    ext = ".png" if "png" in (content_type or "") else ".jpg"
    path = _photos_dir / f"{pid}{ext}"
    path.write_bytes(raw)
    with _lock:
        _roster[pid]["photo"] = path.name
        _write(_roster_path, _roster)
    log.info("photo set for %s", _roster[pid]["name"])
    return path.name


def photo_path(pid):
    with _lock:
        p = _roster.get(pid)
    if not p or not p.get("photo"):
        return None
    path = _photos_dir / p["photo"]
    return path if path.is_file() else None


def remove_person(pid):
    with _lock:
        p = _roster.pop(pid, None)
        if p is None:
            raise KeyError(pid)
        if p.get("photo"):
            try:
                (_photos_dir / p["photo"]).unlink(missing_ok=True)
            except OSError:
                pass
        # drop their tags too, so no clip points at somebody who is gone
        for clip in [c for c, t in _tags.items() if t.get("person_id") == pid]:
            _tags.pop(clip, None)
        _write(_roster_path, _roster)
        _write(_tags_path, _tags)
    log.info("removed %s from the roster", p["name"])


def name_of(pid):
    with _lock:
        p = _roster.get(pid)
    return p["name"] if p else None


# -- attribution --

def tag_clip(clip, pid, by="dashboard"):
    """Attribute a clip to someone. pid None clears it."""
    with _lock:
        if pid is None:
            _tags.pop(clip, None)
        else:
            if pid not in _roster:
                raise KeyError(pid)
            _tags[clip] = {"person_id": pid, "by": by,
                           "at": datetime.now(timezone.utc).isoformat()}
        _write(_tags_path, _tags)
    return _tags.get(clip)


def tag_of(clip):
    with _lock:
        t = _tags.get(clip)
        if not t:
            return None
        p = _roster.get(t["person_id"])
        return {**t, "name": p["name"] if p else "(removed)"}


def counts():
    """How many clips each person is on the hook for."""
    with _lock:
        out = {pid: 0 for pid in _roster}
        for t in _tags.values():
            if t["person_id"] in out:
                out[t["person_id"]] += 1
        return {_roster[pid]["name"]: n for pid, n in out.items()}


def forget_clip(clip):
    """Called by retention so tags do not outlive the footage."""
    with _lock:
        if _tags.pop(clip, None) is not None:
            _write(_tags_path, _tags)
