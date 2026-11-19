#!/usr/bin/env python3
"""camera_chain.py — the camera-layer filter on main's `stage` / `room` scenes.

Since 2026-11-18 each camera scene carries ONE filter, `camera` (camera.shader): alignment,
key and look in a single pass (every pass on the 3840x1080 canvas costs 3-4 ms here). Before
that it was three: sbs align -> camera key -> camera out (same controls, same names).

usage:
  camera_chain.py status             print the filters and settings on both scenes
  camera_chain.py merge [--dry-run]  one-off: build `camera` from the three old filters'
                                     values and disable those; rollback dump to obs_config/
  camera_chain.py drop-old           remove the three disabled old filters after checking
  camera_chain.py reload             recreate `camera` with its current settings
                                     (re-reads camera.shader — no OBS relaunch needed)

Talks to the main instance (control_bridge/config.yaml → obs_instances.viture).
"""
import asyncio
import json
import math
import os
import shutil
import sys
import time

import simpleobsws
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
CFG = yaml.safe_load(open(os.path.join(REPO, "control_bridge", "config.yaml")))["obs_instances"]["viture"]
URL = f"ws://{CFG.get('host', 'localhost')}:{CFG['port']}"

SCENES = ("stage", "room")
SHADER = {
    "sbs align": os.path.join(REPO, "sbs_align.shader"),
    "camera key": os.path.join(REPO, "camera_key.shader"),
    "camera out": os.path.join(REPO, "camera_out.shader"),
    "camera": os.path.join(REPO, "camera.shader"),
}
NEW_ORDER = ("camera",)
OLD_THREE = ("sbs align", "camera key", "camera out")
SCENE_FILE = os.path.expanduser(
    "~/Library/Application Support/obs-studio/basic/scenes/structure-elegato-viture.json")
BACKUP_DIR = os.path.join(REPO, "obs_config")


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class OBS:
    def __init__(self):
        self.ws = simpleobsws.WebSocketClient(url=URL, password=CFG["password"])

    async def __aenter__(self):
        await self.ws.connect()
        await self.ws.wait_until_identified()
        return self

    async def __aexit__(self, *a):
        await self.ws.disconnect()

    async def call(self, t, d=None):
        r = await self.ws.call(simpleobsws.Request(t, d), timeout=30)
        if not r.ok():
            raise RuntimeError(f"{t} {d}: {r.requestStatus.comment}")
        return r.responseData or {}

    async def filters(self, scene):
        fl = (await self.call("GetSourceFilterList", {"sourceName": scene})).get("filters", [])
        return {f["filterName"]: {"index": f["filterIndex"], "kind": f["filterKind"],
                                   "enabled": f["filterEnabled"], "settings": f["filterSettings"]}
                for f in fl}

    async def nudge_save(self):
        """Filter add/remove over obs-websocket does NOT mark the project changed, so OBS never
        autosaves the new chain (seen 2026-11-14: the collection on disk kept a half-migrated
        state for 7+ min). Creating and removing a scratch scene goes through the UI path and
        saves within a few seconds."""
        await self.call("CreateScene", {"sceneName": "zz_save_nudge"})
        await asyncio.sleep(0.5)
        await self.call("RemoveScene", {"sceneName": "zz_save_nudge"})

    async def create_shader(self, scene, name, settings, enabled):
        st = dict(settings)
        st.update({"from_file": True, "shader_file_name": SHADER[name]})
        st.pop("shader_text", None)
        st.pop("last_error", None)
        await self.call("CreateSourceFilter", {"sourceName": scene, "filterName": name,
                                               "filterKind": "shader_filter", "filterSettings": st})
        await asyncio.sleep(1.0)
        await self.call("SetSourceFilterEnabled", {"sourceName": scene, "filterName": name,
                                                   "filterEnabled": bool(enabled)})
        f = await self.call("GetSourceFilter", {"sourceName": scene, "filterName": name})
        err = f["filterSettings"].get("last_error", "")
        if err:
            raise RuntimeError(f"{scene}/{name}: shader compile error:\n{err}")
        return f


def show(name, f):
    st = {k: v for k, v in f["settings"].items() if k not in ("shader_file_name", "shader_text", "last_error", "from_file")}
    sf = f["settings"].get("shader_file_name", "")
    print(f"  [{f['index']}] {name!r:14} kind={f['kind']} enabled={f['enabled']} {os.path.basename(sf) if sf else ''}")
    print("      " + json.dumps(st, sort_keys=True))


async def cmd_status():
    async with OBS() as o:
        for scene in SCENES:
            print(f"=== {scene}")
            fl = await o.filters(scene)
            for name, f in sorted(fl.items(), key=lambda kv: kv[1]["index"]):
                show(name, f)


async def shot(o, scene):
    import base64
    r = await o.call("GetSourceScreenshot", {"sourceName": scene, "imageFormat": "png",
                                             "imageWidth": 960, "imageHeight": 270})
    return base64.b64decode(r["imageData"].split(",", 1)[1])


async def cmd_merge(dry):
    """One-off (2026-11-18): sbs align + camera key + camera out -> one `camera` filter with the
    same values (key_enable = whether camera key was enabled). Screenshots of the scene before
    and after go to /tmp/camera_merge_<scene>_{before,after}.png for a by-eye check."""
    stamp = time.strftime("%Y-%m-%d")
    async with OBS() as o:
        old_all = {s: await o.filters(s) for s in SCENES}
        if not dry:
            dump = os.path.join(BACKUP_DIR, f"camera_chain_before_{stamp}.json")
            with open(dump, "w") as fh:
                json.dump(old_all, fh, indent=1, sort_keys=True)
            print("rollback dump:", dump)
        for scene in SCENES:
            old = old_all[scene]
            if "camera" in old:
                print(f"{scene}: already merged, skipped")
                continue
            if any(n not in old for n in OLD_THREE):
                print(f"{scene}: expected {OLD_THREE}, found {list(old)}; skipped")
                continue
            st = {}
            for n in OLD_THREE:
                st.update({k: v for k, v in old[n]["settings"].items()
                           if k not in ("shader_file_name", "shader_text", "last_error", "from_file")})
            st["key_enable"] = bool(old["camera key"]["enabled"])
            if not old["sbs align"]["enabled"] or not old["camera out"]["enabled"]:
                print(f"{scene}: NOTE sbs align / camera out was disabled; the merged filter has no such switch")
            print(f"=== {scene}: {json.dumps(st, sort_keys=True)}")
            if dry:
                continue
            open(f"/tmp/camera_merge_{scene}_before.png", "wb").write(await shot(o, scene))
            for n in OLD_THREE:
                await o.call("SetSourceFilterEnabled", {"sourceName": scene, "filterName": n, "filterEnabled": False})
            try:
                f = await o.create_shader(scene, "camera", st, True)
            except Exception:
                for n in OLD_THREE:   # put the old chain back exactly as it was
                    await o.call("SetSourceFilterEnabled", {"sourceName": scene, "filterName": n,
                                                            "filterEnabled": old[n]["enabled"]})
                try:
                    await o.call("RemoveSourceFilter", {"sourceName": scene, "filterName": "camera"})
                except Exception:
                    pass
                raise
            await asyncio.sleep(1.0)
            open(f"/tmp/camera_merge_{scene}_after.png", "wb").write(await shot(o, scene))
            print(f"  created 'camera' at index {f['filterIndex']} (compile ok); old three disabled")
        if not dry:
            await o.nudge_save()


async def cmd_drop_old():
    """After checking the merge by eye: remove the three disabled old filters."""
    async with OBS() as o:
        for scene in SCENES:
            fl = await o.filters(scene)
            if "camera" not in fl:
                print(f"{scene}: not merged, nothing removed")
                continue
            for n in OLD_THREE:
                if n in fl:
                    await o.call("RemoveSourceFilter", {"sourceName": scene, "filterName": n})
                    print(f"{scene}: removed {n!r}")
        await o.nudge_save()


async def cmd_reload():
    async with OBS() as o:
        for scene in SCENES:
            fl = await o.filters(scene)
            for name in NEW_ORDER:
                if name not in fl:
                    print(f"{scene}: no {name!r}, skipped")
                    continue
            for name in NEW_ORDER:
                if name in fl:
                    await o.call("RemoveSourceFilter", {"sourceName": scene, "filterName": name})
            for name in NEW_ORDER:
                if name in fl:
                    f = await o.create_shader(scene, name, fl[name]["settings"], fl[name]["enabled"])
                    print(f"{scene}: reloaded {name!r} at index {f['filterIndex']}")
        await o.nudge_save()


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("status", "merge", "drop-old", "reload"):
        print(__doc__)
        sys.exit(2)
    if args[0] == "status":
        asyncio.run(cmd_status())
    elif args[0] == "merge":
        asyncio.run(cmd_merge("--dry-run" in args))
    elif args[0] == "drop-old":
        asyncio.run(cmd_drop_old())
    else:
        asyncio.run(cmd_reload())


if __name__ == "__main__":
    main()
