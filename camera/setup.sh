#!/usr/bin/env bash
set -e

echo "=== dishwatcher edge setup ==="
echo ""

# -- 1. deps --
echo "[1/4] checking for system packages..."
HAVE_SYSTEM=true
python3 -c "import cv2" 2>/dev/null || HAVE_SYSTEM=false
python3 -c "import requests" 2>/dev/null || HAVE_SYSTEM=false
python3 -c "import numpy" 2>/dev/null || HAVE_SYSTEM=false

if [ "$HAVE_SYSTEM" = true ]; then
    echo "  found system opencv, numpy, requests"
    echo "  creating venv with system packages..."
    python3 -m venv --system-site-packages venv
else
    echo "  system packages not found, trying apt install..."
    sudo apt install -y python3-numpy python3-opencv python3-requests 2>/dev/null

    HAVE_SYSTEM=true
    python3 -c "import cv2" 2>/dev/null || HAVE_SYSTEM=false
    python3 -c "import numpy" 2>/dev/null || HAVE_SYSTEM=false
    python3 -c "import requests" 2>/dev/null || HAVE_SYSTEM=false

    if [ "$HAVE_SYSTEM" = true ]; then
        echo "  installed system packages"
        echo "  creating venv with system packages..."
        python3 -m venv --system-site-packages venv
    else
        echo "  apt failed, falling back to pip (this will be slow)..."
        python3 -m venv venv
        venv/bin/pip install --upgrade pip numpy
        venv/bin/pip install -r requirements.txt
    fi
fi

echo "  verifying..."
venv/bin/python -c "import cv2, requests, numpy; print('  ok: numpy ' + numpy.__version__ + ', opencv ' + cv2.__version__)"
echo ""

# -- 2. config --
echo "[2/4] configuring..."

# load existing .env if there is one
CURRENT_URL=""
CURRENT_KEY=""
CURRENT_CAM="0"
if [ -f ".env" ]; then
    CURRENT_URL=$(grep -oP 'DISH_SERVER_URL=\K.*' .env 2>/dev/null || true)
    CURRENT_KEY=$(grep -oP 'DISH_API_KEY=\K.*' .env 2>/dev/null || true)
    CURRENT_CAM=$(grep -oP 'CAMERA_INDEX=\K.*' .env 2>/dev/null || true)
fi

# server url
echo ""
if [ -n "$CURRENT_URL" ] && [ "$CURRENT_URL" != "http://YOUR_SERVER_IP:8000/upload" ]; then
    echo "  current server url: $CURRENT_URL"
    read -p "  new url (enter to keep): " INPUT_URL
    [ -z "$INPUT_URL" ] && INPUT_URL="$CURRENT_URL"
else
    read -p "  server url (e.g. http://192.168.1.50:8000/upload): " INPUT_URL
    while [ -z "$INPUT_URL" ]; do
        read -p "  cant be empty, server url: " INPUT_URL
    done
fi

# api key
echo ""
if [ -n "$CURRENT_KEY" ] && [ "$CURRENT_KEY" != "changeme" ]; then
    echo "  current api key: $CURRENT_KEY"
    read -p "  new key (enter to keep): " INPUT_KEY
    [ -z "$INPUT_KEY" ] && INPUT_KEY="$CURRENT_KEY"
else
    read -p "  api key (must match server, enter for none): " INPUT_KEY
fi

# camera index
echo ""
if [ -n "$CURRENT_CAM" ]; then
    echo "  current camera index: $CURRENT_CAM"
    read -p "  camera index (enter to keep): " INPUT_CAM
    [ -z "$INPUT_CAM" ] && INPUT_CAM="$CURRENT_CAM"
else
    read -p "  camera index (enter for 0): " INPUT_CAM
    [ -z "$INPUT_CAM" ] && INPUT_CAM="0"
fi

# -- 3. write .env --
echo ""
echo "[3/4] writing .env..."

# start from example if it exists, otherwise create fresh
if [ -f ".env.example" ]; then
    cp .env.example .env
fi

# write values (create or replace)
_set_env() {
    local key="$1" val="$2"
    if grep -q "^${key}=" .env 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" .env
    else
        echo "${key}=${val}" >> .env
    fi
}

_set_env "DISH_SERVER_URL" "$INPUT_URL"
_set_env "DISH_API_KEY" "$INPUT_KEY"
_set_env "CAMERA_INDEX" "$INPUT_CAM"

echo "  saved to .env"
echo ""

# -- 4. test connection --
echo "[4/4] testing connection to server..."

# strip /upload to get base url, hit /healthz
BASE_URL=$(echo "$INPUT_URL" | sed 's|/upload$||')
HEALTH_URL="${BASE_URL}/healthz"

echo "  hitting $HEALTH_URL ..."

RESULT=$(venv/bin/python -c "
import requests, sys
try:
    r = requests.get('${HEALTH_URL}', timeout=5)
    if r.status_code == 200:
        d = r.json()
        print('  connected! server state: ' + d.get('state', '?') + ', model: ' + d.get('model', '?'))
        sys.exit(0)
    else:
        print('  server returned http ' + str(r.status_code))
        sys.exit(1)
except requests.ConnectionError:
    print('  could not connect to ' + '${HEALTH_URL}')
    print('  is the server running?')
    sys.exit(1)
except Exception as e:
    print('  error: ' + str(e))
    sys.exit(1)
" 2>&1) || true

echo "$RESULT"
echo ""

if echo "$RESULT" | grep -q "connected!"; then
    echo "=== all good, ready to go ==="
else
    echo "=== setup done but server connection failed ==="
    echo "check that the server is running and the url is right"
    echo "you can edit .env and re-run ./setup.sh anytime"
fi

echo ""
echo "run with: venv/bin/python watcher.py"
