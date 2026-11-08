#!/usr/bin/env python3
"""synth_plane: place the synth's stereo plane relative to the stereo camera, live.

Writes /tmp/camera_align_ctl.json, which control_bridge's camera_align reads on
every pass (no restart). Disparities are SOURCE px of the camera (960/eye basis);
negative = behind the screen plane, positive = in front.

  synth_plane.py status            camera far/near planes, current mode, resulting splat
  synth_plane.py behind [GAP]      synth FRONT plane GAP px behind the camera's far plane (default 2)
                                   -> the statue sits on top of the synth; knob = how deep it recedes
  synth_plane.py front PX          synth front plane at an absolute disparity PX (e.g. -20)
  synth_plane.py match             synth spans the camera's own depth range; knob = fraction
  synth_plane.py off               knob drives splat depth directly (legacy)
  synth_plane.py offset [VALUE]    show / set the convergence OFFSET (default 0 = reset).
                                   Moving the splat's convergence slider in OBS's filter
                                   panel also changes this offset — the bridge adds it to
                                   the solved convergence instead of overwriting the slider.

Run with: control_bridge/.venv/bin/python3 tools/synth_plane.py <cmd>
"""
import json, os, sys, time

CTL = "/tmp/camera_align_ctl.json"
OFFSET = "/tmp/camera_align_offset.json"
STATS = "/tmp/stereopi_depth_stats.json"
LOG = "/tmp/owl_bridge.log"


def write(ctl: dict):
    tmp = CTL + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(ctl, fh)
    os.replace(tmp, CTL)
    print("control ->", ctl)


def status():
    try:
        st = json.load(open(STATS))
        age = time.time() - st["t"]
        print(f"camera (displayed, source px): far p5 {st['p5']:+.1f}  median {st['p50']:+.1f}  "
              f"near p95 {st['p95']:+.1f}   ({age:.1f} s old{'' if age < 6 else ' — STALE'})")
    except Exception as e:
        print(f"camera stats unavailable ({e}) — is depth_sidecar running?")
    try:
        off = json.load(open(OFFSET)).get("conv_offset", 0.0)
    except Exception:
        off = 0.0
    print(f"convergence offset: {off:+.3f}  (move the splat's convergence slider in OBS, or: synth_plane.py offset 0)")
    ctl = {}
    if os.path.exists(CTL):
        ctl = json.load(open(CTL))
    print("control file:", ctl or "(none — bridge config defaults apply)")
    try:
        lines = [l for l in open(LOG).read().splitlines() if "camera_align:" in l]
        print("bridge:", lines[-1].split("INFO", 1)[-1].strip() if lines else "(no camera_align lines yet)")
    except OSError:
        pass


def main():
    a = sys.argv[1:]
    if not a or a[0] == "status":
        status()
    elif a[0] == "behind":
        write({"mode": "behind", "gap_px": float(a[1]) if len(a) > 1 else 2.0})
    elif a[0] == "front":
        write({"mode": "front", "front_px": float(a[1])})
    elif a[0] == "match":
        write({"mode": "match"})
    elif a[0] == "off":
        write({"mode": "off"})
    elif a[0] == "offset":
        if len(a) > 1:
            tmp = OFFSET + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"conv_offset": float(a[1]), "t": time.time()}, fh)
            os.replace(tmp, OFFSET)
            print("convergence offset ->", float(a[1]))
        else:
            status()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
