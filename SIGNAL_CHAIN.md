# Signal chain — status as of 2026-11-07

Source-to-screen map of the live rig. Kept current by hand; when something here
changes, change it here too. Config snapshots live in [`obs_config/`](obs_config/),
launch in [`launch_dual.sh`](launch_dual.sh) (+ [`launch_stereopi.sh`](launch_stereopi.sh)).

```
video synth ──HDMI──► Elgato 4K S ──┐                     OWL-ACDC ◄── rack CV (4 ch)
                                    │                         │  control_bridge (60 Hz)
StereoPi (stereo lens) ─HDMI─► Blackmagic UltraStudio ─┐      ▼
                                    │                  │   shader params
                                    ▼                  ▼
                     MAIN  elegato->full_sbs  (3840×1080, ws :4455, the only shaders)
                       Scene = synth layer (Elgato + chain)  ⊕  camera_chain (screen/keyed)
                                    │ program ─► OBS Virtual Camera
                                    │ Mic/Aux ─► monitor → VITURE glasses (+15 dB, limiter)
          ┌─────────────────────────┼──────────────────────────┐
          ▼                         ▼                          ▼
 full_sbs->twitch (:4456)   full_sbs->youtube (:4457)   full_sbs->projector (:4458)
 letterbox, VT h264 6000    letterbox, VT h264 8000     half-SBS squash, no stream
 → Twitch                   → YouTube RTMPS (custom)    → fullscreen projector on LTV
 Mic/Aux → stream           Mic/Aux → stream            Mic/Aux → monitor → LTV HDMI audio
```

## Inputs

| input | path | notes |
|---|---|---|
| Video synth | HDMI → **Elgato 4K S** (picture + audio) | Every instance's `Mic/Aux` captures the Elgato's CoreAudio device directly (UID-pinned, never "default"). CoreAudio allows all four readers at once. |
| Stereo cameras ×2 | Two StereoPi + stereo lens, 1920×1080 SBS (960/eye) → HDMI → two **Blackmagic UltraStudio Recorder 3G** boxes on the OWC Thunderbolt hub: **stage** = avcapture id `94:14e3c000:…` (hub route 701), **room** = `94:7892aa70:…` (route 501). macOS names both identically; only the id differs. Each box feeds **one** OBS instance at a time (`tools/bmd_handoff.py` moves the stage box). Several minutes from Pi power-on to first frames. The stereo lens puts the raw eyes ~140 source px apart horizontally and ~49 px vertically; that offset scales with zoom. UltraStudio needs a real Thunderbolt cable — a USB-C charge cable leaves it invisible. |
| CV | rack → **OWL-ACDC** USB (4 DC-coupled inputs, ±5 V ↔ ±1.0) → `control_bridge/bridge.py` | ch0 splat depth (wrapped by camera alignment), ch1 bake hue rotation, ch2 palette rotation, ch3 palette hue pull. The video-rack crow (`sieve_dual`) is the usual CV source. |

## Main instance — `elegato->full_sbs` (ws :4455, canvas 3840×1080)

The only instance that runs shaders. Streams nowhere; its program feeds the OBS
Virtual Camera for the relays. Scene `Scene`, bottom to top:

1. **Synth layer** — source `Video Capture Device` (Elgato), stretched to 3840×1080.
   Chain: `test pattern`[off] → `sbs align`[off] → `synth color` → `palette` →
   `depth bake` → `field smooth h` → `field smooth v` → `stereo splat` (native
   plugin) → `stereo soften`[off] → `stereo displace`[off] → `pillar box`.
   Output: full-SBS stereo, one eye per 1920 px half.
2. **Camera layers** — nested scenes **`stage`** (raw `StereoPi stage`) and
   **`room`** (raw `StereoPi room`), each with the camera stretched to
   3840×1080 inside it; room sits below stage in `Scene`. Each scene carries the same
   filter set (room's sbs align is auto-measured with SIFT on the raw pair — re-run after any refocus, it moved the vertical by 23 px: currently left_y +76.7 / right_y −76.7, left_x −75.7 / right_x +75.7, margin 60; `camera key` is OFF on room — a lit room has no black background to key). The room rig is
   mounted upside down: its item inside `room` is scaled −1/−1 (a 180° turn of the
   whole SBS frame, which rights each eye AND swaps the eye halves — a vertical flip
   alone would leave it mirrored with the eyes crossed). Scene filters: `sbs align` (convergence/trim, **canvas px = 2× camera px**;
   slider ±400; **`mask_common`** on: after the shift, each eye is cropped to the
   region BOTH eyes can see — the stereo window — so no pixel is visible to one eye only;
   **`window_margin`** adds the scene's largest off-plane disparity on every horizontal edge,
   because near/far objects still spill out of one eye's frame — room uses 60 px — its depth range after convergence spans about ±48 px, measured p1..p99) → `synth color` (soften 0) → `palette` → `pillar box` (aspect 1.0) →
   `mix opacity` → **`camera key`** ([`camera_key.shader`](camera_key.shader): luma key
   `luma_min` 0.07 / ramp 0.012 — the grading lifts true black to luma 0.063 — plus a
   morphological hole fill: a keyed-out pixel is kept when foreground is found within
   `fill_radius` 40 px in ≥ 80 % of 16 directions, so the statue's deep shadows stay
   solid while open background doesn't; rays never cross the eye seam). Must sit LAST
   because the shader filters before it write opaque alpha. Costs ~8 ms of render time
   on this machine. Blend **normal**, so the keyed statue occludes the synth (was
   *screen* before the key). Depth synthesis is
   off here: the camera already carries real stereo. **Trim sbs align on
   `stage` / `room`, not on the inputs** — the depth sidecar screenshots the raw stage input.

Audio: `Mic/Aux` unmuted, *monitor and output*, monitoring device = system default
(VITURE DisplayPort audio) via filters `Monitor Gain` +15 dB → `Monitor Limiter` −3 dB
(the VITURE has no macOS volume control and the fader tops out at 0 dB).

### Live control
- `control_bridge` writes CV → filter properties every pass. Launch detached with
  nohup and **its `.venv` python** (system python lacks numpy).
- **Camera alignment** (`camera_align` on the ch0 mapping): `depth_sidecar` publishes
  the camera's displayed-disparity percentiles (p5/p50/p95, source px) to
  `/tmp/stereopi_depth_stats.json`; the bridge solves splat `depth`/`convergence`.
  Modes via `tools/synth_plane.py`: **behind** (default: synth front plane `gap_px`
  behind the statue's far plane, knob = how far it recedes), `front PX`, `match`
  (synth spans the statue's depth, knob = fraction), `off`. Stale stats → `off`.
  The splat's **convergence slider** in the OBS filter panel acts as a persistent
  OFFSET on the solved convergence (bridge polls it; `synth_plane.py offset 0` resets).
- `depth bake` can also take the sidecar's depth map (`room_depth`, Syphon source
  `StereoPi Depth`); `room_mix` is 0 and the sidecar runs the cheap stereo-only engine,
  so the depth map is currently unused.
- `tools/sbs_mode.py on|off` switches main's synth chain for SBS *input* (camera on the
  Elgato) — not used in the current topology; harmless.

## Relays (each 1920×1080, source = main's virtual camera, **no shaders**)

| instance | ws | program | output | audio |
|---|---|---|---|---|
| `full_sbs->twitch` (dir `structure-elegato-projector-halfsbs`, collection file `structure-elegato-stereo.json`) | 4456 | `full_sbs` letterbox | Twitch, VideoToolbox h264 CBR 6000, keyint 2 | Mic/Aux unmuted → stream |
| `full_sbs->youtube` | 4457 | `full_sbs` letterbox | YouTube RTMPS (**`rtmp_custom`**, never the built-in YouTube service — CEF dock crashes a 3rd instance), VideoToolbox CBR 8000 | Mic/Aux unmuted → stream |
| `full_sbs->projector` | 4458 | `half_sbs` squash | fullscreen Program projector on the **LTV** display (`projector/projector.py open`) | Mic/Aux unmuted, monitor-only → LTV HDMI audio |

Both relays also carry a `half_sbs` scene as the format switch. The legacy Elgato
source + shader chain was removed from the relays on 2026-11-07: it held ~2 GB per
relay and pushed the 8 GB M2 into swap.

## Daemons (all detached with nohup — Claude Code background tasks die at session end)

| daemon | launch | log |
|---|---|---|
| control bridge | `control_bridge/.venv/bin/python3 control_bridge/bridge.py` | `/tmp/owl_bridge.log` (`camera_align:` lines) |
| depth sidecar (stats-only, stage camera) | `depth_sidecar/.venv/bin/python3 depth_sidecar/depth_sidecar.py --engine sgbm --fps 1.5 --cv-threads 2 --grab-width 1920 --align-source stage --align-source-width 3840` | `/tmp/depth_sidecar.log` |
| YouTube watchdog | `youtube_live/.venv/bin/python3 youtube_live/yt_live.py watch` | `/tmp/yt_live.log` |

## Dormant

- `stereopi->full_sbs` (ws :4459, `launch_stereopi.sh`): the camera alone through the
  chain. Stopped — starting it steals the Blackmagic from main.
- In main: scene `camera_test`, the `Oracle Preview` and `Test Pattern` layers (off).

## Known failure: fullscreen projectors on a display that goes away

If a display carrying a fullscreen projector (VITURE, LTV) is turned off or unplugged,
the OBS instance owning that projector logs `obs_display_init: Failed to create swap
chain` repeatedly and **leaks memory without bound** (2026-11-08: the projector
instance reached 19 GB, swap 21 GB, and main could no longer create GL surfaces —
the Filters dialog preview went blank). Fix: restart that instance
(`projector/projector.py` relaunch or `launch_dual.sh`); swap drains immediately.
Check with `ps -o rss= -p <pid>` when the machine feels wrong.

## Machine

Mac mini **M2, 8 cores, 8 GB RAM**. Four OBS instances + daemons are near the memory
ceiling; keep relays shader-free and prefer VideoToolbox. OBS pinned to 32.1.2.

## Open items

- Camera convergence (`sbs align` on `camera_chain`) is tied to the current zoom;
  auto-converge from the sidecar's median disparity is the natural next step.
- Google OAuth app for `youtube_live` is still in *Testing*: re-run `yt_live.py auth`
  weekly until "Publish app" is enabled.
- The depth sidecar's full (fused) engine is too expensive for this machine while
  four OBS instances run; bring it back only if `room_depth` is used.
