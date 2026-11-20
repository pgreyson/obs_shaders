#!/usr/bin/env python3
"""pi_watchdog — reboot a StereoPi whose camera feed has frozen.

Both StereoPis (CM4, firmware 2021-11-08, legacy raspivid stack) freeze every few hours:
the VideoCore firmware stops answering (mailbox wedge: `vcgencmd` prints nothing) or
raspivid simply stalls, while Linux, ping and the web console stay up. The picture in OBS
sits on one frame until the Pi is rebooted. This watches main's two camera sources and
reboots the matching Pi through its root web console.

Decision, per source, every POLL_S:
  1. GetSourceScreenshot (small PNG) of the raw input; the frame is "static" when its hash
     has not changed for STALL_S. Camera noise normally changes the hash every poll.
  2. Confirm on the Pi before acting (static content is not proof): raspivid's CPU ticks
     (/proc/<pid>/stat utime+stime) must be unchanged over CONFIRM_S too, or `vcgencmd
     measure_temp` must answer empty (the mailbox form). raspivid not found also counts.
  3. `?command=reboot` on the console, then COOLDOWN_S before this Pi is considered again
     (boot + first frames take ~40–60 s).
A Pi whose console does not answer cannot be rebooted from here; that is logged once and
needs a power cycle.

Run detached (launch_dual.sh does):  nohup .venv/bin/python3 tools/pi_watchdog.py &
Log: /tmp/pi_watchdog.log.  Talks to main (:4455) only.
"""
import asyncio
import hashlib
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import simpleobsws
import subprocess
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = yaml.safe_load(open(os.path.join(HERE, "..", "control_bridge", "config.yaml")))["obs_instances"]["viture"]
URL = f"ws://{CFG.get('host', 'localhost')}:{CFG['port']}"

PIS = {  # OBS input name -> Pi web console host (last known; refreshed by MAC, see resolve())
    # WiFi "rofo" since 2026-11-17 (192.168.50.x, DHCP: addresses drift without a router
    # reservation: stage went .19 -> .105 on 2026-11-18). Old network: stage 10.0.1.38, room 10.0.1.45.
    "StereoPi stage": "192.168.50.105",
    # room: has not joined "rofo" (seen only at a self-assigned 169.254 address), so it cannot
    # be rebooted from here until its WiFi settings are updated.
    "StereoPi room": "192.168.50.0",
}
MACS = {
    "StereoPi stage": "dc:a6:32:ff:13:9c",
    "StereoPi room": "d8:3a:dd:95:92:4a",
}
SUBNET = "192.168.50."


def resolve(name):
    """Current address of a Pi by MAC from the ARP table (after a broadcast ping to fill it);
    updates PIS and returns the host. Falls back to the last known address."""
    mac = [int(x, 16) for x in MACS[name].split(":")]
    try:
        subprocess.run(["ping", "-c", "2", "-t", "2", SUBNET + "255"], capture_output=True, timeout=6)
        out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=6).stdout
    except Exception:
        return PIS[name]
    for m in re.finditer(r"\((\S+)\) at ([0-9a-f:]+)", out):
        ip, got = m.group(1), m.group(2)
        try:
            if ip.startswith(SUBNET) and [int(x, 16) for x in got.split(":")] == mac and ip != PIS[name]:
                log(f"{name}: address changed {PIS[name]} -> {ip}")
                PIS[name] = ip
        except ValueError:
            pass
    return PIS[name]


POLL_S = 5.0
STALL_S = 30.0        # identical frames for this long -> suspect
CONFIRM_S = 4.0       # raspivid tick sample interval
COOLDOWN_S = 240.0    # after a reboot, leave the Pi alone this long
HEALTH_S = 600.0      # per-Pi health line (temp, GPU heap headroom, alloc failures) this often
LOG = "/tmp/pi_watchdog.log"


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    try:
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def console(host, command, timeout=8):
    """Run one command on the StereoPi root web console; returns plain text or None."""
    q = urllib.parse.urlencode({"command": command, "cd": "/"})
    try:
        with urllib.request.urlopen(f"http://{host}/console/?{q}", timeout=timeout) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    return re.sub(r"<[^>]*>", "", html)


def raspivid_ticks(host):
    """(pid, utime+stime) of the raspivid process, or (None, None) if not running / no answer."""
    ps = console(host, "ps aux")
    if ps is None:
        return None, None
    for line in ps.splitlines():
        if "bin/raspivid -t" in line and "sh -c" not in line:
            pid = line.split()[1]
            st = console(host, f"cat /proc/{pid}/stat")
            if st:
                for l in st.splitlines():
                    if l.startswith(pid + " "):
                        f = l.split()
                        return pid, int(f[13]) + int(f[14])
            return pid, None
    return None, None


def pi_health(host):
    """One line of evidence for the config experiment, independent of whether the scene moves:
    temperature, free relocatable GPU heap (camera + encoder live there; the stock 160M config
    leaves ~20M), the firmware's alloc_fails counter, raspivid CPU ticks over CONFIRM_S."""
    up = console(host, "uptime")
    if up is None or "load average" not in up:
        return f"{host}: console unreachable"
    m = re.search(r"up\s+(.*?),\s+\d+ users?", up)
    uptime = m.group(1).strip() if m else "?"
    temp = (console(host, "vcgencmd measure_temp") or "").strip() or "temp=EMPTY(mailbox wedged)"
    reloc = (console(host, "vcgencmd get_mem reloc") or "").strip() or "reloc=?"
    stats = console(host, "vcdbg reloc stats") or ""
    fails = re.search(r"alloc_fails\s*:\s*(\d+)", stats)
    fails = fails.group(1) if fails else "?"
    pid, t0 = raspivid_ticks(host)
    time.sleep(CONFIRM_S)
    _, t1 = raspivid_ticks(host)
    ticks = "raspivid=none" if pid is None else (f"raspivid_ticks/{CONFIRM_S:.0f}s={t1 - t0}" if None not in (t0, t1) else "raspivid_ticks=?")
    return f"{host}: up {uptime} | {temp} | {reloc} alloc_fails={fails} | {ticks}"


def pi_confirms_stall(host):
    """True when the Pi itself shows the camera pipeline is dead; None when it cannot be reached."""
    up = console(host, "uptime")
    if up is None or "load average" not in up:
        return None
    temp = console(host, "vcgencmd measure_temp") or ""
    if "temp=" not in temp:
        log(f"  {host}: vcgencmd answers empty -> firmware mailbox wedged")
        return True
    pid, t0 = raspivid_ticks(host)
    if pid is None:
        log(f"  {host}: no raspivid process")
        return True
    time.sleep(CONFIRM_S)
    _, t1 = raspivid_ticks(host)
    if t0 is not None and t1 is not None and t1 == t0:
        log(f"  {host}: raspivid pid {pid} consumed 0 CPU ticks in {CONFIRM_S:.0f} s -> stalled")
        return True
    return False


async def main():
    log(f"pi_watchdog start: {PIS}  stall {STALL_S:.0f}s cooldown {COOLDOWN_S:.0f}s")
    last_hash = {n: None for n in PIS}
    static_since = {n: None for n in PIS}
    cooldown_until = {n: 0.0 for n in PIS}
    unreachable_logged = {n: False for n in PIS}
    healthy_static_logged = {n: False for n in PIS}
    next_health = 0.0
    ws = None
    while True:
        try:
            if ws is None:
                ws = simpleobsws.WebSocketClient(url=URL, password=CFG["password"])
                await asyncio.wait_for(ws.connect(), 10)
                await asyncio.wait_for(ws.wait_until_identified(), 10)
                log("connected to OBS main")
            now = time.time()
            if now >= next_health:
                next_health = now + HEALTH_S
                for name, host in PIS.items():
                    line = await asyncio.get_event_loop().run_in_executor(None, pi_health, host)
                    log(f"health {name}: {line}")
            for name, host in PIS.items():
                r = await ws.call(simpleobsws.Request("GetSourceScreenshot", {
                    "sourceName": name, "imageFormat": "png", "imageWidth": 320}), timeout=15)
                if not r.ok():
                    continue
                h = hashlib.md5(r.responseData.get("imageData", "").encode()).hexdigest()
                if h != last_hash[name]:
                    last_hash[name] = h
                    static_since[name] = None
                    unreachable_logged[name] = False
                    healthy_static_logged[name] = False
                    continue
                if static_since[name] is None:
                    static_since[name] = now
                    continue
                if now - static_since[name] < STALL_S or now < cooldown_until[name]:
                    continue
                if not healthy_static_logged[name]:
                    log(f"{name}: frame unchanged for {now - static_since[name]:.0f} s, checking {host}")
                verdict = await asyncio.get_event_loop().run_in_executor(None, pi_confirms_stall, host)
                if verdict is None:
                    moved = await asyncio.get_event_loop().run_in_executor(None, resolve, name)
                    if moved != host:
                        host = moved
                        verdict = await asyncio.get_event_loop().run_in_executor(None, pi_confirms_stall, host)
                if verdict is None:
                    if not unreachable_logged[name]:
                        log(f"{name}: {host} console does not answer -> cannot reboot from here (power cycle needed)")
                        unreachable_logged[name] = True
                    cooldown_until[name] = now + 60.0
                elif verdict:
                    log(f"{name}: REBOOTING {host}")
                    await asyncio.get_event_loop().run_in_executor(None, console, host, "reboot", 10)
                    cooldown_until[name] = now + COOLDOWN_S
                    static_since[name] = None
                else:
                    if not healthy_static_logged[name]:
                        log(f"{name}: Pi healthy, scene is just still; re-checking quietly every 60 s")
                        healthy_static_logged[name] = True
                    cooldown_until[name] = now + 60.0
        except Exception as e:  # OBS restart, websocket drop, ...
            log(f"error: {type(e).__name__}: {e}; reconnecting in 10 s")
            try:
                if ws is not None:
                    await ws.disconnect()
            except Exception:
                pass
            ws = None
            await asyncio.sleep(10)
            continue
        await asyncio.sleep(POLL_S)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
