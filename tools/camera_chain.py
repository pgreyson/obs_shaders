#!/usr/bin/env python3
"""camera_chain.py — the camera-layer filter chain on main's `stage` / `room` scenes.

    sbs align  (alignment correctness: per-eye shift + hard common mask)
    camera key (the key and nothing else; its key_* controls condition only the key signal)
    camera out (everything about the look: stereo window + feather, grading, hue
                concentration, opacity)

usage:
  camera_chain.py status             print the chain and settings on both scenes
  camera_chain.py migrate [--dry-run] one-off (2026-11-14): replace the old six-filter chain
                                     (sbs align → synth color → palette → pillar box →
                                     mix opacity → camera key) with the three above, translating
                                     the live values approximately (the user re-trims by eye).
                                     Writes a rollback dump to obs_config/ first.
  camera_chain.py reload             recreate the three filters with their current settings
                                     (re-reads the .shader files — no OBS relaunch needed)

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
}
NEW_ORDER = ("sbs align", "camera key", "camera out")
OLD_FILTERS = ("sbs align", "synth color", "palette", "pillar box", "mix opacity", "camera key")

ALIGN_KEYS = ("left_x", "left_y", "right_x", "right_y", "mask_common", "convergence")
WINDOW_KEYS = ("window_width", "window_shift", "window_offset", "mask_top", "mask_bottom", "vignette")
KEY_DROP = ("out_gain", "out_contrast", "out_brightness")

SCENE_FILE = os.path.expanduser(
    "~/Library/Application Support/obs-studio/basic/scenes/structure-elegato-viture.json")
BACKUP_DIR = os.path.join(REPO, "obs_config")


def g(d, k, default):
    v = d.get(k, default)
    return float(v) if isinstance(v, (int, float)) else v


def translate(old):
    """old: {filterName: {"enabled": bool, "settings": {...}}} → new three filters."""
    al = old.get("sbs align", {}).get("settings", {})
    sc = old.get("synth color", {}).get("settings", {})
    pal = old.get("palette", {}).get("settings", {})
    mix = old.get("mix opacity", {}).get("settings", {})
    key = old.get("camera key", {}).get("settings", {})

    # --- sbs align: shifts + common mask only
    align = {k: al[k] for k in ALIGN_KEYS if k in al}

    # --- camera key: drop the out_* colour controls, add the new neutral ones
    k2 = {k: v for k, v in key.items() if k not in KEY_DROP and k not in ("shader_file_name", "shader_text", "last_error")}
    k2.update({"key_amount": 1.0, "key_gain": 1.0, "key_gamma": 1.0, "key_contrast": 1.0, "key_brightness": 0.0})

    # --- camera out
    # synth color (0..1 sliders, 0.5 neutral) → natural units
    contrast_s = 2.0 ** ((g(sc, "contrast", 0.5) - 0.5) * 2.0)
    gamma_s = 1.0 / (2.0 ** ((0.5 - g(sc, "gamma", 0.5)) * 3.0))
    sat_s = g(sc, "saturation", 0.5) * 2.0
    hue_s = (g(sc, "hue_shift", 0.5) - 0.5) * 360.0
    # OBS color_filter_v2 (0 neutral) → multipliers
    cm = g(mix, "contrast", 0.0)
    contrast_m = cm + 1.0 if cm >= 0 else 1.0 / (1.0 - cm)
    gm = g(mix, "gamma", 0.0)
    gamma_m = 1.0 + gm if gm >= 0 else 1.0 / (1.0 - gm)
    sat_m = g(mix, "saturation", 0.0) + 1.0
    hue_m = g(mix, "hue_shift", 0.0)
    bright_m = g(mix, "brightness", 0.0)
    opacity = g(mix, "opacity", 1.0)
    # camera key's out_* (yesterday's stopgap)
    out_gain = g(key, "out_gain", 1.0)
    out_contrast = g(key, "out_contrast", 1.0)
    out_brightness = g(key, "out_brightness", 0.0)

    hue = hue_s + hue_m
    hue = (hue + 180.0) % 360.0 - 180.0

    out = {
        "soften": g(sc, "soften", 0.0),
        "black_level": g(sc, "black_level", 0.0),
        "gain": clamp(out_gain, 0.0, 2.0),
        "contrast": clamp(contrast_s * contrast_m * out_contrast, 0.0, 3.0),
        "gamma": clamp(gamma_s * gamma_m, 0.2, 4.0),
        "brightness": clamp(bright_m + out_brightness, -1.0, 1.0),
        "saturation": clamp(sat_s * sat_m, 0.0, 3.0),
        "hue_shift": hue,
        "hue_peaks": g(pal, "size", 6.0),
        "hue_pull": g(pal, "hue_pull", 0.0),
        "hue_phase": g(pal, "rotation", 0.0),
        "opacity": clamp(opacity, 0.0, 1.0),
    }
    for k in WINDOW_KEYS:
        if k in al:
            out[k] = al[k]
    # sbs align used to feather from the window ∩ common-mask edges; camera out only knows
    # the window, so when the layer is feathered start the window at the common-mask extents
    # (the hard mask then coincides with the feather's start, as before).
    if float(out.get("vignette", 0.0)) > 0.5:
        lx, rx, ly, ry = (g(al, k, 0.0) for k in ("left_x", "right_x", "left_y", "right_y"))
        out["mask_top"] = max(float(out.get("mask_top", 0.0)), math.ceil(max(0.0, ly, ry)))
        out["mask_bottom"] = max(float(out.get("mask_bottom", 0.0)), math.ceil(max(0.0, -ly, -ry)))
        cx0 = max(0.0, lx, rx); cx1 = 1920.0 + min(0.0, lx, rx)
        if float(out.get("window_width", 0.0)) < 0.5:
            out["window_width"] = math.floor(cx1 - cx0)
            out["window_shift"] = round((cx0 + cx1) / 2.0 - 960.0)
            out["window_offset"] = 0.0
    out = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in out.items()}

    enabled = {
        "sbs align": old.get("sbs align", {}).get("enabled", True),
        "camera key": old.get("camera key", {}).get("enabled", True),
        "camera out": True,
    }
    return {"sbs align": align, "camera key": k2, "camera out": out}, enabled


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


async def cmd_migrate(dry):
    stamp = time.strftime("%Y-%m-%d")
    async with OBS() as o:
        old_all = {s: await o.filters(s) for s in SCENES}
        if not dry:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            dump = os.path.join(BACKUP_DIR, f"camera_chain_before_{stamp}.json")
            with open(dump, "w") as fh:
                json.dump(old_all, fh, indent=1, sort_keys=True)
            if os.path.exists(SCENE_FILE):
                shutil.copy2(SCENE_FILE, os.path.join(BACKUP_DIR, f"structure-elegato-viture.before_camera_chain_{stamp}.json"))
            print("rollback dump:", dump)
        for scene in SCENES:
            old = old_all[scene]
            new, enabled = translate(old)
            print(f"=== {scene}: old chain = {[n for n, _ in sorted(old.items(), key=lambda kv: kv[1]['index'])]}")
            for name in NEW_ORDER:
                print(f"  {name!r:14} enabled={enabled[name]} {json.dumps(new[name], sort_keys=True)}")
            if dry:
                continue
            # remove everything we know about (anything else stays, after the new three)
            for name in sorted(old, key=lambda n: -old[n]["index"]):
                if name in OLD_FILTERS:
                    await o.call("RemoveSourceFilter", {"sourceName": scene, "filterName": name})
            for name in NEW_ORDER:
                f = await o.create_shader(scene, name, new[name], enabled[name])
                print(f"  created {name!r} at index {f['filterIndex']} (compile ok)")
            leftovers = [n for n in await o.filters(scene) if n not in NEW_ORDER]
            if leftovers:
                print(f"  NOTE: other filters still on {scene}: {leftovers}")
        if not dry:
            await o.nudge_save()
            print("project save nudged (scratch scene created + removed)")


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
    if not args or args[0] not in ("status", "migrate", "reload"):
        print(__doc__)
        sys.exit(2)
    if args[0] == "status":
        asyncio.run(cmd_status())
    elif args[0] == "migrate":
        asyncio.run(cmd_migrate("--dry-run" in args))
    else:
        asyncio.run(cmd_reload())


if __name__ == "__main__":
    main()
