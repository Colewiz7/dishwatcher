# dishwatcher-edge

lightweight motion detector that runs on a raspberry pi. watches the sink with a usb webcam, and when something moves it posts the frame to the server for yolo inference. also has a heartbeat mode so it keeps checking even after motion stops (catches dishes that are just sitting there).

## setup

```bash
cd camera/
chmod +x setup.sh
./setup.sh
```

the setup script will:
- find a compatible python version (3.9-3.12, avoids 3.13 numpy issues)
- create a venv and install deps
- copy `.env.example` to `.env` for you to edit

then edit `.env` and set `DISH_SERVER_URL` to your server address:

```bash
nano .env
```

run it:

```bash
source venv/bin/activate
python watcher.py
```

or without activating the venv:

```bash
venv/bin/python watcher.py
```

### auto-start with systemd

```bash
# edit the service file first - replace YOUR_USERNAME with your actual username
nano systemd/dishwatcher-edge.service

sudo cp systemd/dishwatcher-edge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dishwatcher-edge

# check logs
journalctl -u dishwatcher-edge -f
```

## how it works

1. grabs frames from usb webcam
2. converts to grayscale + downscales to 320x240 (pi cant handle full res for motion)
3. runs MOG2 background subtraction to find motion
4. if motion exceeds threshold, post the full-res frame to the server
5. enter monitoring mode: send a heartbeat frame every 30s even without motion
6. if server says clear 3x in a row, exit monitoring
7. if nothing is happening, sleep (cpu goes from ~100% to ~2%)

## env vars

| var | default | notes |
|-----|---------|-------|
| `DISH_SERVER_URL` | `http://localhost:8000/upload` | **set this to your server** |
| `DISH_API_KEY` | (empty) | shared secret, must match server |
| `CAMERA_INDEX` | 0 | usb webcam index |
| `FRAME_WIDTH` | 640 | capture res (what gets sent to server) |
| `FRAME_HEIGHT` | 480 | |
| `MOTION_WIDTH` | 320 | motion detection res (lower = less cpu) |
| `MOTION_HEIGHT` | 240 | |
| `MIN_CONTOUR_AREA` | 500 | noise filter in motion-frame pixels |
| `MOTION_PERCENT` | 0.5 | % of frame that needs to move |
| `PROCESS_EVERY_N` | 3 | only run motion detection every Nth frame |
| `IDLE_SLEEP_MS` | 50 | sleep when idle (crucial for not pegging cpu) |
| `MOTION_COOLDOWN_SEC` | 10 | min seconds between motion posts |
| `JPEG_QUALITY` | 60 | compression quality |
| `HEARTBEAT_INTERVAL_SEC` | 30 | seconds between heartbeat posts |
| `MONITORING_DURATION_SEC` | 7200 | max monitoring time (2hr default) |
| `CLEAR_EXIT_COUNT` | 3 | consecutive clears to exit monitoring |

## tuning per pi model

**pi zero / zero 2w:** slow as shit. set `PROCESS_EVERY_N=5` and `IDLE_SLEEP_MS=100`.

**pi 3b+:** defaults should be fine. if cpu is above 30% idle, bump `PROCESS_EVERY_N` to 4.

**pi 4/5:** these have headroom. can drop `PROCESS_EVERY_N` to 2 or even 1 for faster motion response.

## why is this so optimized

the old version had no sleep in the main loop and ran motion detection on every single full-color frame. it pegged the pi at 100% cpu doing basically nothing. now it:

- runs motion on grayscale (3x less data)
- downscales to 320x240 for motion (75% fewer pixels)
- skips 2/3 of frames
- sleeps when idle
- reuses tcp connections
- doesnt call contourArea twice per contour (lol)

idle cpu went from ~100% to ~2-5%.

## python 3.13 note

python 3.13 has known compatibility issues with numpy + opencv. the setup script avoids it and picks 3.9-3.12 automatically. if you only have 3.13 on your system:

```bash
sudo apt install python3.11 python3.11-venv
rm -rf venv
./setup.sh
```
