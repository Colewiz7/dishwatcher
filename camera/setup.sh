#!/usr/bin/env bash
set -e

echo "=== dishwatcher edge setup ==="

python3 -m venv venv
venv/bin/pip install --upgrade pip numpy -q
venv/bin/pip install -r requirements.txt -q

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "created .env from .env.example"
    echo ">>> edit .env and set DISH_SERVER_URL <<<"
fi

echo ""
echo "done. run with: venv/bin/python watcher.py"
