#!/usr/bin/env python3
"""synth_merge.py — one-off (2026-11-18): replace `synth color` + `palette` on main's synth
source with ONE filter `synth color` running synth_color_palette.shader (same controls).

  synth_merge.py merge      build the merged filter from the live values at the old position;
                            the two old filters stay, disabled, as `synth color OLD` / `palette`
  synth_merge.py drop-old   remove the two disabled old filters
  synth_merge.py reload     recreate the merged filter (re-reads the shader file)

The control bridge's palette targets must point at filter "synth color" afterwards.
"""
import asyncio, base64, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from camera_chain import OBS, REPO, BACKUP_DIR

SRC = "Video Capture Device"
NEW, OLD_A, OLD_A_PARKED, OLD_B = "synth color", "synth color", "synth color OLD", "palette"
FILE = os.path.join(REPO, "synth_color_palette.shader")
SKIP = ("shader_file_name", "shader_text", "last_error", "from_file")
PALETTE_KEYS = ("size", "rotation", "hue_pull", "skew", "luma_peaks", "luma_pull", "black_floor",
                "chroma_pull", "chroma_target")


async def create(o, settings, index, enabled=True):
    st = dict(settings, from_file=True, shader_file_name=FILE)
    await o.call("CreateSourceFilter", {"sourceName": SRC, "filterName": NEW,
                                        "filterKind": "shader_filter", "filterSettings": st})
    await o.call("SetSourceFilterEnabled", {"sourceName": SRC, "filterName": NEW, "filterEnabled": False})
    await o.call("SetSourceFilterIndex", {"sourceName": SRC, "filterName": NEW, "filterIndex": index})
    await asyncio.sleep(1.0)
    f = await o.call("GetSourceFilter", {"sourceName": SRC, "filterName": NEW})
    err = f["filterSettings"].get("last_error", "")
    if err:
        await o.call("RemoveSourceFilter", {"sourceName": SRC, "filterName": NEW})
        raise RuntimeError("shader compile error:\n" + err)
    if enabled:
        await o.call("SetSourceFilterEnabled", {"sourceName": SRC, "filterName": NEW, "filterEnabled": True})


async def shot(o, path):
    r = await o.call("GetSourceScreenshot", {"sourceName": SRC, "imageFormat": "png",
                                             "imageWidth": 960, "imageHeight": 270})
    open(path, "wb").write(base64.b64decode(r["imageData"].split(",", 1)[1]))


async def merge():
    async with OBS() as o:
        fl = await o.filters(SRC)
        if OLD_A_PARKED in fl:
            print("already merged"); return
        a, b = fl[OLD_A], fl[OLD_B]
        if not a["settings"].get("shader_file_name", "").endswith("synth_color.shader"):
            print("synth color is not the old shader; nothing done"); return
        json.dump({k: fl[k] for k in (OLD_A, OLD_B)},
                  open(os.path.join(BACKUP_DIR, f"synth_color_palette_before_{time.strftime('%Y-%m-%d')}.json"), "w"),
                  indent=1, sort_keys=True)
        st = {k: v for k, v in a["settings"].items() if k not in SKIP}
        st.update({k: v for k, v in b["settings"].items() if k in PALETTE_KEYS})
        if not b["enabled"]:
            st.update(hue_pull=0.0, luma_pull=0.0, chroma_pull=0.0, rotation=0.0)
        await shot(o, "/tmp/synth_merge_before.png")
        await o.call("SetSourceFilterName", {"sourceName": SRC, "filterName": OLD_A, "newFilterName": OLD_A_PARKED})
        try:
            await create(o, st, a["index"], enabled=False)
        except Exception:
            await o.call("SetSourceFilterName", {"sourceName": SRC, "filterName": OLD_A_PARKED, "newFilterName": OLD_A})
            raise
        await asyncio.gather(*[o.call("SetSourceFilterEnabled", {"sourceName": SRC, "filterName": n, "filterEnabled": e})
                               for n, e in ((OLD_A_PARKED, False), (OLD_B, False), (NEW, a["enabled"]))])
        await asyncio.sleep(1.0)
        await shot(o, "/tmp/synth_merge_after.png")
        print("merged:", json.dumps(st, sort_keys=True))
        await o.nudge_save()


async def drop_old():
    async with OBS() as o:
        fl = await o.filters(SRC)
        if OLD_A_PARKED not in fl:
            print("not merged; nothing removed"); return
        for n in (OLD_A_PARKED, OLD_B):
            if n in fl:
                await o.call("RemoveSourceFilter", {"sourceName": SRC, "filterName": n}); print("removed", n)
        await o.nudge_save()


async def reload():
    async with OBS() as o:
        f = (await o.filters(SRC))[NEW]
        st = {k: v for k, v in f["settings"].items() if k not in SKIP}
        await o.call("RemoveSourceFilter", {"sourceName": SRC, "filterName": NEW})
        await create(o, st, f["index"], f["enabled"])
        await o.nudge_save()


if __name__ == "__main__":
    cmd = {"merge": merge, "drop-old": drop_old, "reload": reload}.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if not cmd:
        print(__doc__); sys.exit(2)
    asyncio.run(cmd())
