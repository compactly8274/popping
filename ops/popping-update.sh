#!/bin/sh
# Update Popping to the latest build and clear out old images.
#
# Each update pulls new backend/frontend images (1-2 GB) and the old
# ones stay on disk until removed, which fills a small VPS over a few
# months. This pulls, restarts, then deletes images nothing uses.
#
# Install (as root):
#   cp ops/popping-update.sh /usr/local/bin/popping-update && chmod 700 /usr/local/bin/popping-update
# Run:
#   popping-update
set -eu

cd /opt/popping

echo "==> Updating repo files"
git pull --ff-only

echo "==> Downloading new images"
docker compose pull

echo "==> Restarting with the new images"
docker compose up -d

echo "==> Removing unused images"
docker image prune -f

echo "==> Waiting for the backend to report healthy"
i=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q backend)" 2>/dev/null)" = "healthy" ]; do
    i=$((i + 1))
    if [ "$i" -gt 36 ]; then
        echo "Backend not healthy after 3 minutes. Check: docker compose logs --tail 50 backend"
        exit 1
    fi
    sleep 5
done

echo "==> Done"
docker compose ps
df -h / | tail -1 | awk '{print "Disk: " $3 " used of " $2 " (" $5 ")"}'
