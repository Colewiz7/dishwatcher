#!/usr/bin/env bash
set -e

echo "=== dishwatcher edge setup ==="
echo ""

# -- 1. deps --
echo "[1/3] checking for system packages..."
HAVE_SYSTEM=true
python3 -c "import cv2" 2>/dev/null || HAVE_SYSTEM=false
python3 -c "import requests" 2>/dev/null || HAVE_SYSTEM=false
python3 -c "import numpy" 2>/dev/null || HAVE_SYSTEM=false

if [ "$HAVE_SYSTEM" = true ]; then
    echo "  found system opencv, numpy, requests"
    python3 -m venv --system-site-packages venv
else
    echo "  not found, trying apt install..."
    sudo apt install -y python3-numpy python3-opencv python3-requests 2>/dev/null

    HAVE_SYSTEM=true
    python3 -c "import cv2" 2>/dev/null || HAVE_SYSTEM=false
    python3 -c "import numpy" 2>/dev/null || HAVE_SYSTEM=false
    python3 -c "import requests" 2>/dev/null || HAVE_SYSTEM=false

    if [ "$HAVE_SYSTEM" = true ]; then
        python3 -m venv --system-site-packages venv
    else
        echo "  apt failed, using pip (slow)..."
        python3 -m venv venv
        venv/bin/pip install --upgrade pip numpy
        venv/bin/pip install -r requirements.txt
    fi
fi

venv/bin/python -c "import cv2, requests, numpy; print('  deps ok')"
echo ""

# -- 2. config --
echo "[2/3] config..."

# load existing values
CURRENT_HOST=""
CURRENT_KEY=""
if [ -f ".env" ]; then
    # extract just the hostname from the url
    CURRENT_HOST=$(grep -oP 'DISH_SERVER_URL=http://\K[^:/]+' .env 2>/dev/null || true)
    CURRENT_KEY=$(grep -oP 'DISH_API_KEY=\K.*' .env 2>/dev/null || true)
fi

# server hostname
echo ""
if [ -n "$CURRENT_HOST" ] && [ "$CURRENT_HOST" != "YOUR_SERVER_IP" ] && [ "$CURRENT_HOST" != "localhost" ]; then
    echo "  current server: $CURRENT_HOST"
    read -p "  server hostname (enter to keep): " INPUT_HOST
    [ -z "$INPUT_HOST" ] && INPUT_HOST="$CURRENT_HOST"
else
    read -p "  server hostname (e.g. 192.168.1.50 or myserver.local): " INPUT_HOST
    while [ -z "$INPUT_HOST" ]; do
        read -p "  cant be empty: " INPUT_HOST
    done
fi

# strip http:// if they pasted a full url
INPUT_HOST=$(echo "$INPUT_HOST" | sed 's|^https\?://||' | sed 's|/.*||' | sed 's|:.*||')

# api key
echo ""
if [ -n "$CURRENT_KEY" ] && [ "$CURRENT_KEY" != "changeme" ]; then
    echo "  current api key: $CURRENT_KEY"
    read -p "  api key (enter to keep): " INPUT_KEY
    [ -z "$INPUT_KEY" ] && INPUT_KEY="$CURRENT_KEY"
else
    read -p "  api key (enter for none): " INPUT_KEY
fi

# write .env
[ -f ".env.example" ] && [ ! -f ".env" ] && cp .env.example .env
[ ! -f ".env" ] && touch .env

_set() {
    if grep -q "^${1}=" .env 2>/dev/null; then
        sed -i "s|^${1}=.*|${1}=${2}|" .env
    else
        echo "${1}=${2}" >> .env
    fi
}

_set "DISH_SERVER_URL" "http://${INPUT_HOST}:8000/upload"
_set "DISH_API_KEY" "$INPUT_KEY"

echo ""
echo "  saved to .env"
echo ""

# -- 3. test --
echo "[3/3] testing connection to $INPUT_HOST ..."

RESULT=$(venv/bin/python -c "
import requests, sys
try:
    r = requests.get('http://${INPUT_HOST}:8000/healthz', timeout=5)
    if r.status_code == 200:
        d = r.json()
        print('  connected! state: ' + d.get('state', '?') + ', model: ' + d.get('model', '?'))
        sys.exit(0)
    else:
        print('  server returned http ' + str(r.status_code))
        sys.exit(1)
except requests.ConnectionError:
    print('  cant reach ' + '${INPUT_HOST}:8000')
    sys.exit(1)
except Exception as e:
    print('  error: ' + str(e))
    sys.exit(1)
" 2>&1) || true

echo "$RESULT"
echo ""

if echo "$RESULT" | grep -q "connected!"; then
    echo "=== ready to go ==="
else
    echo "=== setup done but couldnt reach server ==="
    echo "make sure the server is running, then try: venv/bin/python watcher.py"
fi

echo ""
echo "run with: venv/bin/python watcher.py"
