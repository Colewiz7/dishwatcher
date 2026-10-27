"""Convert lightweight camera chunks to browser video on the server, not the Pi."""
import json
import math
import subprocess
import tempfile
from pathlib import Path

import cv2


def normalize_video(raw, rotation=None):
    if not raw or len(raw) > 32 * 1024 * 1024:
        raise ValueError("video must be between 1 byte and 32 MiB")
    filters = {cv2.ROTATE_180: "hflip,vflip", cv2.ROTATE_90_CLOCKWISE: "transpose=clock",
               cv2.ROTATE_90_COUNTERCLOCKWISE: "transpose=cclock"}
    transform = filters.get(rotation)
    scale = "scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2"
    with tempfile.TemporaryDirectory(prefix="dishwatcher-video-") as folder:
        source, output = Path(folder) / "source", Path(folder) / "clip.mp4"
        source.write_bytes(raw)
        result = subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
            "-map", "0:v:0", "-an", "-vf", f"{transform},{scale}" if transform else scale,
            "-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-crf", "26",
            "-pix_fmt", "yuv420p", "-t", "125", "-movflags", "+faststart", str(output),
        ], capture_output=True, timeout=120)
        if result.returncode or not output.is_file():
            raise ValueError("video could not be decoded")
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "json", str(output)], capture_output=True, check=True, timeout=10)
        duration = float(json.loads(probe.stdout)["format"]["duration"])
        if not math.isfinite(duration) or not 0 < duration <= 126:
            raise ValueError("invalid clip duration")
        return output.read_bytes(), round(duration, 2)
