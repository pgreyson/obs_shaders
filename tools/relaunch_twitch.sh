#!/usr/bin/env bash
# relaunch_twitch.sh — restart ONLY the Twitch relay (:4456). The Twitch stream drops for
# about 30 s and Twitch starts a new past broadcast. Needed when an output setting that OBS
# reads only at launch has changed (e.g. the recording encoder used by tools/stream_archive.py).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERE/control_bridge/.venv/bin/python3"
P="OBS Twitch.app/Contents/MacOS/OBS"
pkill -TERM -f "$P" 2>/dev/null || true
for _ in $(seq 1 15); do pgrep -f "$P" >/dev/null || break; sleep 1; done
pkill -KILL -f "$P" 2>/dev/null || true
sleep 1
open -n "/Applications/OBS Twitch.app" --args --multi --disable-shutdown-check --profile 'full_sbs->twitch' \
    --collection 'full_sbs->twitch' --websocket_port 4456
# start the stream again once the websocket answers (the relay does not auto-start it)
HERE_DIR="$HERE" "$PY" - <<'PYEOF'
import asyncio, os, sys, yaml, simpleobsws
here = os.environ.get("HERE_DIR")
PW = yaml.safe_load(open(os.path.join(here, "control_bridge", "config.yaml")))["obs_instances"]["viture"]["password"]
async def main():
    for _ in range(40):
        try:
            ws = simpleobsws.WebSocketClient(url="ws://127.0.0.1:4456", password=PW)
            await asyncio.wait_for(ws.connect(), 3); await asyncio.wait_for(ws.wait_until_identified(), 3)
            break
        except Exception:
            await asyncio.sleep(1)
    else:
        sys.exit("Twitch relay did not come up")
    await asyncio.sleep(4)
    st = (await ws.call(simpleobsws.Request("GetStreamStatus"))).responseData
    if not st["outputActive"]:
        r = await ws.call(simpleobsws.Request("StartStream"))
        print("stream start:", "ok" if r.ok() else r.requestStatus.comment)
    else:
        print("stream already running")
    await ws.disconnect()
asyncio.run(main())
PYEOF
