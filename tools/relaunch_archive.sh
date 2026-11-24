#!/usr/bin/env bash
# relaunch_archive.sh — restart ONLY the archive instance (:4460). It streams nowhere, so
# nothing on air changes; the continuous recording gets a gap of about 30 s and
# tools/stream_archive.py starts it again by itself.
set -euo pipefail
P="OBS Archive.app/Contents/MacOS/OBS"
pkill -TERM -f "$P" 2>/dev/null || true
for _ in $(seq 1 15); do pgrep -f "$P" >/dev/null || break; sleep 1; done
pkill -KILL -f "$P" 2>/dev/null || true
sleep 1
open -n "/Applications/OBS Archive.app" --args --multi --disable-shutdown-check --profile 'full_sbs->archive' \
    --collection 'full_sbs->archive' --websocket_port 4460
