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
