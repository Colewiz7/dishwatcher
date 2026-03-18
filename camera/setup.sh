#!/usr/bin/env bash
set -e

echo "=== dishwatcher edge setup ==="
echo ""

echo "[1/4] creating venv..."
python3 -m venv venv

echo "[2/4] upgrading pip + numpy (this takes a sec)..."
venv/bin/pip install --upgrade pip numpy

echo ""
echo "[3/4] installing opencv + requests..."
venv/bin/pip install -r requirements.txt

echo ""
echo "[4/4] setting up .env..."
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "created .env from .env.example"
    echo ">>> edit .env and set DISH_SERVER_URL <<<"
elif [ -f ".env" ]; then
    echo ".env already exists, skipping"
else
    echo "no .env.example found, youll need to set env vars manually"
fi

echo ""
echo "=== done ==="
echo "run with: venv/bin/python watcher.py"
