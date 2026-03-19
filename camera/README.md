# dishwatcher camera

raspberry pi edge node. watches the sink, records blame clips, sends detection frames to the server after the person leaves.

see the [root README](../README.md) for the full system overview.

## setup

```bash
cd camera/
chmod +x setup.sh
./setup.sh
# asks for server hostname + api key, tests connection
```

run: `venv/bin/python watcher.py`

## how it works

1. idle, barely using any cpu
2. someone walks up to the sink, motion detected
3. starts recording frames into a 15 second ring buffer (blame clip)
4. person walks away, motion stops
5. waits 10 seconds for them to fully leave (so we get a clean shot)
6. encodes the ring buffer as an mp4
7. captures a clean photo of the sink
8. posts both to the server
9. enters monitoring mode, heartbeat every 30s
10. if server says clear 3x in a row, goes back to idle

## env vars

| var | default | what |
|-----|---------|------|
| `DISH_SERVER_URL` | `http://localhost:8000/upload` | server endpoint |
| `DISH_API_KEY` | (empty) | must match server |
| `CAMERA_INDEX` | 0 | usb webcam |
| `VIDEO_FPS` | 5 | blame clip framerate |
| `VIDEO_DURATION` | 15 | blame clip length (seconds) |
| `CAPTURE_DELAY_SEC` | 10 | wait after motion stops |
| `PROCESS_EVERY_N` | 3 | motion check every Nth frame |
| `IDLE_SLEEP_MS` | 50 | sleep when idle |
| `HEARTBEAT_INTERVAL_SEC` | 30 | monitoring heartbeat |

## tuning

**pi zero/2w:** `PROCESS_EVERY_N=5`, `IDLE_SLEEP_MS=100`, `VIDEO_FPS=3`

**pi 3b+:** defaults are fine

**pi 4/5:** can lower `PROCESS_EVERY_N` to 2 for faster response
