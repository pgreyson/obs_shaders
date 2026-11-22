#!/usr/bin/env bash
# relaunch_projector.sh — restart ONLY the projector relay (:4458). It streams nowhere, so
# only the LTV projector picture/audio drops for ~20 s. Needed because this instance leaks
# (about 290 MB/h of MALLOC_LARGE, 9 GB after 31 h on 2026-11-16); see SIGNAL_CHAIN.md.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
P="OBS Projector.app/Contents/MacOS/OBS"
pkill -TERM -f "$P" 2>/dev/null || true
for _ in $(seq 1 10); do pgrep -f "$P" >/dev/null || break; sleep 1; done
pkill -KILL -f "$P" 2>/dev/null || true
sleep 1
open -n "/Applications/OBS Projector.app" --args --multi --disable-shutdown-check --profile 'full_sbs->projector' \
    --collection 'full_sbs->projector' --websocket_port 4458
sleep 12
"$HERE/control_bridge/.venv/bin/python3" "$HERE/projector/projector.py" open || echo "projector display not found"
