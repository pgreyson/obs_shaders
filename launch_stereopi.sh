#!/usr/bin/env bash
# launch_stereopi.sh — start the OPTIONAL 5th OBS instance for the stereoscopic camera.
#
#   profile = stereopi->full_sbs   (canvas 3840×1080, same as main; streams nowhere)
#   scene   = stereopi->full_sbs   (source "StereoPi AV" = Blackmagic UltraStudio Recorder 3G
#                                   carrying the StereoPi's 1920×1080 SBS feed, with the main
#                                   instance's full shader chain in SBS mode: depth block off,
#                                   sbs align on, synth color soften 0, pillar box passthrough)
#   obs-websocket port = 4459
#
# Separate from launch_dual.sh because the camera is only on some of the time.
# Run it after the camera has booted (~1 min); stop with:  pkill -f "websocket_port 4459"
set -euo pipefail
OBS_APP="/Applications/OBS.app"
PORT=4459
if pgrep -f "websocket_port $PORT" >/dev/null; then
    echo "stereopi->full_sbs already running (ws :$PORT)"; exit 0
fi
echo "Launching stereopi->full_sbs (ws :$PORT)..."
open -n "$OBS_APP" --args --multi --profile 'stereopi->full_sbs' --collection 'stereopi->full_sbs' --websocket_port "$PORT"
