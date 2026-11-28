#!/usr/bin/env bash
# mount_shares.sh — mount the Mac Studio's Workspace and Archive shares (main's ProRes
# recordings and the 30-day archive live there). macOS does not remount them after a restart.
# Uses the password saved in the login keychain; retries for 10 minutes because the network
# and the Studio may not be ready at login. Run by the login agent
# ~/Library/LaunchAgents/com.obs-shaders.shares.plist and by launch_dual.sh.
SERVER="smb://paulgreyson@Paul’s Mac Studio._smb._tcp.local"
for _ in $(seq 1 40); do
  missing=0
  for s in Workspace Archive; do
    if [ ! -d "/Volumes/$s" ]; then
      osascript -e 'with timeout of 25 seconds' -e "mount volume \"$SERVER/$s\"" -e 'end timeout' >/dev/null 2>&1
      [ -d "/Volumes/$s" ] && echo "$(date '+%F %T') mounted $s" || missing=1
    fi
  done
  [ $missing = 0 ] && exit 0
  sleep 15
done
echo "$(date '+%F %T') shares still missing after 10 minutes"; exit 1
