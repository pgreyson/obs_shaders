#!/usr/bin/env python3
"""net_watch: log when the Mac's link to the router or to the internet goes bad.

The streams glitch when the WiFi link drops packets (2026-11-21: 33 % loss to the router itself
for minutes at a time, with a good signal). This pings the router and a public address every
2 s and writes a line to /tmp/net_watch.log whenever either changes between ok / lossy / down
(over the last 20 s), plus a summary every 10 minutes. Loss to the ROUTER means WiFi or router;
loss only to the INTERNET means the line beyond it.

  nohup python3 tools/net_watch.py > /dev/null 2>&1 &
"""
import subprocess, time, re, collections

TARGETS = {"router": None, "internet": "1.1.1.1"}
LOG = "/tmp/net_watch.log"


def gateway():
    out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True).stdout
    m = re.search(r"gateway: (\S+)", out)
    return m.group(1) if m else "192.168.50.1"


def ping(host):
    r = subprocess.run(["ping", "-c", "1", "-W", "1000", host], capture_output=True, text=True)
    m = re.search(r"time=([\d.]+)", r.stdout)
    return float(m.group(1)) if r.returncode == 0 and m else None


def log(msg):
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def main():
    hist = {k: collections.deque(maxlen=10) for k in TARGETS}
    state = {k: "ok" for k in TARGETS}
    tot = {k: [0, 0] for k in TARGETS}          # sent, lost since the last summary
    last_sum = time.time()
    log("net_watch start")
    while True:
        t0 = time.time()
        for k, host in TARGETS.items():
            ms = ping(host or gateway())
            hist[k].append(ms)
            tot[k][0] += 1
            tot[k][1] += ms is None
            lost = sum(1 for x in hist[k] if x is None)
            new = "down" if lost >= 8 else "lossy" if lost >= 2 else "ok"
            if new != state[k] and len(hist[k]) == 10:
                good = [x for x in hist[k] if x is not None]
                log(f"{k}: {state[k]} -> {new} ({lost}/10 lost" + (f", {sum(good) / len(good):.0f} ms avg" if good else "") + ")")
                state[k] = new
        if time.time() - last_sum > 600:
            log("10 min: " + ", ".join(f"{k} {100 * v[1] / max(1, v[0]):.1f} % lost" for k, v in tot.items()))
            tot = {k: [0, 0] for k in TARGETS}
            last_sum = time.time()
        time.sleep(max(0.0, 2.0 - (time.time() - t0)))

main()
