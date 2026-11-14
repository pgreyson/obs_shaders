#!/usr/bin/env bash
# pi_conf.sh — edit a StereoPi's /boot config files through its root web console.
#
#   tools/pi_conf.sh <ip> tft-off        disable Cosmostreamer's TFT mirror daemon
#                                        (tft_display_enabled/model, touchscreen_enabled -> 0)
#   tools/pi_conf.sh <ip> freeze-exp     config.txt: gpu_mem 160->256, force_turbo=1, arm_freq=1200
#   tools/pi_conf.sh <ip> show           print the relevant lines of both files
#   tools/pi_conf.sh <ip> run '<cmd>'    one console command (quotes ok; no ^ $ [ ] or redirection)
#
# Console facts (see SIGNAL_CHAIN.md): it runs a shell but backslash-escapes ^ $ [ ] and swallows
# redirection, so every sed here is UNANCHORED and value-only. /boot is vfat mounted read-only;
# each edit remounts rw, keeps a .bak-<date> copy, syncs and remounts ro. Nothing here reboots:
# run `tools/pi_conf.sh <ip> run reboot` yourself when the stream can take ~45 s without that camera.
set -euo pipefail
IP=${1:?ip}; CMD=${2:?command}; shift 2 || true
STAMP=$(date +%Y%m%d)

run() {
  local q; q=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$1")
  curl -s -m 15 "http://$IP/console/?command=$q&cd=/" | sed -e 's/<[^>]*>//g' | grep -v '^\s*$' | grep -vE '^(Command|cd=|Enter)' || true
}
alive() { run uptime | grep -q 'load average'; }
rw() { run "mount -o remount,rw /boot" >/dev/null; }
ro() { run sync >/dev/null; run "mount -o remount,ro /boot" >/dev/null; }
backup() { run "cp $1 $1.bak-$STAMP" >/dev/null; }

alive || { echo "$IP: console does not answer (power cycle needed?)"; exit 1; }

case "$CMD" in
  show)
    echo "== $IP config.txt"; run "grep -n -e gpu_mem -e force_turbo -e arm_freq -e start_x /boot/config.txt"
    echo "== $IP cosmostreamer.conf"; run "grep -n -e tft_display -e touchscreen_enabled -e display_enabled /boot/cosmostreamer.conf" | grep -v '#'
    ;;
  tft-off)
    rw; backup /boot/cosmostreamer.conf
    run "sed -i 's/tft_display_enabled=1/tft_display_enabled=0/' /boot/cosmostreamer.conf"
    run "sed -i 's/tft_display_model=1/tft_display_model=0/' /boot/cosmostreamer.conf"
    run "sed -i 's/touchscreen_enabled=1/touchscreen_enabled=0/' /boot/cosmostreamer.conf"
    ro
    echo "== $IP cosmostreamer.conf now:"; run "grep -n -e tft_display -e touchscreen_enabled /boot/cosmostreamer.conf" | grep -v '#'
    run "wc -l /boot/cosmostreamer.conf /boot/cosmostreamer.conf.bak-$STAMP"
    echo "reboot to apply: tools/pi_conf.sh $IP run reboot"
    ;;
  freeze-exp)
    rw; backup /boot/config.txt
    run "sed -i 's/gpu_mem=160/gpu_mem=256/' /boot/config.txt"
    run "sed -i '/gpu_mem=256/a force_turbo=1' /boot/config.txt"
    run "sed -i '/force_turbo=1/a arm_freq=1200' /boot/config.txt"
    run "sed -i '/gpu_mem=256/i # obs_shaders freeze experiment: gpu_mem 160 to 256, fixed 1200 MHz clock. backup: config.txt.bak-$STAMP' /boot/config.txt"
    ro
    echo "== $IP config.txt tail:"; run "tail -n 10 /boot/config.txt"
    echo "reboot to apply: tools/pi_conf.sh $IP run reboot"
    ;;
  run) run "$1" ;;
  *) echo "unknown command $CMD"; exit 2 ;;
esac
