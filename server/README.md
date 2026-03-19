# dishwatcher server

detection engine + dashboard. receives frames and blame clips from the pi, runs SSIM comparison against a clean reference to detect dishes, optionally labels with yolo, tracks state, and serves a real-time dashboard.

see the [root README](../README.md) for the full system overview.

## setup

```bash
cd server/
# edit docker-compose.yml (set DISH_API_KEY at minimum)
docker compose up -d
# dashboard at http://your-server-ip:8000
```

## first time: set reference

1. open dashboard, make sure pi is sending frames
2. clean the sink
3. click "set reference" (saves the clean photo + auto-detects sink ROI)

## api

| method | path | what |
|--------|------|------|
| GET | `/` | dashboard |
| GET | `/healthz` | health check |
| POST | `/upload` | pi sends frames + videos here |
| GET | `/stream` | sse event stream |
| GET | `/status` | state + consensus |
| GET | `/status/stats` | aggregate stats |
| POST | `/admin/set-reference` | save clean reference image |
| GET | `/admin/reference.jpg` | get current reference |
| POST | `/admin/set-roi` | set sink/counter ROI |
| GET | `/admin/roi` | get current ROI |
| POST | `/admin/auto-detect-sink` | yolo sink detection |
| POST | `/admin/force-state` | manual override |
| GET | `/view/list` | recent images |
| GET | `/view/videos` | blame clips |
| GET | `/view/video/{file}` | serve a video |

## env vars

| var | default | what |
|-----|---------|------|
| `DISH_API_KEY` | none | shared secret with pi |
| `SAVE_DIR` | ~/dishwasher | base dir for images/videos/db |
| `CAMERA_ROTATION` | 180 | CCW/CW/180/NONE |
| `SSIM_THRESHOLD` | 0.82 | below this = dishes detected |
| `YOLO_ENABLED` | true | run yolo for labels |
| `COUNTER_ENABLED` | false | enable counter detection |
| `CONSENSUS_WINDOW` | 7 | ring buffer size |
| `CONSENSUS_THRESHOLD` | 5 | frames to agree |
| `GRACE_MINUTES` | 90 | timer before alert |
