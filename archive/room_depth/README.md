# Room depth (shelved 2026-11-22)

An experiment in mapping the synth directly onto a 3D surface, like texturing: the
synth's colours rendered at the geometry of the physical room. A sidecar process
measured a depth map of the room from the stereo camera and published it into OBS;
the `depth bake` shader blended that map into the synth's own depth field.

It was pulled out of the live chain while shelved. Nothing here runs or is loaded.

## What is in this folder

| File | What it was |
|---|---|
| `depth_sidecar.py` | The sidecar: polls a camera source over obs-websocket, estimates depth (MiDaS / SGBM / fused), publishes it as Syphon server `stereopi-depth`. Also wrote camera disparity stats to `/tmp/stereopi_depth_stats.json`. |
| `eval_depth.py` | Evaluation harness for the depth map. |
| `SIDECAR.md` | The sidecar's original README (engines, costs, calibration). Paths in it predate the move. |
| `rectify.npz` | Camera rectification from `--calibrate` (gitignored; local only). |
| `chromadepth_bake.with_room_depth.shader` | `chromadepth_bake.shader` as it was with the room controls. |

## What was removed from the live rig

- **Shader:** `room_depth` (texture), `room_mix`, `room_mode`, `room_gain` in
  `chromadepth_bake.shader`, and the blend after `field_depth()`:
  crossfade / sculpt (`d * room`) / relief (`max(d, room)`), mixed by `room_mix`.
- **OBS (main):** Syphon input `StereoPi Depth` (kind `syphon-input`, app `Python`,
  server name `stereopi-depth`), which was also an item in scene `camera_test`; and the
  `room_*` settings on `depth bake` (`room_depth_source` = `StereoPi Depth`).
- **Control surface:** the "Room depth (unused)" section of `depth bake` in
  `control_surface/info.yaml`.

The last commit with everything in place is `d8d60df`.

## Bringing it back

1. Restore the shader: copy `chromadepth_bake.with_room_depth.shader` over
   `chromadepth_bake.shader` (or port the two `room` blocks if the bake has moved on),
   and relaunch main so the filter recompiles.
2. Move `depth_sidecar.py`, `eval_depth.py`, `rectify.npz` back to `depth_sidecar/`
   (the venv is still there: `depth_sidecar/.venv`, kept because other tools use its OpenCV).
3. Start the sidecar detached; in OBS add a Syphon Client source named `StereoPi Depth`
   bound to `[Python] stereopi-depth` and pick it as `depth bake`'s room depth source.
4. Restore the `room_*` entries in `control_surface/info.yaml` from `d8d60df`.

Cost warning from when it ran: the fused engine was too expensive for the Mac mini
alongside four OBS instances, and the rig is GPU-bound (see `SIGNAL_CHAIN.md`).
