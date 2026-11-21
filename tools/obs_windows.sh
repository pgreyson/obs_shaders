#!/usr/bin/env bash
# obs_windows.sh minimize|restore — minimise (or bring back) the main window of every OBS
# instance. Each open OBS window draws its preview on the GPU the instances share: measured
# 2026-11-21 with synth + both cameras on, main took 38.6 ms a frame (26 fps) with the three
# windows open and 33 ms (30 fps, no skipped frames) with them minimised. Fullscreen
# projectors (Viture, LTV) are separate windows and are not touched.
# Needs Accessibility permission for the app that runs it (System Settings > Privacy).
set -euo pipefail
case "${1:-minimize}" in minimize) V=true ;; restore) V=false ;; *) echo "usage: $0 minimize|restore"; exit 2 ;; esac
for pid in $(pgrep -f "OBS[ A-Za-z]*\.app/Contents/MacOS/OBS( |$)" || true); do
  osascript -e "tell application \"System Events\" to tell (first process whose unix id is $pid) to set value of attribute \"AXMinimized\" of (every window whose name contains \"Profile\") to $V" >/dev/null 2>&1 \
    || echo "  could not reach the window of OBS pid $pid (Accessibility permission?)"
done
