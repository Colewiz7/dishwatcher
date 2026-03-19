# dishwatcher

iot system that watches my kitchen sink with a camera and nags me when dishes pile up. also records blame clips so i know who did it.

```
dishwatcher/
├── server/    <- debian server (detection + dashboard)
└── camera/    <- raspberry pi (motion + video capture)
```

## what it does

a usb webcam on a raspberry pi watches the sink. when someone walks up and leaves, the pi:

1. saves a **15 second blame clip** of who was there
2. waits 10 seconds for them to fully leave
3. takes a clean photo of the sink (no one blocking the view)
4. posts both to the server

the server compares the photo against a saved "clean sink" reference image using **SSIM** (structural similarity). if the sink looks different from clean, there are dishes. way more reliable than yolo alone because SSIM catches everything: pots, pans, cutting boards, random shit piled up. not just the 5 coco classes yolo knows.

if dishes sit there for 90 minutes, you get an alert. the blame clip tells you who put them there.

## architecture

```
┌─────────────────────┐                          ┌──────────────────────────┐
│  raspberry pi       │   POST frame + video     │  server                  │
│                     │ ────────────────────────► │                          │
│  1. motion detected │                          │  1. ssim vs reference    │
│  2. record 15s clip │                          │     "is sink different   │
│  3. wait 10s        │ ◄──────────────────────── │      from clean?"       │
│  4. capture + send  │   json: state, ssim,     │  2. yolo labels (opt)   │
│                     │   consensus              │  3. state machine       │
│  5. heartbeat mode  │                          │  4. dashboard + alerts  │
└─────────────────────┘                          └──────────────────────────┘
```

## detection: how it actually works

**old approach (v4):** yolo looks for cups/forks/bowls. misses pots, pans, cutting boards, and anything that isnt in its training data. also the sink roi was tiny.

**new approach (v5):** take a photo of your clean sink, save it as a reference. every new frame gets compared using SSIM on just the sink region. SSIM asks "do these two images look the same?" not "can i find a fork?" this catches everything.

yolo still runs optionally as a secondary pass to label what it sees (so the dashboard can say "2 bowls, 1 pot" instead of just "dirty"). but the primary dirty/clean decision is SSIM.

the reference frame gets histogram-equalized before comparison so lighting changes (daylight vs kitchen light) dont cause false positives.

### setup flow

1. deploy server, open dashboard
2. clean your sink
3. hit "set reference" on the dashboard (saves the clean photo)
4. the server auto-detects the sink bounding box with yolo
5. done, system is calibrated

if you move the camera, just set a new reference.

## blame clips

when someone walks up to the sink:
- the pi starts recording frames into a ring buffer (15 sec at 5fps)
- when they walk away, the pi waits 10 seconds (capture delay)
- then it encodes the buffer as an mp4 and sends it with the detection frame
- the server saves the clip and shows it in the dashboard

you can scrub through blame clips on the dashboard to see who left dishes.

## counter detection (optional)

toggle in the dashboard. defines a second ROI for the counter area next to the sink. uses the same SSIM approach. items on the counter (drying rack, etc) show up in the dashboard but dont trigger alerts.

## state machine

```
CLEAR ──(5/7 frames say dirty)──► CONFIRMED ──(90 min timer)──► ALERTED
  ▲                                      │                           │
  └──────(5/7 frames say clean)──────────┘───────────────────────────┘
```

same consensus engine as before. 5 out of 7 frames need to agree before changing state. sqlite-backed, full history.

## quick start

### server

```bash
git clone https://github.com/Colewiz7/dishwatcher
cd dishwatcher/server
# edit docker-compose.yml
docker compose up -d
# dashboard at http://your-server-ip:8000
```

### camera (raspberry pi)

```bash
git clone https://github.com/Colewiz7/dishwatcher
cd dishwatcher/camera
chmod +x setup.sh
./setup.sh         # handles python, venv, deps, asks for server hostname
venv/bin/python watcher.py
```

### first time calibration

1. open the dashboard in a browser
2. make sure the camera can see the sink
3. clean the sink
4. click "set reference" on the dashboard
5. system is ready

## repo structure

```
dishwatcher/
├── README.md
├── .gitignore
├── server/
│   ├── server.py              <- fastapi, routes, sse
│   ├── detector.py            <- ssim comparison + optional yolo
│   ├── state_machine.py       <- consensus engine + sqlite
│   ├── storage.py             <- image + video saves
│   ├── notifier.py            <- discord webhooks
│   ├── static/
│   │   ├── viewer.html
│   │   ├── style.css
│   │   └── app.js
│   ├── systemd/
│   │   └── dishwatcher-server.service
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── requirements.txt
└── camera/
    ├── watcher.py             <- motion + video buffer + delayed capture
    ├── setup.sh               <- interactive setup script
    ├── .env.example
    ├── systemd/
    │   └── dishwatcher-edge.service
    └── requirements.txt
```

## tech stack

| component | tech |
|-----------|------|
| primary detection | SSIM (structural similarity) |
| secondary labeling | yolov8n (optional) |
| edge motion | opencv mog2 |
| blame clips | opencv VideoWriter (mp4) |
| server | fastapi + uvicorn |
| real-time updates | server-sent events |
| state persistence | sqlite (wal mode) |
| dashboard | vanilla html/css/js + chart.js |
| notifications | discord, browser notifications, web audio |
| deployment | docker, systemd |

## future stuff

- [ ] dashboard rewrite (mobile-first, video gallery with playback, swipeable history)
- [ ] visual ROI editor on dashboard (drag to set sink/counter regions)
- [ ] fine-tune yolo on actual sink images
- [ ] ntfy.sh notifications
- [ ] auto image/video cleanup (delete old files after N days)
- [ ] multi-camera support
