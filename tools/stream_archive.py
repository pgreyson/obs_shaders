#!/usr/bin/env python3
"""stream_archive — keep a continuous copy of the Twitch stream on the Archive volume.

The Twitch relay (:4456) records the stream it is already encoding (profile: AdvOut
RecEncoder = none, i.e. "use stream encoder"; hybrid MP4; a new file every 60 min) into a
LOCAL folder, so a sleeping or unmounted file server never stops the recording. This daemon

  * starts that recording whenever the relay is up and not recording;
  * moves every finished file to ARCHIVE_DIR/YYYY-MM/ (copy to *.part, compare sizes, rename,
    then delete the local file) and simply catches up later if the share is away;
  * stops the recording if the local disk runs low (share away for too long);
  * stops it and refuses to restart if the file grows much faster than the stream — that
    means the relay is still using its old ProRes recording encoder (OBS only picks up a
    changed RecEncoder at launch) and the relay needs a relaunch;
  * warns when the Archive volume is WARN_USED_PCT full (log, and STATUS for the control
    surface, which shows it in its status bar).

About 6.1 Mbit/s = 2.7 GB/h = 66 GB/day.

Run detached (see launch_dual.sh):
  nohup control_bridge/.venv/bin/python3 tools/stream_archive.py > /tmp/stream_archive.log 2>&1 &
"""
import asyncio, json, os, shutil, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "control_bridge"))
import yaml, simpleobsws                     # noqa: E402

PORT = 4456
LOCAL_DIR = os.path.expanduser("~/Movies/twitch_capture")
ARCHIVE_VOLUME = "/Volumes/Archive"
ARCHIVE_DIR = os.path.join(ARCHIVE_VOLUME, "obs_twitch_capture")
STATUS = "/tmp/stream_archive.json"
WARN_USED_PCT = 50.0          # warn when the Archive volume is this full
MIN_LOCAL_GB = 6.0            # stop recording below this much free local disk
MAX_MBIT = 25.0               # a stream-encoder file runs ~6; ProRes runs ~80
SETTLE_S = 90.0               # a file untouched this long (and not the newest) is finished
TICK_S = 30.0
EXTS = (".mp4", ".mov", ".mkv")


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def password():
    cfg = yaml.safe_load(open(os.path.join(HERE, "..", "control_bridge", "config.yaml")))
    return cfg["obs_instances"]["viture"]["password"]


def local_files():
    out = []
    for n in sorted(os.listdir(LOCAL_DIR)):
        p = os.path.join(LOCAL_DIR, n)
        if n.lower().endswith(EXTS) and os.path.isfile(p):
            out.append(p)
    return out


def move_one(path):
    """Copy one finished file to the archive, verify its size, delete the local copy."""
    month = time.strftime("%Y-%m", time.localtime(os.path.getmtime(path)))
    ddir = os.path.join(ARCHIVE_DIR, month)
    os.makedirs(ddir, exist_ok=True)
    dest = os.path.join(ddir, os.path.basename(path))
    size = os.path.getsize(path)
    if not (os.path.exists(dest) and os.path.getsize(dest) == size):
        part = dest + ".part"
        shutil.copyfile(path, part)
        if os.path.getsize(part) != size:
            os.remove(part)
            raise IOError(f"size mismatch after copy: {os.path.basename(path)}")
        os.replace(part, dest)
    os.remove(path)
    return dest, size


class Archive:
    def __init__(self):
        self.ws = None
        self.blocked = False          # relay recording with the wrong encoder: do not restart
        self.said = {}                # message key -> last time logged (rate limit)
        self.moved_gb = 0.0

    def say(self, key, msg, every=3600):
        if time.time() - self.said.get(key, 0) > every:
            self.said[key] = time.time()
            log(msg)

    async def call(self, typ, data=None):
        r = await asyncio.wait_for(self.ws.call(simpleobsws.Request(typ, data or {})), 8)
        if not r.ok():
            raise RuntimeError(f"{typ}: {r.requestStatus.comment}")
        return r.responseData or {}

    async def connect(self):
        self.ws = simpleobsws.WebSocketClient(url=f"ws://127.0.0.1:{PORT}", password=password())
        await asyncio.wait_for(self.ws.connect(), 5)
        await asyncio.wait_for(self.ws.wait_until_identified(), 5)
        self.blocked = False          # a new connection may be a relaunched relay
        log("connected to the Twitch relay")

    async def recording(self):
        """Keep the relay recording; returns True while a file is open."""
        free = shutil.disk_usage(LOCAL_DIR).free / 1e9
        st = await self.call("GetRecordStatus")
        if st["outputActive"]:
            secs = st["outputDuration"] / 1000.0
            mbit = st["outputBytes"] * 8 / 1e6 / secs if secs > 20 else 0.0
            if mbit > MAX_MBIT:
                await self.call("StopRecord")
                self.blocked = True
                log(f"recording stopped: {mbit:.0f} Mbit/s is not the stream encoder. "
                    "Relaunch the Twitch relay so it picks up RecEncoder = none.")
                return False
            if free < MIN_LOCAL_GB:
                await self.call("StopRecord")
                log(f"recording stopped: only {free:.1f} GB free locally (is the Archive share away?)")
                return False
            return True
        if self.blocked:
            self.say("blocked", "not recording: the Twitch relay needs a relaunch (wrong recording encoder)")
        elif free < MIN_LOCAL_GB + 2:
            self.say("lowdisk", f"not recording: only {free:.1f} GB free locally")
        else:
            await self.call("StartRecord")
            log("recording started")
            return True
        return False

    async def mover(self, rec):
        files = local_files()
        if rec and files:
            files = files[:-1]        # the newest is the open one
        files = [f for f in files if time.time() - os.path.getmtime(f) > SETTLE_S]
        if not files:
            return 0.0
        backlog = sum(os.path.getsize(f) for f in files) / 1e9
        if not os.path.ismount(ARCHIVE_VOLUME):
            self.say("noshare", f"{ARCHIVE_VOLUME} is not mounted: {len(files)} files ({backlog:.1f} GB) waiting locally", 900)
            return backlog
        for f in files:
            try:
                dest, size = await asyncio.to_thread(move_one, f)
                self.moved_gb += size / 1e9
                backlog -= size / 1e9
                log(f"archived {os.path.basename(dest)} ({size / 1e9:.2f} GB)")
            except Exception as e:
                self.say("copyfail", f"archive copy failed ({e!r}); will retry", 600)
                break
        return max(backlog, 0.0)

    async def run(self):
        os.makedirs(LOCAL_DIR, exist_ok=True)
        while True:
            rec, relay = False, True
            try:
                if self.ws is None:
                    await self.connect()
                rec = await self.recording()
            except Exception as e:
                relay = False
                self.say("relay", f"Twitch relay not answering ({e!r})", 600)
                try:
                    if self.ws:
                        await self.ws.disconnect()
                except Exception:
                    pass
                self.ws = None
            backlog = await self.mover(rec)
            st = {"t": time.time(), "relay": relay, "recording": rec, "blocked": self.blocked,
                  "backlog_gb": round(backlog, 1), "warn": ""}
            if os.path.ismount(ARCHIVE_VOLUME):
                u = shutil.disk_usage(ARCHIVE_VOLUME)
                pct = 100.0 * (u.total - u.free) / u.total
                st.update(archive_used_pct=round(pct, 1), archive_free_tb=round(u.free / 1e12, 2))
                if pct >= WARN_USED_PCT:
                    st["warn"] = f"Archive volume {pct:.0f}% full ({u.free / 1e12:.1f} TB free)"
                    self.say("full", "WARNING: " + st["warn"])
            else:
                st["warn"] = "Archive share not mounted"
            if self.blocked:
                st["warn"] = "Stream archive off: Twitch relay needs a relaunch"
            elif relay and not rec:
                st["warn"] = st["warn"] or "Stream archive not recording"
            try:
                with open(STATUS + ".tmp", "w") as f:
                    json.dump(st, f)
                os.replace(STATUS + ".tmp", STATUS)
            except Exception:
                pass
            await asyncio.sleep(TICK_S)


if __name__ == "__main__":
    try:
        asyncio.run(Archive().run())
    except KeyboardInterrupt:
        pass
