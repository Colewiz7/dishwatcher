# state_machine.py - consensus engine + state tracking
# instead of trusting a single yolo frame, we keep a ring buffer of the last N
# results and only change state when a supermajority agrees. kills false positives.
#
# states: CLEAR -> DETECTED -> CONFIRMED -> (grace timer) -> ALERTED
# consensus clears at any point = back to CLEAR

import logging
import os
import sqlite3
from collections import deque
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger("dishwatcher.state")


class DishState(str, Enum):
    CLEAR     = "CLEAR"
    DETECTED  = "DETECTED"
    CONFIRMED = "CONFIRMED"
    ALERTED   = "ALERTED"


# config
CONSENSUS_WINDOW    = int(os.environ.get("CONSENSUS_WINDOW", "7"))
CONSENSUS_THRESHOLD = int(os.environ.get("CONSENSUS_THRESHOLD", "5"))
GRACE_MINUTES       = float(os.environ.get("GRACE_MINUTES", "90"))
DB_PATH             = os.environ.get(
    "DB_PATH", str(Path.home() / "dishwasher" / "dishwatcher.db"))


class ConsensusBuffer:
    """ring buffer that tracks recent detection results (true/false)"""
    __slots__ = ("window", "threshold", "_buf")

    def __init__(self, window, threshold):
        self.window = window
        self.threshold = threshold
        self._buf = deque(maxlen=window)

    def push(self, v):
        self._buf.append(v)

    @property
    def pos(self):
        return sum(self._buf)

    @property
    def neg(self):
        return len(self._buf) - self.pos

    @property
    def confidence(self):
        return self.pos / len(self._buf) if self._buf else 0.0

    def dishes(self):
        return self.pos >= self.threshold

    def clear(self):
        return self.neg >= self.threshold

    def reset(self):
        self._buf.clear()

    def snapshot(self):
        return {
            "buffer": list(self._buf), "size": len(self._buf),
            "window": self.window, "threshold": self.threshold,
            "positive": self.pos, "negative": self.neg,
            "confidence": round(self.confidence, 3),
        }


# -- sqlite setup --

_SCHEMA = """
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    PRAGMA cache_size=-8000;

    CREATE TABLE IF NOT EXISTS state (
        id            INTEGER PRIMARY KEY CHECK (id = 1),
        current_state TEXT NOT NULL DEFAULT 'CLEAR',
        entered_at    TEXT NOT NULL,
        dishes_since  TEXT,
        updated_at    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS detections (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp      TEXT NOT NULL,
        dishes_found   INTEGER NOT NULL,
        detection_count INTEGER NOT NULL DEFAULT 0,
        labels         TEXT,
        confidence_avg REAL,
        inference_ms   REAL,
        image_file     TEXT,
        consensus      REAL
    );

    CREATE TABLE IF NOT EXISTS events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp  TEXT NOT NULL,
        from_state TEXT NOT NULL,
        to_state   TEXT NOT NULL,
        reason     TEXT
    );

    CREATE TABLE IF NOT EXISTS alerts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp  TEXT NOT NULL,
        channel    TEXT NOT NULL,
        success    INTEGER NOT NULL,
        message    TEXT,
        image_file TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_det_ts ON detections(timestamp);
    CREATE INDEX IF NOT EXISTS idx_det_dishes ON detections(dishes_found);
    CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);
"""

def _init_db(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO state (id, current_state, entered_at, updated_at) "
        "VALUES (1, 'CLEAR', ?, ?)", (now, now))
    conn.commit()
    return conn


# -- state machine --

class DishStateMachine:

    def __init__(self, db_path=DB_PATH, window=CONSENSUS_WINDOW,
                 threshold=CONSENSUS_THRESHOLD, grace=GRACE_MINUTES):
        self.db = _init_db(db_path)
        self.consensus = ConsensusBuffer(window, threshold)
        self.grace_minutes = grace

        # load persisted state from db
        row = self.db.execute("SELECT * FROM state WHERE id = 1").fetchone()
        self._state = DishState(row["current_state"])
        self._entered = datetime.fromisoformat(row["entered_at"])
        self._dishes_since = (
            datetime.fromisoformat(row["dishes_since"])
            if row["dishes_since"] else None)

        log.info("state: %s | consensus %d/%d | grace %s min",
                 self._state.value, threshold, window, grace)

    @property
    def state(self):
        return self._state

    @property
    def dishes_since(self):
        return self._dishes_since

    @property
    def grace_remaining(self):
        if self._state != DishState.CONFIRMED or not self._dishes_since:
            return None
        deadline = self._dishes_since + timedelta(minutes=self.grace_minutes)
        return max(deadline - datetime.utcnow(), timedelta(0))

    def update(self, dishes_found, detection_count=0, labels=None,
               confidence_avg=0.0, inference_ms=0.0, image_file=""):
        """feed a frame result into the state machine. returns status dict."""
        now = datetime.utcnow()
        self.consensus.push(dishes_found)

        # log to db
        self.db.execute(
            "INSERT INTO detections "
            "(timestamp,dishes_found,detection_count,labels,confidence_avg,"
            "inference_ms,image_file,consensus) VALUES (?,?,?,?,?,?,?,?)",
            (now.isoformat(), int(dishes_found), detection_count,
             ",".join(labels or []), confidence_avg, inference_ms,
             image_file, self.consensus.confidence))
        self.db.commit()

        old = self._state
        should_alert = False

        # -- state transitions --
        if self._state == DishState.CLEAR:
            if self.consensus.dishes():
                self._transition(DishState.DETECTED, "consensus: dishes")
                self._dishes_since = now
                self._transition(DishState.CONFIRMED, "consensus confirmed")

        elif self._state == DishState.DETECTED:
            if self.consensus.dishes():
                self._transition(DishState.CONFIRMED, "consensus confirmed")
                if not self._dishes_since:
                    self._dishes_since = now
            elif self.consensus.clear():
                self._dishes_since = None
                self._transition(DishState.CLEAR, "false alarm")

        elif self._state == DishState.CONFIRMED:
            if self.consensus.clear():
                self._dishes_since = None
                self._transition(DishState.CLEAR, "cleared before timer")
            elif self._dishes_since:
                mins = (now - self._dishes_since).total_seconds() / 60
                if mins >= self.grace_minutes:
                    self._transition(DishState.ALERTED, f"grace expired ({mins:.0f} min)")
                    should_alert = True

        elif self._state == DishState.ALERTED:
            if self.consensus.clear():
                self._dishes_since = None
                self._transition(DishState.CLEAR, "finally cleared")

        return {
            "state": self._state.value, "previous_state": old.value,
            "changed": self._state != old, "should_alert": should_alert,
            "consensus": self.consensus.snapshot(),
            "grace_remaining": str(self.grace_remaining) if self.grace_remaining else None,
            "dishes_since": self._dishes_since.isoformat() if self._dishes_since else None,
        }

    def _transition(self, new, reason):
        now = datetime.utcnow()
        log.info("STATE: %s -> %s (%s)", self._state.value, new.value, reason)

        self.db.execute(
            "INSERT INTO events (timestamp,from_state,to_state,reason) VALUES (?,?,?,?)",
            (now.isoformat(), self._state.value, new.value, reason))

        self._state = new
        self._entered = now
        self.db.execute(
            "UPDATE state SET current_state=?, entered_at=?, dishes_since=?, updated_at=? "
            "WHERE id=1",
            (new.value, now.isoformat(),
             self._dishes_since.isoformat() if self._dishes_since else None,
             now.isoformat()))
        self.db.commit()

    def log_alert(self, channel, success, message="", image_file=""):
        self.db.execute(
            "INSERT INTO alerts (timestamp,channel,success,message,image_file) "
            "VALUES (?,?,?,?,?)",
            (datetime.utcnow().isoformat(), channel, int(success), message, image_file))
        self.db.commit()

    # -- queries for the dashboard --

    def get_status(self):
        return {
            "state": self._state.value,
            "entered_at": self._entered.isoformat(),
            "time_in_state": str(datetime.utcnow() - self._entered),
            "dishes_since": self._dishes_since.isoformat() if self._dishes_since else None,
            "grace_remaining": str(self.grace_remaining) if self.grace_remaining else None,
            "grace_minutes": self.grace_minutes,
            "consensus": self.consensus.snapshot(),
        }

    def get_stats(self):
        """aggregate stats for the dashboard"""
        s = {}

        r = self.db.execute(
            "SELECT COUNT(*) AS total_frames, SUM(dishes_found) AS frames_with_dishes, "
            "ROUND(AVG(inference_ms),1) AS avg_inference_ms, "
            "ROUND(AVG(CASE WHEN dishes_found THEN confidence_avg END),3) AS avg_dish_confidence "
            "FROM detections").fetchone()
        s.update(dict(r))

        r = self.db.execute(
            "SELECT COUNT(*) AS today_frames, SUM(dishes_found) AS today_dishes "
            "FROM detections WHERE DATE(timestamp)=DATE('now')").fetchone()
        s.update(dict(r))

        r = self.db.execute(
            "SELECT COUNT(*) AS hour_frames, SUM(dishes_found) AS hour_dishes "
            "FROM detections WHERE timestamp>=DATETIME('now','-1 hour')").fetchone()
        s.update(dict(r))

        s["total_transitions"] = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        s["total_alerts"] = self.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]

        recent = self.db.execute(
            "SELECT dishes_found FROM detections ORDER BY id DESC LIMIT 20").fetchall()
        s["recent_dish_rate"] = round(
            sum(r[0] for r in recent) / len(recent), 3) if recent else 0.0

        # hourly breakdown for the chart
        hourly = self.db.execute(
            "SELECT STRFTIME('%H',timestamp) AS hour, COUNT(*) AS frames, "
            "SUM(dishes_found) AS dishes FROM detections "
            "WHERE timestamp>=DATETIME('now','-24 hours') "
            "GROUP BY STRFTIME('%H',timestamp) ORDER BY hour").fetchall()
        s["hourly"] = [dict(h) for h in hourly]

        return s

    def recent_detections(self, limit=50):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM detections ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def recent_events(self, limit=50):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def recent_alerts(self, limit=20):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def force_state(self, new_state, reason="manual override"):
        self._transition(DishState(new_state), reason)
        if new_state == DishState.CLEAR.value:
            self._dishes_since = None
            self.consensus.reset()
