# control_bridge

Bridge daemon that reads DC-coupled CV from the Rebel Technology **OWL-ACDC** USB interface and pushes the values to mapped shader-filter properties in both running OBS instances. Also (planned for v2) bidirectionally mirrors GUI changes between the two instances so a slider tweak in one updates the other automatically.

## Topology

```
   OWL-ACDC (USB, DC-coupled)              OBS #1 (Viture)     OBS #2 (projector)
   4 inputs @ 48 kHz                       ws :4455            ws :4456
        │                                      ▲                   ▲
        └────────► bridge.py ──────────────────┴───────────────────┘
                   (sounddevice + simpleobsws)
```

Two OBS instances run because the Viture (32:9) and projector (16:9) need different canvas aspect ratios. They share the Elgato capture but each owns its own filter chain. Without the bridge, the depth/contrast/mode sliders in instance #1 and instance #2 drift independently. With the bridge:

1. **Linked control via CV**: turning a knob on the synth rack pushes the same value into both instances' matching filter properties.
2. **Linked control via GUI** (v2): moving a slider in one OBS UI mirrors to the other.
3. **CV-back-to-synth** (later): the OWL-ACDC's 4 outputs can be driven from OBS state too — e.g. a "depth pulse" signal sent back to a modulation input.

## First-time setup

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml` if your source/filter/property names differ from the defaults — the example matches the current `structure-elegato-viture` and `structure-elegato-stereo` scene collections.

## Running

Make sure both OBS instances are up (use `../launch_dual.sh` from the parent directory) and obs-websocket is enabled on ports 4455 / 4456. Then:

```bash
source .venv/bin/activate
python3 bridge.py
```

On stdout you'll see:

- "connected" lines for each obs-websocket endpoint that responded
- a one-time dump of the current settings of each mapped filter
- a continuous stream of `CV ch0: +0.342` lines as you move CV inputs

Ctrl-C to exit cleanly.

## Status

- **v1 (current)**: prove plumbing — connect, list properties, stream CV values to the console.
- **v2 (next)**: wire CV → `SetSourceFilterSettings` writes (CV actually controls filter values).
- **v3**: bidirectional GUI mirror via `SourceFilterSettingsChanged` event subscription, with a guard to prevent feedback loops between instances.
- **v4 (eventually)**: output stage — drive the OWL-ACDC's 4 outputs from OBS state, closing the loop back into the synth rack.

## Camera alignment (`camera_align` on a mapping)

Optional block on the splat `depth` mapping (see `config.example.yaml`). While
`depth_sidecar` publishes fresh camera disparity stats (`stats_file`, younger than
`max_age_s`), the bridge places the splat relative to the camera. Screen disparity
(L−R, canvas px): camera = `canvas_scale`·d_src; splat = 2·(dt−c)·f0·`eye_px` with
f0 = depth²·`max_parallax`, dt = bake depth (1 = synth front plane).

| mode | what it does | knob (this channel) |
|---|---|---|
| `behind` (default) | synth **front** plane `gap_px` behind the camera's far plane (p5); convergence solved every pass: c = 1 − canvas_scale·(p5−gap)/(2·f0·eye_px) | splat depth = how far the synth recedes behind that front |
| `front` | same, front plane at an absolute `front_px` (source px; negative = behind screen) | splat depth |
| `match` | synth's 0..1 range spans the camera's p5..p95: f0 = canvas_scale·(p95−p5)/(2·eye_px), c = −p5/(p95−p5) | fraction of that range (0 flat .. 1 exact) |
| `off` | legacy: knob = depth, convergence untouched | depth |

Stale stats behave like `off`. Convergence may legitimately exceed the 0..1 slider
(a front plane behind the screen needs c > 1); it is clamped to −1..2. Switch modes
live — no restart — with `tools/synth_plane.py` (writes `control_file`):

```bash
P=control_bridge/.venv/bin/python3
$P tools/synth_plane.py status        # camera far/near planes, mode, solved splat
$P tools/synth_plane.py behind 2      # statue on top of the synth, 2 px gap (default)
$P tools/synth_plane.py front -20     # synth front plane 20 source px behind the screen
$P tools/synth_plane.py match         # synth occupies the statue's own depth range
$P tools/synth_plane.py off
```

**Convergence offset from the OBS filter panel.** The splat's `convergence` slider
is not overwritten blindly: the bridge polls it ~10×/s and treats any value it did not
write itself as a slider move. The delta is folded into a persistent offset
(`offset_file`, default `/tmp/camera_align_offset.json`) that is added to the solved
convergence in every mode, and the bridge holds its own convergence writes for
`slider_hold_s` (0.8 s) while you drag. So in *behind* mode the slider nudges the
synth's front plane forward/back of the auto-solved position and stays there as the
camera stats move. `tools/synth_plane.py offset 0` resets it; `offset` alone shows it.
(obs-websocket 5.7 emits no `SourceFilterSettingsChanged` for this filter — verified —
hence polling rather than events.)

Log lines are tagged `camera_align:`. The bridge must run with `.venv/bin/python3`
(system python lacks numpy); launch detached with nohup.
