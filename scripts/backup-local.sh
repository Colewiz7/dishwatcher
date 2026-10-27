#!/usr/bin/env bash
set -euo pipefail
repo=$(git rev-parse --show-toplevel)
projects=${DISHWATCHER_PROJECTS:-/home/cole/Projects}
stamp=$(date +%Y-%m-%d_%H%M%S)
destination="$projects/dishwatcher-backups/$stamp"
mkdir -p "$destination" "$projects/dishwatcher"
git -C "$repo" bundle create "$destination/source.bundle" --all
# Include uncommitted source too, but never credentials, footage, or runtimes.
tar --exclude=.git --exclude=.env --exclude=venv --exclude=.venv-test \
    --exclude=__pycache__ --exclude=.pytest_cache --exclude=data \
    --exclude=images --exclude=videos --exclude=thumbs --exclude=people \
    --exclude=people.json --exclude=clip_tags.json --exclude=config.json \
    --exclude=reference.jpg --exclude=roi.json \
    -czf "$destination/source.tar.gz" -C "$repo" .
if [ "$repo" != "$projects/dishwatcher" ]; then
    tar -xzf "$destination/source.tar.gz" -C "$projects/dishwatcher"
fi
printf 'Backup saved: %s\n' "$destination"
