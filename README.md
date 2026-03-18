# dishwatcher

iot system that watches my kitchen sink with a camera and yells at me when dishes have been sitting there too long.

```
dishwatcher/
├── server/    <- runs on a debian server (yolov8 + dashboard)
└── camera/    <- runs on a raspberry pi (motion detection + frame posting)
```

![dashboard](https://img.shields.io/badge/dashboard-live-22c55e) ![yolov8](https://img.shields.io/badge/yolo-v8n-blue) ![python](https://img.shields.io/badge/python-3.11-yellow)

## what it does

a usb webcam pointed at the sink feeds frames to a raspberry pi. the pi runs background subtraction to detect motion, then posts the frame to a server over http. the server runs yolov8 to figure out if there are actually dishes in the sink (cups, bowls, forks, spoons, knives). if dishes sit there for 90 minutes, i get an alert.

the key insight is that single yolo frames are unreliable. one shadow, one weird angle, and you get a false positive. so the server uses a **temporal consensus engine**: it keeps a ring buffer of the last 7 detection results and only changes state when 5 of them agree. this kills false positives and false negatives.

## architecture

```
┌─────────────────┐         HTTP POST /upload         ┌──────────────────────────┐
│  raspberry pi   │ ─────────────────────────────────► │  server                  │
│                 │     jpeg frame + mode header       │                          │
│  watcher.py     │                                    │  server.py  (fastapi)    │
│  - usb webcam   │                                    │  detector.py (yolov8)    │
│  - mog2 motion  │ ◄──────────────────────────────── │  state_machine.py        │
│  - heartbeat    │     json: state, consensus,        │  storage.py              │
│    mode         │     detections, grace timer         │  notifier.py (discord)   │
└─────────────────┘                                    │                          │
                                                       │  GET / ──► viewer.html   │
                                                       │  GET /stream ──► SSE     │
                                                       └──────────────────────────┘
                                                                │
                                                       browser connects to :8000
                                                       real-time dashboard via sse
```

designed for networks with client isolation (like university wifi) where the server cant reach the pi. all communication is pi-initiated http.

## quick start

### server (your linux box / homelab)

```bash
git clone https://github.com/Colewiz7/dishwatcher
cd dishwatcher/server
# edit docker-compose.yml (at minimum set DISH_API_KEY)
docker compose up -d
# dashboard at http://your-server-ip:8000
```

or bare metal: `pip install -r requirements.txt && uvicorn server:app --host 0.0.0.0 --port 8000`

### camera (raspberry pi)

```bash
git clone https://github.com/Colewiz7/dishwatcher
cd dishwatcher/camera
chmod +x setup.sh
./setup.sh         # auto-detects python, makes venv, installs deps
nano .env          # set DISH_SERVER_URL to your server
venv/bin/python watcher.py
```

both sides include systemd service files for auto-start on boot. see the READMEs in each folder for full details.

## how detection works

**dual-pass yolo inference:**

1. **pass 1**: run yolov8n on the full 640x480 frame. this finds the sink (coco class 71), bowls, cups, and other larger objects. on first detection, the sink bounding box gets cached to disk so we dont need to re-find it every frame.

2. **pass 2**: crop just the sink region, upscale it to 640px wide, and run yolo again. forks, spoons, and knives are often only ~20px in a full frame, which is below yolo's effective detection threshold. the upscaled crop makes them 3-4x larger and way easier to catch.

detections from both passes get merged, deduplicated by iou, and filtered so only objects whose center point is inside the cached sink bounding box count. a bowl on the counter doesnt trigger anything.

## state machine

```
CLEAR ──(5/7 frames say dishes)──► CONFIRMED ──(90 min timer)──► ALERTED
  ▲                                      │                           │
  └──────(5/7 frames say clear)──────────┘───────────────────────────┘
```

- **CLEAR**: sink is empty, system is idle
- **CONFIRMED**: consensus agrees dishes are present, grace timer starts
- **ALERTED**: timer expired, notification fired. stays here until dishes are cleared

every state transition requires 5 out of 7 frames to agree. one yolo hallucination doesnt do shit.

all state, detections, and transitions get logged to sqlite so the dashboard can show history and stats.

## the dashboard

real-time web ui served at the server root. no separate frontend deploy, its just static files served by fastapi.

- **sse connection** (server-sent events, not polling) so updates are instant
- **live camera feed** that updates on every frame the pi sends
- **consensus buffer visualization** showing the ring buffer state
- **grace timer countdown** that ticks every second
- **24hr detection timeline chart** (chart.js)
- **detection log + state event log** that update in real time
- **browser notifications** (with image attachment) and **audio alerts**
- **admin controls**: reset sink cache, force state, test notifications
- **stats panel**: frames today, dish rate, avg inference, total alerts

dark theme, monospace data, teal accents. looks like a monitoring dashboard, not a webapp.

## heartbeat mode

the biggest problem with earlier versions was that once someone places dishes and walks away, motion stops, and the pi stops sending frames. the dishes become invisible to the system.

fix: after any motion is detected, the pi enters "monitoring mode" and sends a heartbeat frame every 30 seconds even without motion. it exits monitoring when the server confirms the sink has been clear for 3 consecutive heartbeats. if the server state is CONFIRMED or ALERTED, monitoring stays active indefinitely.

## pi optimizations

the pi is slow. the old code pegged it at 100% cpu doing basically nothing. current version:

- motion detection runs on **grayscale** (3x less data through mog2)
- motion frame is **downscaled to 320x240** (75% fewer pixels to process)
- only process **every 3rd frame** for motion (configurable)
- **sleep when idle** instead of busy-looping on cap.read()
- **connection pooling** (reuse tcp session instead of handshake per post)

idle cpu: ~2-5% (was ~100%).

## tech stack

| component | tech |
|-----------|------|
| edge motion detection | opencv mog2 (background subtraction) |
| object detection | yolov8n (coco pretrained) |
| server framework | fastapi + uvicorn |
| real-time updates | server-sent events |
| state persistence | sqlite (wal mode) |
| image annotation | pillow |
| dashboard | vanilla html/css/js + chart.js |
| notifications | discord webhooks, browser notifications, web audio |
| deployment | docker, systemd |

## repo structure

```
dishwatcher/
├── README.md                  <- you are here
├── .gitignore
│
├── server/
│   ├── server.py              <- fastapi app, routes, sse
│   ├── detector.py            <- yolov8 dual-pass inference
│   ├── state_machine.py       <- consensus engine + sqlite
│   ├── storage.py             <- threaded image saves
│   ├── notifier.py            <- discord webhooks (optional)
│   ├── static/
│   │   ├── viewer.html        <- dashboard markup
│   │   ├── style.css          <- dashboard styles
│   │   └── app.js             <- dashboard logic + sse client
│   ├── systemd/
│   │   └── dishwatcher-server.service
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   └── README.md
│
└── camera/
    ├── watcher.py             <- motion detection + heartbeat
    ├── setup.sh               <- auto python/venv/deps setup
    ├── .env.example
    ├── systemd/
    │   └── dishwatcher-edge.service
    ├── requirements.txt
    └── README.md
```

## future stuff

- [ ] fine-tune yolov8n on actual sink images (even 50 labeled frames would help)
- [ ] ntfy.sh as a notification channel (lighter than discord)
- [ ] "snooze" button on the dashboard to delay alerts
- [ ] image retention policy (auto-delete frames older than N days)
- [ ] multi-camera support
- [ ] grafana integration for long-term metrics
