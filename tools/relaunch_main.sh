#!/usr/bin/env bash
# relaunch_main.sh — restart ONLY the main OBS instance (:4455), e.g. to load a rebuilt
# stereo-splat plugin. The three relays keep running and keep their streams connected;
# their picture freezes for the ~20 s main is down, then follows the virtual camera again.
# Afterwards restart the bridge (it holds a websocket to main).
set -euo pipefail
PIDS=$(pgrep -f "^/Applications/OBS.app/Contents/MacOS/OBS" || true)
for p in $PIDS; do kill -TERM "$p" 2>/dev/null || true; done
for _ in $(seq 1 10); do pgrep -f "^/Applications/OBS.app/Contents/MacOS/OBS" >/dev/null || break; sleep 1; done
for p in $(pgrep -f "^/Applications/OBS.app/Contents/MacOS/OBS" || true); do kill -KILL "$p" 2>/dev/null || true; done
sleep 1
open -n "/Applications/OBS.app" --args --multi --profile 'elegato->full_sbs' \
    --collection 'elegato->full_sbs' --websocket_port 4455 --startvirtualcam
echo "main relaunched"
