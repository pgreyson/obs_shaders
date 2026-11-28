# OBS Shaders

> **Current live topology, source to screen: [`SIGNAL_CHAIN.md`](SIGNAL_CHAIN.md).**

Stereoscopic video-synthesis shaders for OBS, run via the [obs-shaderfilter](https://github.com/exeldro/obs-shaderfilter) plugin. Companion to [structure_tools](../structure_tools), which holds the GLES 2.0 shaders that run on the Erogenous Tones Structure module itself. These OBS shaders do the same kinds of work — mono→stereo conversion, per-eye stereo effects — but in software, around a mono-native video synth, with no Structure-hardware constraints (full HD instead of 320/640 SD).

## Why a separate OBS chain

Structure stereo-izes a mono input and runs stereo feedback onboard, but it can't send a luma signal back into the synth to close an external feedback loop. Moving the work into OBS lets us split it across two cooperating OBS instances:

```
                 mono synth out
                       │
        ┌──────────────┴──────────────┐
        ▼                              ▼
   OBS #1 (pre-synth)            OBS #2 (post-synth / display)
   affine transform +           mono → stereo (stereo_displace)
   effects                          + stereo effects (stereo_zoom,
        │                            stereo_feedback_depth)
        ▼                              │
   luma back into synth ───loop        ▼
                                   half-SBS → Viture / 3D projector
```

- **OBS #1** transforms the synth's output and feeds it back into the synth as a luma signal — an external feedback loop the synth couldn't close on its own.
- **OBS #2** takes the (now externally-fed-back) mono output and handles all stereoization and stereo effects, emitting half-SBS for the display chain (see [structure_tools/monitoring](../structure_tools/monitoring) for the Viture/projector side).

OBS doesn't support general-purpose node routing like Structure, so shaders here are a flat collection of source-filter effects rather than a gen/fx/mix hierarchy.

## Shaders

All shaders are obs-shaderfilter `.shader` files. Add them as a **User-defined shader** filter on a source (right-click source → Filters → Add → User-defined shader → Load shader text from file).

| Shader | Input | Output | Purpose |
|--------|-------|--------|---------|
| `palette_quantize.shader` | mono | mono | **Continuous probabilistic palette.** A smooth hue likelihood (von Mises comb / circular softmax) with N peaks that colors flow toward — no quantization. Runs BETWEEN `synth color` and `depth bake`, so because color is depth here, the N hue peaks become N soft depth strata. Knobs: `size` (peak count N, continuous), `rotation` (peak phase), `hue_pull` (softmax temperature), `skew` (peak asymmetry), plus a matching `luma_pull`/`chroma_pull`. Defaults = passthrough. See [`SHADERS.md`](SHADERS.md). |
| `chromadepth.shader` | mono | half-SBS | Stereo from hue + luma×saturation weighting. Low-confidence (dim or achromatic) pixels collapse to far; bright vivid colors get wide depth spread. Three knobs: `depth`, `hue_rotation`, `color_weight`. See [`SHADERS.md`](SHADERS.md). |
| `lumadepth.shader` | mono | half-SBS | Stereo from luma. Robust on grayscale or low-color content but flattens distinctions in vivid scenes. Three knobs: `depth`, `luma_anchor`, `falloff`. See [`SHADERS.md`](SHADERS.md). |
| `stereo_displace.shader` | mono | half-SBS | Color-based parallax (luma or chromadepth). Mono→stereo. |
| `stereo_displace_b.shader` | mono | half-SBS | Same + narrow 3-tap depth blur (1px) to reduce edge aliasing. |
| `stereo_displace_bw.shader` | mono | half-SBS | Same + wide depth blur (4px) for stronger smoothing. |
| `sbs_align.shader` | SBS | SBS + alpha | Per-eye x/y shift (convergence + vertical trim) and the hard common-region mask (`mask_common`). Alignment correctness only — the stereo window and feather moved to `camera_out.shader` (2026-11-14). First on the camera chain. |
| `stereo_window.shader` | full SBS | full SBS | (currently disabled) Program-wide stereo window: identical black bands in both eyes for all layers (`tools/stereo_window.py auto`). Last filter on main's `Scene`. |
| `camera_key.shader` | SBS camera | SBS camera + alpha | The key and nothing else (second on the camera chain, after `sbs align`). `key_source` luma / chroma / max; `key_gain` / `key_gamma` / `key_contrast` / `key_brightness` condition only the signal the key measures, never the output colour; `luma_min` threshold, `luma_smooth` ramp; morphological hole fill (16-ray search within `fill_radius`, never crosses the eye seam); `key_amount` 0 = unkeyed mix. |
| `camera_out.shader` | SBS camera + alpha | SBS camera + alpha | Output stage of the camera chain (last): stereo window (`window_width` / `window_shift` / `window_offset` / `mask_top` / `mask_bottom`) with `vignette` feather, then soften → black level → gain → contrast → gamma → brightness → saturation → hue shift → hue concentration (`hue_peaks` / `hue_pull` / `hue_phase`) → `opacity`; `bypass` checkbox shows the incoming (keyed) signal with none of it. Natural units, defaults = passthrough; only multiplies the key's alpha. |
| `display_zero.shader` | program scene | full SBS | Global stereo offset for the whole program (`zero_px`): makes 0 on every other stereo control the Viture's relaxed plane. `tools/stereo_field.py zero N [--keep]`. |
| `plugin/` (`stereo splat`) | bake output | full SBS | Native forward splat. Controls in canvas px (1920/eye, + = in front of screen): `plane_px`, `thickness_px` (CV ch0), `anchor`, `vignette_pct` (depth vignette), `frame_inset_px`; displayed disparity of depth dt = plane + thickness·(dt − anchor). `tools/stereo_field.py status\|converge\|snap\|set` measures and places synth and cameras on one scale. |
| `stereo_zoom.shader` | half-SBS | half-SBS | Per-eye centered zoom. Apply after a mono→stereo shader. |
| `stereo_feedback_depth.shader` | mono | half-SBS | **(Planned)** Parallax encodes feedback recency, decoupled from color. Color-displace optional via parameter. |

Typical OBS #2 filter chain on the synth-capture source:

```
stereo_displace  →  stereo_zoom
(mono → half-SBS)   (per-eye zoom)
```

## OBS config snapshots (`obs_config/`)

Sanitized copies of the OBS profiles and scene collections (Twitch/auth sections stripped).

**The two displays have incompatible aspect ratios** — Viture is 32:9 (3840×1080), 3D projector is 16:9 (1920×1080) — and OBS's Fullscreen Projector preserves the canvas aspect ratio when scaling to a display. So each display needs a canvas matching its aspect, which means each output target needs its own profile *and* its own scene collection (because the source's bounding-box transform is stored per scene collection).

| File | Mirrors | Purpose |
|------|---------|---------|
| `obs_config/profile-viture.ini` | `…/profiles/structure-elegato-viture/basic.ini` | Viture display profile. Base **3840×1080**, Output 3840×1080. |
| `obs_config/scene-viture.json` | `…/scenes/structure-elegato-viture.json` | Paired with Viture profile. Elgato source uses **Stretch to bounds 3840×1080** so each half-SBS eye fills 1920px on the 3840 canvas. Filter stack: `halfsbs_pillarbox` + `stereo_displace_bw` (chromadepth). Top layers of `Scene`: nested scenes `stage` and `room` (= raw `StereoPi stage` / `StereoPi room` with `sbs align` → `camera key` → `camera out` as *scene* filters; `tools/camera_chain.py status`) blended **screen** over the synth output. The chain lives on the scene, not the input, so `depth_sidecar` can screenshot the raw camera. **Adjust `sbs align` on `camera_chain`**, not on the input. Camera must be handed to main (`tools/bmd_handoff.py to-main`). |
| `obs_config/profile-projector.ini` | `…/profiles/structure-elegato-projector-halfsbs/basic.ini` | Twitch relay profile (ws :4456; display name `full_sbs->twitch`, renamed from `full_sbs->stream` 2026-11-07). Base **1920×1080**, **VideoToolbox h264 CBR 6000** since 2026-11-07 (x264 ate 80–150 % CPU on the 8 GB M2). |
| `obs_config/scene-stereo.json` | `…/scenes/structure-elegato-stereo.json` | Twitch relay collection `full_sbs->twitch` (was `full_sbs->stream`): ONLY the virtual-camera source + `half_sbs`/`full_sbs` scenes. The legacy Elgato source + shader chain was removed 2026-11-07 — it cost ~1 GB per relay and pushed the 8 GB machine into swap. Relays stay shader-free. |
| `obs_config/profile-youtube.ini` | `…/profiles/full_sbs-youtube/basic.ini` | YouTube relay profile (ws :4457). Base **1920×1080**, VideoToolbox h264 CBR 8000. Service must stay `rtmp_custom` → YouTube RTMPS ingest (see `youtube_live/README.md`). Auth/token lines stripped. |
| `obs_config/scene-youtube.json` | `…/scenes/full_sbs-youtube.json` | Clone of the Twitch relay collection: `full_sbs` (letterbox, program) / `half_sbs` (squash) over the main instance's virtual camera. Legacy Elgato chain removed 2026-11-07 (memory). |
| `obs_config/profile-halfsbs-projector.ini` | `…/profiles/full_sbs-projector/basic.ini` | 4th instance (ws :4458) for the 3D projector. Base **1920×1080**, no streaming. `[Audio] MonitoringDeviceId` = the projector's HDMI audio (set with `projector/projector.py audio <name>`). |
| `obs_config/profile-stereopi.ini` | `…/profiles/stereopi-full_sbs/basic.ini` | Optional 5th instance (ws :4459) for the StereoPi camera. Base **3840×1080** like main, no streaming, no monitoring. Launch with `launch_stereopi.sh`, which also runs `tools/bmd_handoff.py to-stereopi`: the Blackmagic camera extension feeds **one** OBS instance at a time, so the camera is released in main and re-selected here (`to-main` reverses it). |
| `obs_config/scene-stereopi.json` | `…/scenes/stereopi-full_sbs.json` | Source `StereoPi AV` (Blackmagic UltraStudio Recorder 3G, 1920×1080 SBS from the Pi) stretched to the 3840×1080 canvas, carrying the main chain in **SBS mode** (as `tools/sbs_mode.py on`: depth bake / field smooth / splat / stereo soften off, `sbs align` on, synth color `soften` 0, pillar box `aspect_correction` 1.0). `StereoPi Depth` syphon source included for `room_depth` binding. |
| `obs_config/scene-halfsbs-projector.json` | `…/scenes/full_sbs-projector.json` | Clone of the relay collection with program = `half_sbs` (squash) and Mic/Aux unmuted, *monitor only* → sound goes to the projector, nowhere else. `projector/projector.py open` fullscreens the program on the projector display. |

Pairings:

- **Viture (full-SBS):** `viture_half_to_full_sbs` profile + `structure-elegato-viture` scene collection.
- **Projector (half-SBS):** `projector_halfsbs` profile + `structure-elegato-stereo` scene collection.

Switching display target = two clicks (Profile menu + Scene Collection menu). Profile Output (Scaled) Resolution affects encoder output only; the OBS preview and Fullscreen Projector render at the canvas (Base) resolution.

Paths inside the scene JSONs reference `/Users/paulgreyson/Dev/...` directly (matches how `structure_tools/monitoring/obs-scene.json` does it). Restoring on another machine would require path substitution.

## obs-shaderfilter template

obs-shaderfilter uses an HLSL dialect (not the GLES 2.0 that Structure uses). Key differences from the Structure template:

| Structure (GLES 2.0) | obs-shaderfilter (HLSL) |
|----------------------|--------------------------|
| `vec2` / `vec3` / `vec4` | `float2` / `float3` / `float4` |
| `varying vec2 tcoord;` | `v_in.uv` (param of `mainImage`) |
| `texture2D(tex, uv)` | `image.Sample(textureSampler, uv)` |
| `uniform vec2 tres;` (texel size via `1.0/tres.x`) | `uv_pixel_interval.x` (built-in) |
| `uniform vec4 fparams;` + `//f0:label:` | `uniform float foo< ... >` annotation blocks |
| `ftime` (0–1 ramp), `itime` | `elapsed_time` (seconds, built-in) |
| entry: `void main()` → `gl_FragColor` | entry: `float4 mainImage(VertData v_in) : TARGET` |

Parameters are declared as annotated uniforms that obs-shaderfilter renders as UI controls:

```hlsl
uniform float depth<
    string label = "Depth";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    return image.Sample(textureSampler, uv);
}
```

Built-in uniforms available (no declaration needed): `image`, `textureSampler`, `uv_size` (pixels), `uv_pixel_interval` (1/uv_size), `elapsed_time`.

See `../structure_tools/monitoring/halfsbs_pillarbox.shader` for a working reference in this format.

## Install obs-shaderfilter

```bash
gh release download 2.6.0 --repo exeldro/obs-shaderfilter --pattern "*macos-arm64.pkg" --dir /tmp
open /tmp/obs-shaderfilter-2.6.0-macos-arm64.pkg
# click through installer, restart OBS
```

## Dual OBS instances and the control bridge

The Viture (32:9) and 3D projector (16:9) displays have incompatible aspect ratios, so each needs its own profile + scene collection + OBS instance (see [`obs_config/`](obs_config/)). The two instances share the Elgato capture device but otherwise run independently.

To launch both pinned to the right profile/scene + obs-websocket port:

```bash
./launch_dual.sh
```

That brings up four OBS processes: main `elegato->full_sbs` on ws :4455, Twitch relay `full_sbs->twitch` on :4456, YouTube relay `full_sbs->youtube` on :4457, and the half-SBS projector relay `full_sbs->projector` on :4458 (see [`projector/`](projector/)). It also starts `tools/audio_watchdog.py` detached: it watches every instance's `Mic/Aux` meters and re-opens the Elgato audio capture if it silently stalls (log `/tmp/audio_watchdog.log`). Two detached watchdogs start with it: `tools/audio_watchdog.py` (re-arms a silently stalled Elgato audio capture) and `tools/pi_watchdog.py` (reboots a StereoPi whose camera feed froze, after confirming the stall on the Pi); logs in `/tmp/audio_watchdog.log` and `/tmp/pi_watchdog.log`.

**One-time setup** before running the script for the first time: enable obs-websocket in OBS (Tools → WebSocket Server Settings → "Enable WebSocket server"). This flips `server_enabled` to `true` in `~/Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json` — the config is shared by both instances, but each binds its own port via the `--websocket_port` launch flag. Note the auto-generated password there; the control bridge needs it.

## Touch control surface (`control_surface/`)

One web page with every filter parameter of main, for an iPad on the same WiFi:
`http://<mac address>:8080/` (also `http://Pauls-Mac-mini.local:8080/`). Started by
`launch_dual.sh`; by hand:
`nohup control_bridge/.venv/bin/python3 control_surface/server.py > /tmp/control_surface.log 2>&1 &`

**Live view:** `http://<mac address>:8080/view` shows the program picture at up to 30 fps with
well under a second of delay (JPEG frames, quality 75 by default and adjustable per device with the overlay's *Stream quality* row, sent over one WebSocket from a relay instance, so it costs main nothing;
frames are grabbed only while a viewer is open). Full screen, both eyes, nothing over the picture; a hairline round each eye picture, as on a stereo card; held in portrait the page is not stereo: one eye's picture across the top as a monitor and the full control page under it; on a phone the picture is 70 % of the screen width for a phone stereo viewer (full width on bigger screens; the overlay's *Picture width %* row changes it per device); *Eye gap %* leaves a gap between the two eye pictures (each eye gets narrower by half of it) and *Keep eye aspect* chooses whether the narrower eyes stay 16:9 or are squeezed; double tap anywhere to bring the stereo control overlay forward or hide it. LAN only:
the surface has no login, so reach it from outside through a VPN (e.g. Tailscale), never a port forward.

**Stereo overlay:** double tap anywhere in the viewer (with the overlay up, anywhere except its three buttons). It draws one narrow, translucent
list of named controls hard against the left edge of each eye's picture, so it fuses into a single
panel in a phone stereo viewer. Its depth is the *Overlay depth* row at the top of the list (one collapsible tree: tab > filter >
section > control; set it through the viewer; it starts at the rig's global zero). The list is a picker
wheel: the row on the centre line is the highlighted one. Sections start collapsed. The overlay
hides for as long as the right thumb is down (except on *Overlay depth*). The list is drawn in both eyes at the far left. Touch goes by screen half: on the left a drag anywhere scrolls the list and taps do nothing; on the right a drag anywhere is the slider
for the highlighted control, a single tap acts on it (opens a section, flips a switch, next option) and a double tap hides the overlay. Switches are not on the slider: a right-hand tap flips them. The header shows the viewer's frame rate, and the last row of the list, *Full controls*, opens the full control page (followed by *Full screen* where the browser supports it). A single tap does nothing; the old bottom bar is gone. Faint until used.

**Legacy filters:** stages and tabs marked `legacy: true` in `control_surface/info.yaml` (replaced
filters kept off for rollback, the retired renderers, the test pattern and the Test bench tab) are
hidden. The control page's **Legacy** pill shows them again; the stereo overlay never lists them.

**Record:** the control page's header has a **Record** pill, and the stereo overlay a *Record* row
near the end of its list (middle button flips it; a red dot shows by the frame rate while it runs).
Both start and stop recording on OBS main with main's own recording settings and folder, and show
the free disk space. The server refuses to start, and stops a running recording, below
`REC_MIN_FREE_GB` (5 GB) free.

**HTTPS:** the same pages are on `https://Pauls-Mac-mini.local:8443/` (and the IP), for phones with
Safari's HTTPS-only setting and for Home Screen icons. The certificate comes from a local authority
in `control_surface/certs/` (gitignored; server certificate valid 397 days, names: the `.local` host
and 192.168.50.181, so reissue it if the Mac's address changes and you use the IP). Each device
trusts it once: open `http://<mac>:8080/ca.crt` in Safari, Allow, then Settings > General > VPN &
Device Management > install the profile, then Settings > General > About > Certificate Trust
Settings > switch on "Rig Control Local CA".

- The page is built from the LIVE filter list (every filter, enabled or not) and the slider
  ranges from the shader files, so a new uniform or filter appears after "Reload".
- `control_surface/info.yaml` holds the grouping (tabs, stages, sections), short labels and the
  text of the info overlays. Keep it in step with the shaders; anything it does not describe
  still shows, with the shader's own label and comment.
- Values moved by the CV bridge or the OBS filter panel are read back while a page is open
  (obs-websocket sends no event for filter settings). CV-driven controls carry a "CV n" tag.
- CV-driven controls are RELATIVE: setting one on the surface tells the bridge (UDP
  127.0.0.1:9456), which keeps an offset so the knob's present position stands for the new
  value and the knob moves it from there (`value = remap(cv) + offset`, clamped to the
  mapping's `limits` in `control_bridge/config.yaml`; offsets persist in
  `control_bridge/.cv_offsets.json`). "Clear knob offset" in the control's info overlay
  returns it to the plain `out_range`.
- Changes are saved into the scene collection a few seconds after the last touch.
- No login: anyone on the network can move the controls.

## Stereo regression suite (`tools/convergence_cases.py`)

Every stereo defect that has been seen is kept as a focused test case, so a fix for one
does not quietly break another.

    depth_sidecar/.venv/bin/python3 tools/convergence_cases.py

It renders `test_patterns/cases.png` (one panel per defect) and any real frames in
`test_patterns/fixtures/` off-air through the depth bake and splat with fixed settings,
measures each case, and writes a side-by-side sheet to `/tmp/convergence_cases.png`.
Cases are `fixed` (must pass; a failure is a regression, exit code 1) or `open` (known,
not solved yet).

The rule: when a defect shows up, add the case first, see it fail, then fix, then run the
whole suite. If the defect only shows on real material, save the raw frame and the filter
settings as a fixture (`<name>.png` + `<name>.json`). The on-air `test pattern` filter
shows the same picture as Pattern 4 (Cases). `tools/convergence_test.py` is the older
single-pattern tracker (shift maps for one setting).

## Analog control via the OWL-ACDC

The Rebel Technology **OWL-ACDC** is a DC-coupled USB audio interface (4 in, 4 out, 48 kHz). With it patched into the synth rack, modular CV can drive OBS shader parameters in real time, the same way it already drives Structure's onboard shaders.

The CV-to-OBS bridge lives at [`control_bridge/`](control_bridge/). It reads CV from the OWL-ACDC, connects to both obs-websocket endpoints, and pushes the values into mapped shader-filter properties so both instances stay locked together. It also bidirectionally mirrors GUI slider changes between the two instances (planned, v2). See [`control_bridge/README.md`](control_bridge/README.md) for setup and current implementation status.

## License

TBD.

## Continuous archive (`tools/stream_archive.py`)

A fifth OBS instance, `full_sbs->archive` (ws :4460, `OBS Archive.app`, bundle id
`com.obsproject.obs-studio.archive`), shows main's virtual camera at 3840x1080 in one scene and only
records: HEVC (VideoToolbox hardware) CBR 15 Mbit/s, hybrid MP4, a new file every 60 min, into
`~/Movies/archive_capture`. Main is untouched: its Record button is still ProRes (to
`/Volumes/Workspace/obs_recordings`) and can run at the same time.

The detached daemon `tools/stream_archive.py` (started by `launch_dual.sh`, log
`/tmp/stream_archive.log`) keeps that recording running, moves each finished hour to
`/Volumes/Archive/obs_archive/YYYY-MM/` (copy, size check, then delete locally) and deletes archived
files older than 30 days. If the share is away the files wait locally and the recording stops only
when the local disk falls below 6 GB. 6.8 GB/h, 162 GB/day, 4.9 TB for the 30 days.

It warns when the Archive volume is 50 % full, the share is unmounted or the recording is off: in
the log and as a pill in the control surface's status bar ("Archive NN%" when all is well).
`tools/relaunch_archive.sh` restarts only this instance. The first launch of the clone asks for
microphone and camera access on the mini's screen; until both are allowed the instance sits at
"OBS is not ready".

### File shares after a restart (`tools/mount_shares.sh`)

Main records ProRes to `/Volumes/Workspace/obs_recordings` and the archive lives on
`/Volumes/Archive`; both are shares on the Mac Studio and macOS does not remount them after a
restart. `tools/mount_shares.sh` mounts both with the keychain password, retrying for 10
minutes. The login agent `~/Library/LaunchAgents/com.obs-shaders.shares.plist` (copy in
`tools/`) and `launch_dual.sh` run it; log `/tmp/mount_shares.log`. If the Studio's file
sharing does not answer (port 445 timing out, seen 2026-11-27), toggle File Sharing on the Studio.

## Sidecar iPad after a restart (`tools/sidecar/`)

macOS does not reconnect a Sidecar iPad after a restart. `tools/sidecar/sidecar` (built from
`sidecar.m`; it calls the private SidecarCore framework the Displays menu uses) does
`list`, `connect [NAME] [--wait SECONDS]` and `disconnect`. The login agent
`~/Library/LaunchAgents/com.obs-shaders.sidecar.plist` (copy in `tools/sidecar/`) runs
`sidecar connect --wait 900` at login, retrying every 10 s for 15 minutes; its output is in
`/tmp/sidecar_connect.log`. The iPad must be awake, unlocked and near the mini.

## Memory guard (`tools/mem_guard.py`)

The mini has 8 GB and an OBS instance that leaks takes the whole rig down (seen: the projector
instance at 20 GB, main at 12 GB). The guard reads every instance's memory footprint once a
minute, logs all of them every 10 minutes to `/tmp/mem_guard.log`, and shows a red
"Memory: main 12 GB" pill on the control surface above 3 GB. It also reports any other process that large (the Blackmagic camera extension reached 16 GB). It restarts the projector
instance by itself above 3 GB (it streams and records nothing); the others are only reported.
`launch_dual.sh` starts it.

## Playing the archive back (`/recordings` on the control surface)

The **Recordings** pill on the control page opens a list of every archive file (those still on the
mini and those on the Archive volume), grouped by day. Tapping an hour plays it in the page: scrub
bar, 1x to 16x speed, previous/next hour, one-eye or full side-by-side picture, and it runs on into
the next hour by itself. The clock shown is the wall-clock time of the frame on screen. Files are
served with byte ranges by a small HTTP server inside `server.py` on the page port + 10 (8090 plain,
8453 TLS), restricted to archive file names inside the two archive folders. Main's ProRes
recordings on Workspace are listed too (dashed, tagged ProRes) as markers: tapping one plays the
archive from that session's start time (15 Mbit/s instead of 265). Only sessions older than the
archive are played from the ProRes file itself. In landscape the player uses the streams' layout
(gap and hairline frames as set by `tools/stream_gap.py`).
