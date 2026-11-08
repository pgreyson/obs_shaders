#!/usr/bin/env python3
"""projector.py — drive the 4th OBS instance (full_sbs->projector, ws :4458).

  status                 instance state, monitors OBS can see, monitoring device
  open [--monitor NAME]  fullscreen the PROGRAM output on the projector display
                         (default: first monitor that is not VITURE/Sidecar/Virtual/Built-in)
  devices                list CoreAudio output devices (name + UID) — the projector's HDMI audio
                         shows up here (e.g. "LTV") even while its display is asleep
  audio NAME_SUBSTRING   set the profile's monitoring device to that output, then
                         restart the instance so it takes effect
  close                  close all projector windows of this instance
  show                   bring this instance's CONTROL window to the front (all four
                         instances are called "OBS"; System Events must be matched by pid)

Run with: control_bridge/.venv/bin/python3 projector/projector.py <cmd>
"""
import argparse, asyncio, configparser, os, re, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control_bridge"))
import simpleobsws  # noqa: E402

PORT = 4458


def _password() -> str:
    """obs-websocket password: shared by all instances, kept in control_bridge/config.yaml (gitignored)."""
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "control_bridge/config.yaml").read_text())
    inst = cfg["obs_instances"]
    first = inst[0] if isinstance(inst, list) else next(iter(inst.values()))
    return first["password"]


PASSWORD = _password()
PROFILE_INI = Path.home() / "Library/Application Support/obs-studio/basic/profiles/full_sbs-projector/basic.ini"
PROFILE, COLLECTION = "full_sbs->projector", "full_sbs->projector"
OBS_APP = "/Applications/OBS Projector.app" if os.path.isdir("/Applications/OBS Projector.app") else "/Applications/OBS.app"
NOT_PROJECTOR = re.compile(r"viture|sidecar|virtual|built-in|color lcd|mac mini", re.I)


class WS:
    async def __aenter__(self):
        self.ws = simpleobsws.WebSocketClient(url=f"ws://localhost:{PORT}", password=PASSWORD)
        await self.ws.connect()
        if not await self.ws.wait_until_identified(timeout=5):
            raise SystemExit(f"no OBS instance answering on ws :{PORT} (is full_sbs->projector running?)")
        return self

    async def __aexit__(self, *a):
        await self.ws.disconnect()

    async def call(self, name, data=None):
        r = await self.ws.call(simpleobsws.Request(name, data or {}))
        if not r.ok():
            raise RuntimeError(f"{name}: {r.requestStatus.comment}")
        return r.responseData or {}


def pick_monitor(monitors, want):
    if want:
        hits = [m for m in monitors if want.lower() in m["monitorName"].lower()]
        if not hits:
            raise SystemExit(f"no monitor matching {want!r}; have: {[m['monitorName'] for m in monitors]}")
        return hits[0]
    cands = [m for m in monitors if not NOT_PROJECTOR.search(m["monitorName"])]
    if not cands:
        raise SystemExit("no projector-looking display attached; monitors: "
                         + ", ".join(m["monitorName"] for m in monitors))
    return cands[0]


async def cmd_status(_):
    async with WS() as o:
        v = await o.call("GetVersion")
        p = await o.call("GetProfileList"); c = await o.call("GetSceneCollectionList")
        s = await o.call("GetCurrentProgramScene")
        m = await o.call("GetInputMute", {"inputName": "Mic/Aux"})
        mt = await o.call("GetInputAudioMonitorType", {"inputName": "Mic/Aux"})
        mons = (await o.call("GetMonitorList"))["monitors"]
    ini = configparser.ConfigParser(strict=False); ini.read(PROFILE_INI)
    print(f"OBS {v['obsVersion']}  profile={p['currentProfileName']}  collection={c['currentSceneCollectionName']}")
    print(f"program scene: {s['currentProgramSceneName']}")
    print(f"Mic/Aux muted={m['inputMuted']} monitor={mt['monitorType'].replace('OBS_MONITORING_TYPE_','')}")
    print(f"monitoring device: {ini['Audio'].get('MonitoringDeviceName')} [{ini['Audio'].get('MonitoringDeviceId')}]")
    print("monitors:")
    for x in mons:
        print(f"  {x['monitorIndex']}: {x['monitorName']} {x['monitorWidth']}x{x['monitorHeight']} @({x['monitorPositionX']},{x['monitorPositionY']})")


async def cmd_open(a):
    async with WS() as o:
        mons = (await o.call("GetMonitorList"))["monitors"]
        m = pick_monitor(mons, a.monitor)
        await o.call("OpenVideoMixProjector", {"videoMixType": "OBS_WEBSOCKET_VIDEO_MIX_TYPE_PROGRAM",
                                               "monitorIndex": m["monitorIndex"]})
    print(f"program projector opened on monitor {m['monitorIndex']}: {m['monitorName']} {m['monitorWidth']}x{m['monitorHeight']}")


def list_devices():
    """CoreAudio output devices as (name, uid) — obs-websocket cannot enumerate monitoring devices."""
    from coreaudio_outputs import output_devices
    return output_devices()


async def cmd_devices(_):
    for name, uid in list_devices():
        print(f"{name}\n    {uid}")


def _obs_pids():
    out = subprocess.run(["pgrep", "-fl", "OBS[ A-Za-z]*.app/Contents/MacOS/OBS"], capture_output=True, text=True).stdout
    return [int(l.split()[0]) for l in out.splitlines() if "--websocket_port 4458" in l or f"full_sbs->projector" in l]


def restart_instance():
    for pid in _obs_pids():
        os.kill(pid, 15)
    for _ in range(20):
        if not _obs_pids():
            break
        time.sleep(0.5)
    subprocess.run(["open", "-n", OBS_APP, "--args", "--multi", "--profile", PROFILE,
                    "--collection", COLLECTION, "--websocket_port", str(PORT)], check=True)


async def cmd_audio(a):
    devs = list_devices()
    hits = [(n, u) for n, u in devs if a.name.lower() in n.lower()]
    if len(hits) != 1:
        raise SystemExit(f"need exactly one match for {a.name!r}, got {[n for n, _ in hits] or [n for n, _ in devs]}")
    name, uid = hits[0]
    txt = PROFILE_INI.read_text()
    txt = re.sub(r"^MonitoringDeviceId=.*$", f"MonitoringDeviceId={uid}", txt, flags=re.M)
    txt = re.sub(r"^MonitoringDeviceName=.*$", f"MonitoringDeviceName={name}", txt, flags=re.M)
    PROFILE_INI.write_text(txt)
    print(f"monitoring device -> {name} [{uid}]; restarting full_sbs->projector so it takes effect")
    restart_instance()
    time.sleep(8)
    if a.open:
        await cmd_open(a)


async def cmd_show(_):
    out = subprocess.run(["pgrep", "-f", f"websocket_port {PORT}"], capture_output=True, text=True).stdout.split()
    if not out:
        raise SystemExit(f"no OBS instance on ws :{PORT}")
    script = f'''
tell application "System Events"
  repeat with p in (every process whose name is "OBS")
    if (unix id of p) is {out[0]} then
      set visible of p to true
      set frontmost of p to true
      repeat with w in windows of p
        try
          set value of attribute "AXMinimized" of w to false
        end try
        if name of w starts with "OBS" then perform action "AXRaise" of w
      end repeat
      return name of first window of p
    end if
  end repeat
end tell'''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    print("front window:", r.stdout.strip() or r.stderr.strip())


async def cmd_close(_):
    async with WS() as o:
        # obs-websocket has no CloseProjector; toggling the projector monitor to -1 is not supported either.
        print("close projector windows from the OBS UI (right-click projector → Close) or restart the instance")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sp = sub.add_parser("open"); sp.add_argument("--monitor", help="substring of the display name"); sp.set_defaults(fn=cmd_open)
    sub.add_parser("devices").set_defaults(fn=cmd_devices)
    sp = sub.add_parser("audio"); sp.add_argument("name"); sp.add_argument("--monitor"); sp.add_argument("--open", action="store_true")
    sp.set_defaults(fn=cmd_audio)
    sub.add_parser("close").set_defaults(fn=cmd_close)
    sub.add_parser("show").set_defaults(fn=cmd_show)
    a = p.parse_args()
    asyncio.run(a.fn(a))


if __name__ == "__main__":
    main()
