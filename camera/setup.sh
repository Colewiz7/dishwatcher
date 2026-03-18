#!/usr/bin/env bash
# setup.sh - sets up dishwatcher edge on raspberry pi
# handles python version detection, venv creation, and deps
set -e

echo "=== dishwatcher edge setup ==="

# find a working python >= 3.9
PYTHON=""
for bin in python3.11 python3.12 python3.10 python3.9 python3; do
    if command -v "$bin" &>/dev/null; then
        ver=$("$bin" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ] && [ "$minor" -le 12 ]; then
            PYTHON="$bin"
            echo "found $bin ($ver)"
            break
        else
            echo "skipping $bin ($ver) - need 3.9-3.12"
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo ""
    echo "no compatible python found (need 3.9-3.12)"
    echo "python 3.13 has numpy issues with opencv, avoid it for now"
    echo ""
    echo "on raspberry pi os:"
    echo "  sudo apt install python3.11 python3.11-venv"
    exit 1
fi

# create venv
if [ ! -d "venv" ]; then
    echo "creating venv with $PYTHON..."
    "$PYTHON" -m venv venv
else
    echo "venv already exists"
fi

# install deps
echo "installing dependencies..."
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q
echo "deps installed"

# setup .env if it doesnt exist
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo ""
        echo "created .env from .env.example"
        echo ">>> edit .env and set DISH_SERVER_URL to your server address <<<"
        echo ""
    fi
else
    echo ".env already exists"
fi

echo ""
echo "=== setup complete ==="
echo ""
echo "to run:"
echo "  source venv/bin/activate"
echo "  python watcher.py"
echo ""
echo "or without activating:"
echo "  venv/bin/python watcher.py"
echo ""
echo "for auto-start on boot:"
echo "  sudo cp systemd/dishwatcher-edge.service /etc/systemd/system/"
echo "  # edit the service file paths first!"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now dishwatcher-edge"
