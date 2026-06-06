#!/usr/bin/env bash
# launch_dual.sh — start two OBS instances, each pinned to one display target.
#
# Instance 1: Viture full-SBS pipeline.
#   profile = structure->elegato->viture_half_to_full_sbs  (canvas 3840×1080)
#   scene   = structure-elegato-viture
#   obs-websocket port = 4455
# Instance 2: 3D projector half-SBS pipeline.
#   profile = structure->elegato->projector_halfsbs        (canvas 1920×1080)
#   scene   = structure-elegato-stereo
#   obs-websocket port = 4456
#
# The control_bridge daemon talks to both via the two websocket ports.

set -euo pipefail

OBS_APP="/Applications/OBS.app"

VITURE_PROFILE='structure->elegato->viture_half_to_full_sbs'
VITURE_COLLECTION='structure-elegato-viture'
VITURE_WS_PORT=4455

PROJECTOR_PROFILE='structure->elegato->projector_halfsbs'
PROJECTOR_COLLECTION='structure-elegato-stereo'
PROJECTOR_WS_PORT=4456

echo "Stopping any existing OBS instances..."
# OBS sometimes hangs on a hidden "save changes?" dialog and ignores both
# osascript quit and SIGTERM. Try the polite paths, then escalate to KILL
# for any PIDs that are still alive — we have nothing in the UI worth
# preserving since OBS auto-saves scene collections every minute.
osascript -e 'tell application "OBS" to quit' >/dev/null 2>&1 || true
sleep 1
for pid in $(pgrep -f "OBS.app/Contents/MacOS/OBS" 2>/dev/null); do
    kill -TERM "$pid" 2>/dev/null || true
done
sleep 2
for pid in $(pgrep -f "OBS.app/Contents/MacOS/OBS" 2>/dev/null); do
    echo "  PID $pid did not exit on TERM, sending KILL"
    kill -KILL "$pid" 2>/dev/null || true
done
# Final guard: wait until they're truly gone before launching new ones,
# else the new `open -n` calls can race with the dying processes.
for _ in 1 2 3 4 5; do
    pgrep -f "OBS.app/Contents/MacOS/OBS" >/dev/null 2>&1 || break
    sleep 1
done

echo "Launching Viture instance (ws :$VITURE_WS_PORT)..."
open -n "$OBS_APP" --args \
    --multi \
    --profile "$VITURE_PROFILE" \
    --collection "$VITURE_COLLECTION" \
    --websocket_port "$VITURE_WS_PORT"

# Give OBS time to come up before launching the sibling so they don't race
# on shared config files (user.ini etc.).
sleep 3

echo "Launching projector instance (ws :$PROJECTOR_WS_PORT)..."
open -n "$OBS_APP" --args \
    --multi \
    --profile "$PROJECTOR_PROFILE" \
    --collection "$PROJECTOR_COLLECTION" \
    --websocket_port "$PROJECTOR_WS_PORT"

echo
echo "Both OBS instances launched."
echo "Viture instance  : profile=$VITURE_PROFILE, collection=$VITURE_COLLECTION, ws=:$VITURE_WS_PORT"
echo "Projector instance: profile=$PROJECTOR_PROFILE, collection=$PROJECTOR_COLLECTION, ws=:$PROJECTOR_WS_PORT"
echo
echo "Next: start the control bridge with"
echo "  cd $(dirname "$0")/control_bridge && python3 bridge.py"
