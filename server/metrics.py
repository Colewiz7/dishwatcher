# metrics.py - Prometheus metrics, written for one purpose: make the v1 failure
# mode detectable from outside the process.
#
# v1 was broken for 15 days because the only evidence was a DEBUG line in a
# process running at INFO. Nothing scraped it, nothing alerted, and the score it
# reported (a constant 1.000) looked like a healthy clean sink.
#
# So the two metrics that matter most here are calibration_valid, and a
# histogram of the score itself, because "this number has not varied in 4250
# observations" is a detectable condition and should page somebody.
#
# No prometheus_client dependency: this is a few hundred numbers and a text
# format, and the Pi half has to stay light.

import threading
import time
from collections import defaultdict

_lock = threading.Lock()
_counters = defaultdict(float)
_gauges = {}
_hist_buckets = defaultdict(lambda: defaultdict(float))
_hist_sum = defaultdict(float)
_hist_count = defaultdict(float)
_observed_values = defaultdict(list)

SSIM_BUCKETS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99, 1.0]
LATENCY_BUCKETS = [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]

_HELP = {
    "dishwatcher_calibration_valid":
        ("gauge", "1 when a usable reference and sink ROI exist. 0 means detection is disabled."),
    "dishwatcher_camera_seconds_since_last_frame":
        ("gauge", "Seconds since the camera last produced a frame. Rises without bound when wedged."),
    "dishwatcher_camera_reopens_total":
        ("counter", "Times the capture device was reopened by the watchdog."),
    "dishwatcher_camera_usb_resets_total":
        ("counter", "Times the watchdog escalated to a USB re-authorise."),
    "dishwatcher_motion_transitions_total":
        ("counter", "Committed motion state transitions, by direction."),
    "dishwatcher_motion_flap_ratio":
        ("gauge", "exited/entered. v1 sat at 23.7; healthy is near 1.0."),
    "dishwatcher_ssim_score":
        ("histogram", "Distribution of the SSIM score. Zero variance means the detector is stuck."),
    "dishwatcher_ssim_distinct_recent":
        ("gauge", "Distinct SSIM values in the last 100 observations. 1 means constant, i.e. the v1 bug."),
    "dishwatcher_detections_total":
        ("counter", "Detections by outcome (dirty, clean, uncalibrated)."),
    "dishwatcher_detector_latency_seconds":
        ("histogram", "Detection wall time."),
    "dishwatcher_empty_labels_total":
        ("counter", "Detections where the labeller returned nothing."),
    "dishwatcher_uploads_total":
        ("counter", "Uploads received from the camera, by result."),
    "dishwatcher_state_transitions_total":
        ("counter", "Sink state machine transitions."),
    "dishwatcher_clips_deleted_total":
        ("counter", "Clips removed by retention."),
    "dishwatcher_oldest_clip_age_seconds":
        ("gauge", "Age of the oldest retained clip."),
    "dishwatcher_storage_bytes":
        ("gauge", "Bytes held under the data directory."),
    "dishwatcher_build_info":
        ("gauge", "Build metadata, always 1."),
}


def _key(name, labels):
    if not labels:
        return name, ()
    return name, tuple(sorted(labels.items()))


def inc(name, value=1.0, **labels):
    with _lock:
        _counters[_key(name, labels)] += value


def gauge(name, value, **labels):
    with _lock:
        _gauges[_key(name, labels)] = float(value)


def observe(name, value, buckets=None, **labels):
    b = buckets or (SSIM_BUCKETS if "ssim" in name else LATENCY_BUCKETS)
    k = _key(name, labels)
    with _lock:
        for edge in b:
            if value <= edge:
                _hist_buckets[k][edge] += 1
        _hist_buckets[k]["+Inf"] += 1
        _hist_sum[k] += value
        _hist_count[k] += 1
        vals = _observed_values[k]
        vals.append(round(float(value), 4))
        if len(vals) > 100:
            del vals[:-100]


def distinct_recent(name, **labels):
    """How many distinct values recently. 1 means the detector is stuck."""
    with _lock:
        return len(set(_observed_values.get(_key(name, labels), [])))


def _fmt_labels(pairs):
    if not pairs:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in pairs) + "}"


def render():
    """Prometheus text exposition format."""
    with _lock:
        counters = dict(_counters)
        gauges = dict(_gauges)
        hb = {k: dict(v) for k, v in _hist_buckets.items()}
        hs, hc = dict(_hist_sum), dict(_hist_count)
        observed = {k: list(v) for k, v in _observed_values.items()}

    # derived: the constant-score detector alarm
    for (name, pairs), vals in observed.items():
        if "ssim" in name and vals:
            gauges[("dishwatcher_ssim_distinct_recent", pairs)] = float(len(set(vals)))

    lines, emitted = [], set()

    def header(metric):
        if metric in emitted:
            return
        emitted.add(metric)
        kind, help_text = _HELP.get(metric, ("gauge", ""))
        if help_text:
            lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} {kind}")

    for (name, pairs), v in sorted(gauges.items()):
        header(name)
        lines.append(f"{name}{_fmt_labels(pairs)} {v}")

    for (name, pairs), v in sorted(counters.items()):
        header(name)
        lines.append(f"{name}{_fmt_labels(pairs)} {v}")

    for (name, pairs), buckets in sorted(hb.items()):
        header(name)
        cumulative = 0.0
        for edge in sorted((e for e in buckets if e != "+Inf")):
            cumulative = buckets[edge]
            le = f'le="{edge}"'
            inner = ",".join(list(f'{k}="{v}"' for k, v in pairs) + [le])
            lines.append(f"{name}_bucket{{{inner}}} {cumulative}")
        inner = ",".join(list(f'{k}="{v}"' for k, v in pairs) + ['le="+Inf"'])
        lines.append(f"{name}_bucket{{{inner}}} {buckets.get('+Inf', 0.0)}")
        lines.append(f"{name}_sum{_fmt_labels(pairs)} {hs.get((name, pairs), 0.0)}")
        lines.append(f"{name}_count{_fmt_labels(pairs)} {hc.get((name, pairs), 0.0)}")

    return "\n".join(lines) + "\n"
