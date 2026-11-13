#!/usr/bin/env bash
# launch_dual.sh — start the four OBS instances (main + three relays).
#
# Instance 1: main pipeline — Elgato in, full-SBS out (shaders live here).
#   profile = elegato->full_sbs       (canvas 3840×1080)
#   scene   = elegato->full_sbs
#   obs-websocket port = 4455
#   Also runs the virtual camera (its program feed), which instance 2 consumes.
# Instance 2: stream relay — instance 1's program in, squash/letterbox out.
#   profile = full_sbs->twitch        (canvas 1920×1080, VideoToolbox)
#   scene   = full_sbs->twitch        (scenes: half_sbs squash, full_sbs letterbox)
#   obs-websocket port = 4456
#
# Instance 3: YouTube relay — same as instance 2 but letterboxed to YouTube.
#   profile = full_sbs->youtube       (canvas 1920×1080, VideoToolbox h264)
#   scene   = full_sbs->youtube       (clone of full_sbs->stream; program = full_sbs)
#   obs-websocket port = 4457
#   youtube_live/yt_live.py manages its broadcast + thumbnail.
#
# Instance 4: projector relay — instance 1's program in, half-SBS squash out to the
#   3D projector display via a fullscreen Program projector; streams nowhere.
#   profile = full_sbs->projector     (canvas 1920×1080; Audio monitoring → projector HDMI)
#   scene   = full_sbs->projector     (clone of full_sbs->stream; program = half_sbs)
#   obs-websocket port = 4458
#   projector/projector.py opens the fullscreen projector + sets the monitoring device.
#
# The control_bridge daemon talks to instances 1+2 via their websocket ports.

set -euo pipefail

# One renamed clone of OBS.app per instance so the Dock / app switcher show which is
# which (APFS clones: near-zero disk; Info.plist CFBundleName/DisplayName changed and the
# bundle ad-hoc re-signed — see SIGNAL_CHAIN.md). Plugin updates must be applied to each.
OBS_APP="/Applications/OBS.app"               # fallback
OBS_APP_MAIN="/Applications/OBS Main.app"
OBS_APP_TWITCH="/Applications/OBS Twitch.app"
OBS_APP_YOUTUBE="/Applications/OBS YouTube.app"
OBS_APP_PROJECTOR="/Applications/OBS Projector.app"
for a in "$OBS_APP_MAIN" "$OBS_APP_TWITCH" "$OBS_APP_YOUTUBE" "$OBS_APP_PROJECTOR"; do [ -d "$a" ] || { echo "missing $a — falling back to $OBS_APP"; OBS_APP_MAIN=$OBS_APP; OBS_APP_TWITCH=$OBS_APP; OBS_APP_YOUTUBE=$OBS_APP; OBS_APP_PROJECTOR=$OBS_APP; break; }; done

VITURE_PROFILE='elegato->full_sbs'
VITURE_COLLECTION='elegato->full_sbs'
VITURE_WS_PORT=4455

PROJECTOR_PROFILE='full_sbs->twitch'
PROJECTOR_COLLECTION='full_sbs->twitch'
PROJECTOR_WS_PORT=4456

YOUTUBE_PROFILE='full_sbs->youtube'
YOUTUBE_COLLECTION='full_sbs->youtube'
YOUTUBE_WS_PORT=4457

HALFSBS_PROFILE='full_sbs->projector'
HALFSBS_COLLECTION='full_sbs->projector'
HALFSBS_WS_PORT=4458

echo "Stopping any existing OBS instances..."
# OBS sometimes hangs on a hidden "save changes?" dialog and ignores both
# osascript quit and SIGTERM. Try the polite paths, then escalate to KILL
# for any PIDs that are still alive — we have nothing in the UI worth
# preserving since OBS auto-saves scene collections every minute.
osascript -e 'tell application "OBS" to quit' >/dev/null 2>&1 || true
sleep 1
for pid in $(pgrep -f "OBS[ A-Za-z]*.app/Contents/MacOS/OBS" 2>/dev/null); do
    kill -TERM "$pid" 2>/dev/null || true
done
sleep 2
for pid in $(pgrep -f "OBS[ A-Za-z]*.app/Contents/MacOS/OBS" 2>/dev/null); do
    echo "  PID $pid did not exit on TERM, sending KILL"
    kill -KILL "$pid" 2>/dev/null || true
done
# Final guard: wait until they're truly gone before launching new ones,
# else the new `open -n` calls can race with the dying processes.
for _ in 1 2 3 4 5; do
    pgrep -f "OBS[ A-Za-z]*.app/Contents/MacOS/OBS" >/dev/null 2>&1 || break
    sleep 1
done

echo "Launching Viture instance (ws :$VITURE_WS_PORT)..."
open -n "$OBS_APP_MAIN" --args \
    --multi \
    --profile "$VITURE_PROFILE" \
    --collection "$VITURE_COLLECTION" \
    --websocket_port "$VITURE_WS_PORT" \
    --startvirtualcam

# Give OBS time to come up before launching the sibling so they don't race
# on shared config files (user.ini etc.).
sleep 3

echo "Launching Twitch relay instance (ws :$PROJECTOR_WS_PORT)..."
open -n "$OBS_APP_TWITCH" --args \
    --multi \
    --profile "$PROJECTOR_PROFILE" \
    --collection "$PROJECTOR_COLLECTION" \
    --websocket_port "$PROJECTOR_WS_PORT"

sleep 3

echo "Launching YouTube instance (ws :$YOUTUBE_WS_PORT)..."
open -n "$OBS_APP_YOUTUBE" --args \
    --multi \
    --profile "$YOUTUBE_PROFILE" \
    --collection "$YOUTUBE_COLLECTION" \
    --websocket_port "$YOUTUBE_WS_PORT"

sleep 3

echo "Launching half-SBS projector instance (ws :$HALFSBS_WS_PORT)..."
open -n "$OBS_APP_PROJECTOR" --args \
    --multi \
    --profile "$HALFSBS_PROFILE" \
    --collection "$HALFSBS_COLLECTION" \
    --websocket_port "$HALFSBS_WS_PORT"

# Fullscreen the program output on the projector display (best effort: skipped if
# no projector-looking display is attached).
HERE="$(cd "$(dirname "$0")" && pwd)"
( sleep 12; "$HERE/control_bridge/.venv/bin/python3" "$HERE/projector/projector.py" open \
      || echo "  (projector display not found — plug it in and run: projector/projector.py open)" ) &

# Audio watchdog: re-arms each instance's Elgato audio capture when it silently stalls
# (tools/audio_watchdog.py). Detached so it outlives this shell; skipped if already running.
if ! pgrep -f tools/audio_watchdog.py >/dev/null; then
  nohup "$HERE/control_bridge/.venv/bin/python3" "$HERE/tools/audio_watchdog.py" > /tmp/audio_watchdog.log 2>&1 &
  echo "audio watchdog started (log: /tmp/audio_watchdog.log)"
fi

echo
echo "All four OBS instances launched."
echo "Viture instance  : profile=$VITURE_PROFILE, collection=$VITURE_COLLECTION, ws=:$VITURE_WS_PORT"
echo "Projector instance: profile=$PROJECTOR_PROFILE, collection=$PROJECTOR_COLLECTION, ws=:$PROJECTOR_WS_PORT"
echo "YouTube instance  : profile=$YOUTUBE_PROFILE, collection=$YOUTUBE_COLLECTION, ws=:$YOUTUBE_WS_PORT"
echo "Half-SBS projector: profile=$HALFSBS_PROFILE, collection=$HALFSBS_COLLECTION, ws=:$HALFSBS_WS_PORT"
echo
echo "Next: start the control bridge with"
echo "  cd $(dirname "$0")/control_bridge && python3 bridge.py"
