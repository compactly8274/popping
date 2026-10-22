#!/usr/bin/env bash
# Re-run the Popping iPhone overflow audit end to end.
#   bash frontend/scripts/iphone-audit/run.sh
# Outputs land next to this script: <width>-<nn>-<state>.png (+ -tall variants),
# <width>-overflow-scan.json, summary.json, request-log.txt, analysis.txt, probe-output.json.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
FRONTEND="$(cd "$HERE/../.." && pwd)"
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers   # chromium-1194 is pre-installed here; do NOT run `playwright install`

cd "$FRONTEND"
npm run build
# Start vite itself (not through npx) so $! is the server and the trap kills it.
./node_modules/.bin/vite preview --host 127.0.0.1 --port 4173 --strictPort > "$HERE/vite-preview.log" 2>&1 &
PREVIEW_PID=$!
trap 'kill $PREVIEW_PID 2>/dev/null || true' EXIT
for i in $(seq 1 20); do
  curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4173/ | grep -q 200 && break
  sleep 0.5
done

cd "$HERE"
node fixtures.cjs            # prints the exact fixture lengths (60-char name, 120-char URL, 180-char title, 90-char token)
node capture.cjs             # screenshots + per-state DOM overflow scan at 390x844@3x and 320x568@2x
node analyze.cjs > analysis.txt
node probe.cjs > probe-output.json
echo "done: $(ls "$HERE"/*.png | wc -l) screenshots in $HERE"
