#!/usr/bin/env python3
"""mem_guard — watch the memory footprint of every OBS instance.

The mini has 8 GB. An OBS instance that leaks (seen: the projector instance at 20 GB, main at
11 GB) pushes everything into swap until macOS runs out of memory. This daemon

  * reads each instance's physical footprint once a minute (what Activity Monitor calls
    Memory; RSS is useless here, a swapped-out leak shows a small RSS);
  * logs all of them every LOG_EVERY_S, so a leak's rate and start time can be read back;
  * warns (log, and STATUS for the control surface's status bar) above WARN_GB;
  * restarts the PROJECTOR instance above RESTART_GB. Only that one: it streams and records
    nothing, so a restart costs a few seconds of projector picture. The others are on air or
    recording and are only reported.

Run detached (see launch_dual.sh):
  nohup control_bridge/.venv/bin/python3 tools/mem_guard.py >> /tmp/mem_guard.log 2>&1 &
"""
import ctypes, json, os, re, subprocess, time

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS = "/tmp/mem_guard.json"
NAMES = {4455: "main", 4456: "twitch", 4457: "youtube", 4458: "projector", 4459: "stereopi",
         4460: "archive"}
WARN_GB = 3.0                 # healthy instances sit at 0.5 GB (relays) to about 1.5 GB (main)
RESTART_GB = 3.0              # projector only
TICK_S, LOG_EVERY_S = 60.0, 600.0
PROJECTOR_APP = "/Applications/OBS Projector.app"
PROJECTOR_ARGS = ["--multi", "--disable-shutdown-check", "--profile", "full_sbs->projector",
                  "--collection", "full_sbs->projector", "--websocket_port", "4458"]

_libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def footprint_gb(pid):
    """ri_phys_footprint from proc_pid_rusage(RUSAGE_INFO_V2): the 8th uint64 after a 16-byte uuid."""
    buf = (ctypes.c_uint64 * 64)()
    if _libproc.proc_pid_rusage(int(pid), 2, ctypes.byref(buf)) != 0:
        return None
    return buf[2 + 7] / 1e9


def instances():
    """{name: pid} of the running OBS instances, by websocket port."""
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(.*/Contents/MacOS/OBS)\s.*--websocket_port (\d+)", line)
        if m and int(m[3]) in NAMES:
            found[NAMES[int(m[3])]] = int(m[1])
    return found


def restart_projector(pid):
    os.kill(pid, 15)
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.5)
    else:
        os.kill(pid, 9)       # a 20 GB instance may be too deep in swap to quit cleanly
        time.sleep(2)
    subprocess.run(["open", "-n", PROJECTOR_APP, "--args", *PROJECTOR_ARGS])
    time.sleep(15)
    # fullscreen it again on the projector display; fails harmlessly if that display is off
    subprocess.run([os.path.join(HERE, "..", "control_bridge", ".venv", "bin", "python3"),
                    os.path.join(HERE, "..", "projector", "projector.py"), "open"],
                   capture_output=True, timeout=60)


def main():
    last_log, warned = 0.0, {}
    while True:
        gb = {}
        for name, pid in instances().items():
            g = footprint_gb(pid)
            if g is not None:
                gb[name] = (pid, g)
        if time.time() - last_log > LOG_EVERY_S:
            last_log = time.time()
            log("  ".join(f"{n} {g:.2f}" for n, (_, g) in sorted(gb.items())) + "  (GB)")
        warn = []
        for name, (pid, g) in gb.items():
            if name == "projector" and g > RESTART_GB:
                log(f"projector instance at {g:.1f} GB: restarting it")
                try:
                    restart_projector(pid)
                    log("projector instance restarted")
                except Exception as e:
                    log(f"projector restart failed ({e!r})")
            elif g > WARN_GB:
                warn.append(f"{name} {g:.0f} GB")
                if time.time() - warned.get(name, 0) > 3600:
                    warned[name] = time.time()
                    log(f"WARNING: {name} instance at {g:.1f} GB (relaunch it when you can)")
        st = {"t": time.time(), "gb": {n: round(g, 2) for n, (_, g) in gb.items()},
              "warn": ("OBS memory: " + ", ".join(warn)) if warn else ""}
        try:
            with open(STATUS + ".tmp", "w") as f:
                json.dump(st, f)
            os.replace(STATUS + ".tmp", STATUS)
        except Exception:
            pass
        time.sleep(TICK_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
