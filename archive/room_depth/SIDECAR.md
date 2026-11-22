# depth_sidecar

Live depth map of the StereoPi camera feed, published into OBS as the
Syphon source "stereopi-depth" (OBS source **StereoPi Depth**, bound by
server name `[Python] stereopi-depth`; rebind via the source's `uuid`
setting if it ever goes stale).

```
.venv/bin/python3 depth_sidecar.py                     # fused engine, defaults
nohup .venv/bin/python3 depth_sidecar.py --engine sgbm --fps 1.5 --cv-threads 2 \
      --grab-width 1920 --align-source stage --align-source-width 3840 \
      > /tmp/depth_sidecar.log 2>&1 &                  # detached; stats-only duty (cheap)
# fused engine (full depth map for room_depth) costs far more CPU: add --engine fused --fps 3
.venv/bin/python3 depth_sidecar.py --calibrate --grab-width 1920
.venv/bin/python3 eval_depth.py "label"                # objective metrics
```

## Camera-aligned splat (`--stats-file`, default on)

Every frame the sidecar also publishes the camera's **displayed** disparity
percentiles (p5/p50/p95, source px on the 960-per-eye basis, EMA ≈1 s) to
`/tmp/stereopi_depth_stats.json`. "Displayed" = what the viewer sees: rectified
SGBM disparities mapped back through the inverse homographies to the raw frames,
minus the live `sbs align` shift (re-read every second from `--align-source`,
default `--source`; in main that is the `stage` scene, whose filter units are
canvas px — pass `--align-source-width 3840`). `control_bridge`'s
`camera_align` mapping option turns these into splat depth/convergence so the
synth occupies the same screen-disparity range as the camera — zoom the camera
and the splat follows. The sidecar must see the RAW camera input (no filters on
`StereoPi AV`), or the calibration no longer matches.

**CPU:** at 1920 grab, SGBM+WLS on all cores plus the DPT model saturate an M2 that is
also running four OBS instances (load avg 28). For alignment-stats duty use
`--engine sgbm --fps 1.5 --cv-threads 2`; the depth map is then holey but `room_mix`
is 0 anyway. Bring `fused` back only when the bake actually uses `room_depth`.

## Architecture

OBS owns the Blackmagic capture (its camera extension streams to OBS but
NOT to CLI processes — verified; a plain AVFoundation session gets zero
frames while the Elgato works fine). The sidecar polls `StereoPi AV`
screenshots over obs-websocket, computes depth, Syphons it back. Depth
and image therefore carry equal latency. ~3 fps at quality settings;
for more rate, pin OBS's virtual camera to the camera scene and capture
that instead (readable from CLI), or lower `--grab-width`.

## Engines (`--engine`)

- **fused** (default): DPT_SwinV2_T_256 mono structure, anchored to SGBM
  stereo measurements — robust linear fit on stereo-confident pixels
  (EMA-smoothed coefficients) plus an edge-diffused local residual
  (heavily damped). Mono supplies coverage, stereo supplies truth.
- **midas**: mono only (`--model` MiDaS_small | DPT_SwinV2_T_256 | DPT_Hybrid;
  DPT models require `timm==0.6.12` — pinned in the venv).
- **sgbm**: true stereo only. Needs `--calibrate` first. Honest but holey
  on textureless/dim scenes.

## Calibration (`--calibrate`)

Self-rectification from SIFT matches: fundamental matrix →
`stereoRectifyUncalibrated` homographies, cached in `rectify.npz` with
the matched features' rectified disparity range (the SGBM search window
is derived from it — uncalibrated rectification shifts the zero point,
so disparities straddle zero; never assume `[0, N)`). Re-run whenever
the cameras are physically disturbed, on a bright/textured scene.
Result on this rig: median |dy| 48.8px → 0.31px.

## Post chain (main loop)

EMA (engine output) → CLAHE'd + temporally-smoothed guide →
edge-guided upsample (guidedFilter r=3) → **black-infinity gate**
(near-black image pixels forced smoothly to the far plane — their depth
is unrecoverable and this matches the rig's chromadepth convention) →
post-EMA → vertical flip (Syphon consumers assume GL bottom-left
origin) → Metal texture.

## Hard-won gotchas

- **cv2.ximgproc.guidedFilter: eps must match the guide's scale.** A
  uint8 guide (0–255) with eps≈1e-4 produces ~260K NaN px/frame as
  axis-aligned rectangles (box-filter smear), and any NaN that reaches
  an EMA accumulator is immortal. Guides here are float32 in [0,1].
  NaN counters log + scrub as a tripwire if it ever regresses.
- Swin models tile featureless regions into per-window constant blocks;
  the black-infinity gate makes this moot.
- The eval harness refuses to run when the sidecar is down — OBS keeps
  rendering a dead server's last frame, which silently measures as a
  frozen (perfect-temporal) image.
- `DS_DEBUG=1` dumps per-stage PNGs to /tmp/ds_debug every 20 frames.
