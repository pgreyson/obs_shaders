#!/usr/bin/env bash
# relaunch_main.sh — restart ONLY the main OBS instance (:4455), e.g. to load a rebuilt
# stereo-splat plugin. The three relays keep running and keep their streams connected;
# their picture freezes for the ~20 s main is down, then follows the virtual camera again.
# Restarts the bridge once main answers (the bridge only connects at start-up).
set -euo pipefail
PIDS=$(pgrep -f "^/Applications/OBS.app/Contents/MacOS/OBS" || true)
for p in $PIDS; do kill -TERM "$p" 2>/dev/null || true; done
for _ in $(seq 1 10); do pgrep -f "^/Applications/OBS.app/Contents/MacOS/OBS" >/dev/null || break; sleep 1; done
for p in $(pgrep -f "^/Applications/OBS.app/Contents/MacOS/OBS" || true); do kill -KILL "$p" 2>/dev/null || true; done
sleep 1
open -n "/Applications/OBS.app" --args --multi --disable-shutdown-check --profile 'elegato->full_sbs' \
    --collection 'elegato->full_sbs' --websocket_port 4455 --startvirtualcam
echo "main relaunched"
# The bridge only connects to main at start-up: restart it once main's websocket answers,
# or the CV knobs stop reaching OBS (seen 2026-11-15: bridge started while main was still down).
until nc -z localhost 4455 2>/dev/null; do sleep 2; done
sleep 5
# anchored: an unanchored "bridge.py" also matches (and kills) any shell whose command line
# merely mentions it, including the one running this script from a tool
pkill -f "[Pp]ython3? bridge\.py$" 2>/dev/null || pkill -f "control_bridge/bridge\.py$" 2>/dev/null || true
"$(dirname "$0")/../control_bridge/.venv/bin/python3" "$(dirname "$0")/after_launch.py" || true
sleep 1
( cd "$(dirname "$0")/../control_bridge" && nohup .venv/bin/python3 bridge.py > /tmp/owl_bridge.log 2>&1 < /dev/null & )
echo "bridge restarted"
