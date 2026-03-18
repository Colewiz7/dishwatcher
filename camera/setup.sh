#!/usr/bin/env bash
set -e

echo "=== dishwatcher edge setup ==="
echo ""

# check if system has the packages we need
echo "[1/3] checking for system packages..."
HAVE_SYSTEM=true
python3 -c "import cv2" 2>/dev/null || HAVE_SYSTEM=false
python3 -c "import requests" 2>/dev/null || HAVE_SYSTEM=false
python3 -c "import numpy" 2>/dev/null || HAVE_SYSTEM=false

if [ "$HAVE_SYSTEM" = true ]; then
    echo "  found system opencv, numpy, requests"
    echo "[2/3] creating venv with system packages..."
    python3 -m venv --system-site-packages venv
else
    echo "  system packages not found, trying to install them..."
    sudo apt install -y python3-numpy python3-opencv python3-requests 2>/dev/null

    # check again
    HAVE_SYSTEM=true
    python3 -c "import cv2" 2>/dev/null || HAVE_SYSTEM=false
    python3 -c "import numpy" 2>/dev/null || HAVE_SYSTEM=false
    python3 -c "import requests" 2>/dev/null || HAVE_SYSTEM=false

    if [ "$HAVE_SYSTEM" = true ]; then
        echo "  installed system packages"
        echo "[2/3] creating venv with system packages..."
        python3 -m venv --system-site-packages venv
    else
        echo "  apt install failed, falling back to pip (this will be slow)..."
        echo "[2/3] creating venv + pip installing..."
        python3 -m venv venv
        venv/bin/pip install --upgrade pip numpy
        venv/bin/pip install -r requirements.txt
    fi
fi

# verify
echo ""
echo "  verifying imports..."
venv/bin/python -c "import cv2, requests, numpy; print('  all good: numpy ' + numpy.__version__ + ', opencv ' + cv2.__version__)"

echo ""
echo "[3/3] setting up .env..."
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "  created .env from .env.example"
    echo "  >>> edit .env and set DISH_SERVER_URL <<<"
elif [ -f ".env" ]; then
    echo "  .env already exists, skipping"
fi

echo ""
echo "=== done ==="
echo "run with: venv/bin/python watcher.py"
