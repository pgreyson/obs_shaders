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
| `sbs_align.shader` | SBS | SBS + alpha | Per-eye x/y shift (convergence + vertical trim); stereo window as `window_width` / `window_shift` / `window_offset` (+ top/bottom); `vignette` feathers the edges to transparent. First on the camera chain. |
| `stereo_window.shader` | full SBS | full SBS | (currently disabled) Program-wide stereo window: identical black bands in both eyes for all layers (`tools/stereo_window.py auto`). Last filter on main's `Scene`. |
| `camera_key.shader` | SBS camera | SBS camera + alpha | Luma key with morphological hole fill for the stereo-camera layer (`camera_chain` in main). Keys on graded luma, fills shadow pockets inside the silhouette (16-ray search within `fill_radius`), never crosses the eye seam. Sits last in the chain. |
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
| `obs_config/scene-viture.json` | `…/scenes/structure-elegato-viture.json` | Paired with Viture profile. Elgato source uses **Stretch to bounds 3840×1080** so each half-SBS eye fills 1920px on the 3840 canvas. Filter stack: `halfsbs_pillarbox` + `stereo_displace_bw` (chromadepth). Top layers of `Scene`: nested scenes `stage` and `room` (= raw `StereoPi stage` / `StereoPi room` + the SBS-mode chain, `mix opacity` and `camera key` as *scene* filters) blended **screen** over the synth output. The chain lives on the scene, not the input, so `depth_sidecar` can screenshot the raw camera. **Adjust `sbs align` on `camera_chain`**, not on the input. Camera must be handed to main (`tools/bmd_handoff.py to-main`). |
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

That brings up four OBS processes: main `elegato->full_sbs` on ws :4455, Twitch relay `full_sbs->twitch` on :4456, YouTube relay `full_sbs->youtube` on :4457, and the half-SBS projector relay `full_sbs->projector` on :4458 (see [`projector/`](projector/)).

**One-time setup** before running the script for the first time: enable obs-websocket in OBS (Tools → WebSocket Server Settings → "Enable WebSocket server"). This flips `server_enabled` to `true` in `~/Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json` — the config is shared by both instances, but each binds its own port via the `--websocket_port` launch flag. Note the auto-generated password there; the control bridge needs it.

## Analog control via the OWL-ACDC

The Rebel Technology **OWL-ACDC** is a DC-coupled USB audio interface (4 in, 4 out, 48 kHz). With it patched into the synth rack, modular CV can drive OBS shader parameters in real time, the same way it already drives Structure's onboard shaders.

The CV-to-OBS bridge lives at [`control_bridge/`](control_bridge/). It reads CV from the OWL-ACDC, connects to both obs-websocket endpoints, and pushes the values into mapped shader-filter properties so both instances stay locked together. It also bidirectionally mirrors GUI slider changes between the two instances (planned, v2). See [`control_bridge/README.md`](control_bridge/README.md) for setup and current implementation status.

## License

TBD.
