# dishwatcher-server

the brains of the operation. takes frames from the pi, runs yolov8 to detect dishes in the sink, tracks state with a consensus engine so one bad frame doesnt ruin everything, and serves a real-time dashboard.

## setup

### docker (easiest)

```bash
cd server/
# edit docker-compose.yml - at minimum set DISH_API_KEY
docker compose up -d
```

dashboard lives at `http://YOUR_SERVER_IP:8000`

### bare metal

```bash
pip install -r requirements.txt
export DISH_API_KEY="changeme"
uvicorn server:app --host 0.0.0.0 --port 8000
```

yolo weights download automatically on first run.

### systemd

```bash
# edit the service file first - replace YOUR_USERNAME and paths
nano systemd/dishwatcher-server.service

sudo cp systemd/dishwatcher-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dishwatcher-server
```

## how it works

```
pi posts a frame to /upload
  -> server.py decodes + rotates it
  -> detector.py runs yolo (two passes)
     pass 1: full frame (finds sink, bowls, cups)
     pass 2: cropped sink region upscaled to 640px (catches forks/spoons/knives)
     merge + dedup + filter to only stuff inside the sink
  -> state_machine.py updates consensus buffer (need 5/7 frames to agree)
     CLEAR -> CONFIRMED -> (90 min grace timer) -> ALERTED
  -> storage.py saves annotated frame (threaded, non-blocking)
  -> sse broadcast pushes to dashboard in real time
  -> notifier.py fires discord webhook if alert triggered
```

## api

| method | path | what |
|--------|------|------|
| GET | `/` | dashboard |
| GET | `/healthz` | health check |
| POST | `/upload` | pi sends frames here |
| GET | `/stream` | sse event stream (dashboard connects to this) |
| GET | `/status` | current state + consensus |
| GET | `/status/stats` | aggregate stats |
| GET | `/status/history` | detection log |
| GET | `/status/events` | state transitions |
| POST | `/admin/reset-sink` | wipe sink bbox cache (if you move the camera) |
| POST | `/admin/force-state?state=CLEAR` | manual override |
| POST | `/admin/test-notify` | test discord webhook |
| GET | `/view/latest.jpg` | most recent annotated frame |
| GET | `/view/list` | recent images json |
| GET | `/view/image/{file}` | serve a saved image |

## env vars

| var | default | notes |
|-----|---------|-------|
| `DISH_API_KEY` | none | shared secret with pi. unset = no auth |
| `YOLO_MODEL_PATH` | yolov8n.pt | path to weights |
| `CONFIDENCE_THRESHOLD` | 0.40 | min detection confidence |
| `IMAGE_SAVE_DIR` | ~/dishwasher/images | where annotated frames go |
| `CAMERA_ROTATION` | 180 | CCW / CW / 180 / NONE |
| `CONSENSUS_WINDOW` | 7 | ring buffer size |
| `CONSENSUS_THRESHOLD` | 5 | how many frames need to agree |
| `GRACE_MINUTES` | 90 | timer before alert fires |
| `DB_PATH` | ~/dishwasher/dishwatcher.db | sqlite |
| `DUAL_PASS_ENABLED` | true | second pass on cropped sink region |
| `DISCORD_WEBHOOK_URL` | none | set to enable discord alerts |
| `DISCORD_MENTION` | none | user/role to ping |
| `NOTIFY_COOLDOWN_MIN` | 30 | min between repeated alerts |

## tuning

**too many false positives?** bump `CONSENSUS_THRESHOLD` to 6, or raise `CONFIDENCE_THRESHOLD` to 0.50.

**missing small stuff?** make sure `DUAL_PASS_ENABLED=true` and maybe lower confidence to 0.30 (consensus will filter the noise).

**want faster alerts?** lower `GRACE_MINUTES`. 90 is "hey go wash your dishes", 30 is "i am not fucking around".
