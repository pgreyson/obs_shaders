# Depth Shader Design Notes

Companion document to the depth shaders in this directory. These notes
capture the tradeoffs and known phenomena that have shaped the current
implementations, so future tuning stays grounded.

## Two depth signals, two shaders

- **`lumadepth.shader`** — derives depth from perceived brightness.
- **`chromadepth.shader`** — derives depth from hue position on a color wheel.

They are complementary tools. The right one depends on what the source
looks like and which kind of depth perception you want.

---

## The chroma noise problem (low-luma pixels)

A naive "depth from hue" shader is fundamentally compromised by the math
of hue extraction. Hue is computed as a ratio:

```
hue = (component - other_component) / (cmax - cmin)
```

For a near-black pixel like `rgb(0.005, 0.001, 0.008)`, the eye sees pure
black, but the hue calculation divides by tiny numbers and produces an
unstable, noisy "hue" value. Per-frame analog/compression noise becomes a
per-frame *depth* noise on visually-black pixels — and per-eye parallax then
makes those black pixels shimmer and recede in different directions in each
eye. Eye strain in the regions of the frame that should be visually quietest.

The first attempt to fix this (in an earlier `chromadepth.shader`) was a
`chroma_threshold` smoothstep on `delta` (= cmax − cmin). That helped but
didn't fully capture the problem — a dim purple at `rgb(0.1, 0.0, 0.15)`
has a noticeable `delta` of `0.15` yet is still visually dim and
chroma-noise-vulnerable.

## The lumadepth flattening problem

`lumadepth` is robust to this noise — luma is a stable signal even at low
brightness — but it can't distinguish hue. A red and a green of the same
luma get the same depth. Bright vivid scenes with lots of color variety
collapse onto a narrow band of depths. The depth field "flattens" with
respect to color distinctness.

## The combined approach (current `chromadepth.shader`)

The current chromadepth shader combines both signals to get the best of
each:

```
luma         = dot(rgb, [0.299, 0.587, 0.114])   // perceived brightness
saturation   = (cmax - cmin) / cmax              // HSV saturation
confidence   = luma × saturation                 // bright AND vivid → 1; else → 0
chroma_depth = 1 - fract(hue + rotation)         // color wheel → 0..1

depth        = chroma_depth × confidence
```

This gives us:

- **Pure black, pure white, grays, dim colors** → `confidence ≈ 0` → `depth ≈ 0`.
  All such pixels collapse to a uniform "far" depth, eliminating per-pixel
  chroma noise.
- **Bright vivid colors** → full `chroma_depth`, varying with the pixel's
  hue position on the wheel. Distinct colors get distinct depths.
- **Hue rotation** sweeps which hue is "near", same as before. A slow LFO
  on this parameter creates a depth field that migrates through the
  spectrum over time.

A `color_weight` slider crossfades between the two depth modes per-pixel:
- `0.0` — full lumadepth: depth = luma directly.
- `1.0` — full chromadepth: depth = chroma_depth × luma × sat. Default.
- Middle values lerp between them per-pixel — useful when source content
  has mixed regions (some areas vivid, some grayscale) and you want a
  unified depth field rather than picking one mode wholesale.

(The "raw chromadepth without weighting" regime is intentionally not
exposed — it gave terrible chroma-noise artifacts at low luma and wasn't
worth keeping as an option.)

## When to use which

| Source content                                              | Recommended |
|-------------------------------------------------------------|-------------|
| Grayscale / very low color variation                        | `lumadepth` |
| Bright, vivid, colorful (especially video synthesis output) | `chromadepth` |
| Mixed / unsure                                              | Try both, pick by eye |

## Eye-comfort conventions

The two shaders place their zero-disparity (screen) plane differently:

- **`lumadepth` — anchor at 0.85 (configurable).** Only the highest-depth
  pixels (depth ≈ 1) sit at or forward of the screen plane; everything else
  recedes behind. Divergence-only — much gentler than symmetric "things
  popping toward you" depth. The crop is **asymmetric** (`margin = anchor × f0`,
  with a per-pixel binocular gate) so source samples stay in `[0, 1]` for any
  in-range depth, no clamp-stretched edge artifacts.
- **`chromadepth` — black = infinity (anchor fixed at 0).** Black / achromatic /
  dim pixels (confidence ≈ 0) sit at infinity with zero disparity; bright vivid
  pixels pop forward. Because the anchor is pinned to the extreme, a **symmetric
  four-side crop** (`margin = f0` on all sides) is enough to keep both eyes'
  samples in `[0, 1]` — every output pixel has a valid source in both eyes, no
  gate needed. The equal vertical inset preserves the source's aspect ratio as
  depth changes.

Edge handling differs too:

- **`lumadepth`** smooths the depth signal with a **wide 3-tap blur** (4 px
  taps) so abrupt depth transitions become soft gradients.
- **`chromadepth`** instead resolves edges with an **occlusion-consistent
  depth march**: each output pixel takes the nearest source pixel whose own
  disparity projects onto it, so near surfaces occlude far ones identically
  in both eyes and shapes translate rigidly. Disocclusions (backdrop the mono
  source never recorded) render as the black infinity plane — thin monocular
  reveal strips at trailing edges, mirrored between the eyes, as with real
  objects in front of a black void.

## Parameter ranges

| Shader        | `depth` max parallax | Why |
|---------------|---------------------|-----|
| `lumadepth`   | 12% canvas width    | Applies to every pixel; cumulative depth load is high |
| `chromadepth` | 10% of eye view     | Applies selectively (only bright vivid pixels); one-signed (black=infinity) so the full budget is crossed/forward disparity |

If a setting feels too aggressive on the eyes, lower `depth`. If it feels
flat, raise `depth` first and then consider whether `color_weight`
(chromadepth) or `falloff` (lumadepth) wants tuning.

## Future work

- The depth-modulating-by-color-weight idea could be parameterized on a
  curve (e.g. `pow(confidence, gamma)`) for finer control over how
  aggressively low-confidence pixels collapse.
