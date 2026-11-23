// camera — the whole stereo-camera layer in ONE pass (2026-11-18).
//
// This is sbs_align + camera_key + camera_out merged, stage for stage, with the
// same controls under the same names. Why one file: every shader pass on the
// 3840x1080 canvas costs 3-4 ms on this machine whatever it computes, and the
// rig was at 45 ms a frame against a 33 ms budget. Three passes per camera
// became one. The three source files stay in the repo as the reference for
// each stage (and for the synth chain's own "sbs align").
//
//   1. ALIGN  per-eye x/y shift + convergence, hard mask to the region both
//             eyes can see. Stereo correctness only.
//   2. KEY    luma / chroma key with hole fill. Decides what is kept; never
//             changes colour. `key_enable` off = no key (was: filter disabled).
//   3. LOOK   stereo window + feather, grading, hue concentration, opacity.
//             Alpha from the key is only ever multiplied here.
//
// Units are px of the surface the filter sits on (the camera's nested scene =
// canvas px, 1920 per eye, 2x camera px). Displayed disparity is left - right,
// + = in front of the screen. Every control at its default is a passthrough.

// ==== 1. ALIGN ============================================================
uniform float left_x<
    string label = "Left eye X offset (px)";
    string widget_type = "slider";
    float minimum = -400.0;
    float maximum = 400.0;
    float step = 0.25;
> = 0.0;

uniform float left_y<
    string label = "Left eye Y offset (px)";
    string widget_type = "slider";
    float minimum = -400.0;
    float maximum = 400.0;
    float step = 0.25;
> = 0.0;

uniform float right_x<
    string label = "Right eye X offset (px)";
    string widget_type = "slider";
    float minimum = -400.0;
    float maximum = 400.0;
    float step = 0.25;
> = 0.0;

uniform float right_y<
    string label = "Right eye Y offset (px)";
    string widget_type = "slider";
    float minimum = -400.0;
    float maximum = 400.0;
    float step = 0.25;
> = 0.0;

// CONVERGENCE: one control for where the subject sits in depth. Adds +c/2 to
// the left eye's x shift and -c/2 to the right eye's, on top of the per-eye
// trims above, so displayed disparity (left - right, px of this surface)
// changes by exactly +c. Positive brings the scene toward the viewer,
// negative pushes it back. tools/stereo_field.py converge sets this from a
// measurement so a chosen part of the subject lands on the screen plane.
uniform float convergence<
    string label = "Convergence (px)";
    string widget_type = "slider";
    float minimum = -300.0;
    float maximum = 300.0;
    float step = 0.5;
> = 0.0;

// After shifting, each eye has content the other eye lacks (the strip that
// slid in from off-frame on one side, and the area revealed on the other).
// Those pixels have no counterpart and fight fusion, so mask them in BOTH
// eyes: only the region visible to both survives. Hard edge; camera_out's
// window + vignette normally sit inside it.
uniform bool mask_common<
    string label = "Mask to common";
> = true;

// ==== 2. KEY ==============================================================
// Off = the whole aligned frame is kept (a lit room has no black background
// to key). The other key controls keep their values while it is off.
uniform bool key_enable<
    string label = "Key enabled";
> = true;

uniform float key_amount<
    string label = "Key amount";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 1.0;

uniform float key_source<
    string label = "Key source";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 2.0;
    float step = 1.0;
> = 0.0;

uniform float key_gain<
    string label = "Key gain";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 4.0;
    float step = 0.01;
> = 1.0;

uniform float key_gamma<
    string label = "Key gamma";
    string widget_type = "slider";
    float minimum = 0.2;
    float maximum = 4.0;
    float step = 0.01;
> = 1.0;

uniform float key_contrast<
    string label = "Key contrast";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 4.0;
    float step = 0.01;
> = 1.0;

uniform float key_brightness<
    string label = "Key brightness";
    string widget_type = "slider";
    float minimum = -1.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float luma_min<
    string label = "Key threshold";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.001;
> = 0.07;

uniform float luma_smooth<
    string label = "Key: edge ramp width";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 0.2;
    float step = 0.001;
> = 0.012;

uniform float fill_radius<
    string label = "Fill radius (px)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 120.0;
    float step = 1.0;
> = 40.0;

uniform float fill_cover<
    string label = "Fill cover";
    string widget_type = "slider";
    float minimum = 0.5;
    float maximum = 1.0;
    float step = 0.01;
> = 0.8;

uniform float fill_steps<
    string label = "Fill steps";
    string widget_type = "slider";
    float minimum = 2.0;
    float maximum = 24.0;
    float step = 1.0;
> = 10.0;

// ==== 3. LOOK =============================================================
// ---- 0. bypass -----------------------------------------------------------
// Shows the signal exactly as it arrives from the key: no window, feather,
// grading, hue work or opacity. For checking what the stage is being given.
uniform bool bypass<
    string label = "Bypass";
> = false;

// ---- 1. stereo window --------------------------------------------------
uniform float window_width<
    string label = "Window width (px)";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 1920.0; float step = 1.0;
> = 0.0;
uniform float window_shift<
    string label = "Window shift (px)";
    string widget_type = "slider";
    float minimum = -600.0; float maximum = 600.0; float step = 1.0;
> = 0.0;
uniform float window_offset<
    string label = "Window L/R offset (px)";
    string widget_type = "slider";
    float minimum = -400.0; float maximum = 400.0; float step = 1.0;
> = 0.0;
uniform float mask_top<
    string label = "Crop top (px)";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 400.0; float step = 1.0;
> = 0.0;
uniform float mask_bottom<
    string label = "Crop bottom (px)";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 400.0; float step = 1.0;
> = 0.0;
uniform float vignette<
    string label = "Edge feather (px)";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 400.0; float step = 1.0;
> = 0.0;

// ---- 2. grading (1.0 / 0 = neutral) -------------------------------------
uniform float soften<
    string label = "Soften";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 1.0; float step = 0.01;
> = 0.0;
uniform float black_level<
    string label = "Black level crush";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 1.0; float step = 0.01;
> = 0.0;
uniform float gain<
    string label = "Gain";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 2.0; float step = 0.01;
> = 1.0;
uniform float contrast<
    string label = "Contrast";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 3.0; float step = 0.01;
> = 1.0;
uniform float gamma<
    string label = "Gamma";
    string widget_type = "slider";
    float minimum = 0.2; float maximum = 4.0; float step = 0.01;
> = 1.0;
uniform float brightness<
    string label = "Brightness";
    string widget_type = "slider";
    float minimum = -1.0; float maximum = 1.0; float step = 0.01;
> = 0.0;
uniform float saturation<
    string label = "Saturation";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 3.0; float step = 0.01;
> = 1.0;
uniform float hue_shift<
    string label = "Hue shift";
    string widget_type = "slider";
    float minimum = -180.0; float maximum = 180.0; float step = 0.5;
> = 0.0;

// ---- 3. hue concentration (palette_quantize, hue only) --------------------
uniform float hue_peaks<
    string label = "Hue peaks N";
    string widget_type = "slider";
    float minimum = 1.0; float maximum = 16.0; float step = 0.1;
> = 6.0;
uniform float hue_pull<
    string label = "Hue pull";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 1.0; float step = 0.01;
> = 0.0;
uniform float hue_phase<
    string label = "Hue peak phase";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 1.0; float step = 0.01;
> = 0.0;

// ---- 4. opacity ----------------------------------------------------------
uniform float opacity<
    string label = "Opacity";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 1.0; float step = 0.01;
> = 1.0;

#define LUMA_W float3(0.299, 0.587, 0.114)
#define TAU 6.2831853

// ---- stage 1: the camera frame after alignment (was sbs_align's output) ----
float4 aligned(float2 uv)
{
    float is_left = step(uv.x, 0.5);
    float lx = left_x + 0.5 * convergence;     // effective per-eye x shifts
    float rx = right_x - 0.5 * convergence;
    float2 off = float2(lerp(rx, lx, is_left),
                        lerp(right_y, left_y, is_left));

    if (mask_common) {
        // position within this eye, in px of the eye (half the surface wide)
        float2 eye_px = float2(uv_size.x * 0.5, uv_size.y);
        float2 p = float2((uv.x - (1.0 - is_left) * 0.5) * 2.0, uv.y) * eye_px;
        // after a shift of +d, an eye shows content on [max(0,d), W+min(0,d)];
        // the common window is the intersection over both eyes
        float x0 = max(max(0.0, lx), max(0.0, rx));
        float x1 = eye_px.x + min(min(0.0, lx), min(0.0, rx));
        float y0 = max(max(0.0, left_y), max(0.0, right_y));
        float y1 = eye_px.y + min(min(0.0, left_y), min(0.0, right_y));
        if (p.x < x0 || p.x > x1 || p.y < y0 || p.y > y1)
            return float4(0.0, 0.0, 0.0, 0.0);   // only one eye sees this: transparent
    }

    // shift content by +off pixels = sample -off pixels upstream
    float2 suv = uv - off / uv_size;

    // this eye's horizontal span
    float xmin = lerp(0.5, 0.0, is_left);
    float xmax = lerp(1.0, 0.5, is_left);

    // revealed area (no source content for this eye) -> transparent
    if (suv.x < xmin || suv.x > xmax || suv.y < 0.0 || suv.y > 1.0)
        return float4(0.0, 0.0, 0.0, 0.0);

    // keep samples half a texel inside the eye half: no wrap, no bleed
    float2 half_px = 0.5 / uv_size;
    suv.x = clamp(suv.x, xmin + half_px.x, xmax - half_px.x);
    suv.y = clamp(suv.y, half_px.y, 1.0 - half_px.y);

    return image.Sample(textureSampler, suv);
}

// ---- stage 2: the key (was camera_key) -----------------------------------
float luma_of(float3 c) { return dot(c, float3(0.2126, 0.7152, 0.0722)); }
float chroma_of(float3 c) { return max(c.r, max(c.g, c.b)) - min(c.r, min(c.g, c.b)); }

// The quantity the key thresholds. The conditioning here touches ONLY this
// measurement; the colour that reaches the look stage is untouched.
float key_value(float3 c)
{
    c = c * key_gain;
    float ge = 1.0 / max(key_gamma, 0.01);
    c = pow(max(c, 0.0), float3(ge, ge, ge));
    c = (c - 0.5) * key_contrast + 0.5 + key_brightness;
    c = clamp(c, 0.0, 1.0);
    float l = luma_of(c);
    float k = chroma_of(c);
    if (key_source < 0.5) return l;
    if (key_source < 1.5) return k;
    return max(l, k);
}

float key_at(float2 uv)
{
    return smoothstep(luma_min, luma_min + luma_smooth, key_value(aligned(uv).rgb));
}

float4 keyed(float2 uv)
{
    float4 src = aligned(uv);
    if (!key_enable || key_amount < 0.001 || src.a <= 0.0)
        return src;

    float a = smoothstep(luma_min, luma_min + luma_smooth, key_value(src.rgb));

    if (a < 0.5 && fill_radius > 0.5) {
        // this eye's horizontal span (never let rays cross the seam)
        float is_left = step(uv.x, 0.5);
        float xmin = lerp(0.5, 0.0, is_left) + 0.5 / uv_size.x;
        float xmax = lerp(1.0, 0.5, is_left) - 0.5 / uv_size.x;
        float2 px = 1.0 / uv_size;
        // cheap pre-test: 8 taps at the full radius. Open background far from the
        // object sees nothing and exits here (most of the frame); a pocket inside
        // the silhouette sees foreground on several sides.
        float pre = 0.0;
        for (float q = 0.0; q < 8.0; q += 1.0) {
            float qa = q * 0.78539816;  // 2*pi/8
            float2 p = uv + float2(cos(qa), sin(qa)) * fill_radius * px;
            p.x = clamp(p.x, xmin, xmax); p.y = clamp(p.y, 0.0, 1.0);
            pre += step(0.5, key_at(p));
        }
        if (pre < 3.0)
            return float4(src.rgb, src.a * lerp(1.0, a, key_amount));
        float hits = 0.0;
        // 16 directions around the compass (float counters: GLSL has no int*float)
        for (float d = 0.0; d < 16.0; d += 1.0) {
            float ang = d * 0.39269908;  // 2*pi/16
            float2 dir = float2(cos(ang), sin(ang));
            float hit = 0.0;
            for (float s = 1.0; s <= 24.0; s += 1.0) {
                if (s > fill_steps) break;
                float r = fill_radius * s / fill_steps;
                float2 p = uv + dir * r * px;
                if (p.x < xmin || p.x > xmax || p.y < 0.0 || p.y > 1.0) break;
                if (key_at(p) >= 0.5) { hit = 1.0; break; }
            }
            hits += hit;
        }
        if (hits / 16.0 >= fill_cover)
            a = 1.0;   // inside the silhouette: hard fill
    }
    return float4(src.rgb, src.a * lerp(1.0, a, key_amount));
}

// ---- stage 3: the look (was camera_out) ----------------------------------
// 4 bilinear corner taps at +-r: a cheap disc blur of the aligned colour (the
// key never changes colour, so this is what camera_out blurred).
float3 blur4(float2 uv, float r) {
    float2 o = r * uv_pixel_interval;
    return 0.25 * (aligned(uv + float2(-o.x, -o.y)).rgb
                 + aligned(uv + float2( o.x, -o.y)).rgb
                 + aligned(uv + float2(-o.x,  o.y)).rgb
                 + aligned(uv + float2( o.x,  o.y)).rgb);
}

float glsl_mod(float x, float y) { return x - y * floor(x / y); }

// Max-channel HSV hue — same formula as palette_quantize / chromadepth_bake.
float hue_of(float3 c, float delta, float cmax) {
    float h = 0.0;
    if (delta > 0.001) {
        if (cmax == c.r)
            h = glsl_mod((c.g - c.b) / delta, 6.0);
        else if (cmax == c.g)
            h = (c.b - c.r) / delta + 2.0;
        else
            h = (c.r - c.g) / delta + 4.0;
        h /= 6.0;
    }
    return h;
}

float3 hsv2rgb3(float h, float s, float v) {
    float r = clamp(abs(h * 6.0 - 3.0) - 1.0, 0.0, 1.0);
    float g = clamp(2.0 - abs(h * 6.0 - 2.0), 0.0, 1.0);
    float b = clamp(2.0 - abs(h * 6.0 - 4.0), 0.0, 1.0);
    return v * lerp(float3(1.0, 1.0, 1.0), float3(r, g, b), s);
}

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    if (bypass)
        return keyed(uv);
    float is_left = step(uv.x, 0.5);

    // ---- 1. stereo window + feather (alpha only)
    float fade = 1.0;
    if (window_width > 0.5 || mask_top > 0.5 || mask_bottom > 0.5 || vignette > 0.5) {
        float2 eye_px = float2(uv_size.x * 0.5, uv_size.y);
        float2 p = float2((uv.x - (1.0 - is_left) * 0.5) * 2.0, uv.y) * eye_px;
        float x0 = 0.0;
        float x1 = eye_px.x;
        if (window_width > 0.5) {
            float c = eye_px.x * 0.5 + window_shift + (is_left > 0.5 ? 0.5 : -0.5) * window_offset;
            x0 = c - window_width * 0.5;
            x1 = c + window_width * 0.5;
        }
        float y0 = mask_top;
        float y1 = eye_px.y - mask_bottom;
        if (p.x < x0 || p.x > x1 || p.y < y0 || p.y > y1)
            return float4(0.0, 0.0, 0.0, 0.0);   // outside the window: transparent
        if (vignette > 0.5) {
            float v = vignette;
            fade = clamp((p.x - x0) / v, 0.0, 1.0) * clamp((x1 - p.x) / v, 0.0, 1.0)
                 * clamp((p.y - y0) / v, 0.0, 1.0) * clamp((y1 - p.y) / v, 0.0, 1.0);
            fade = fade * fade * (3.0 - 2.0 * fade);   // smoothstep
        }
    }

    float4 src = keyed(uv);
    if (src.a <= 0.0)
        return float4(0.0, 0.0, 0.0, 0.0);
    float3 c = src.rgb;

    // ---- 2. grading
    if (soften > 0.001) {
        float3 b = blur4(uv, 0.5 + 1.5 * soften);
        c = lerp(c, b, soften);
    }
    if (black_level > 0.001) {
        float bl = black_level * 0.3;
        c = max(c - bl, 0.0) / (1.0 - bl);
    }
    c = c * gain;
    c = (c - 0.5) * contrast + 0.5;
    c = clamp(c, 0.0, 1.0);
    if (abs(gamma - 1.0) > 0.001) {
        float ge = 1.0 / max(gamma, 0.01);
        c = pow(c, float3(ge, ge, ge));
    }
    c = clamp(c + brightness, 0.0, 1.0);
    if (abs(saturation - 1.0) > 0.001) {
        float y = dot(c, LUMA_W);
        c = clamp(y + (c - y) * saturation, 0.0, 1.0);
    }
    float ang = hue_shift * (TAU / 360.0);
    if (abs(ang) > 0.0001) {
        // rotate around the luma axis (YIQ), as synth_color does
        float i = dot(c, float3(0.596, -0.274, -0.322));
        float q = dot(c, float3(0.211, -0.523, 0.312));
        float y2 = dot(c, LUMA_W);
        float ca = cos(ang);
        float sa = sin(ang);
        float i2 = i * ca - q * sa;
        float q2 = i * sa + q * ca;
        c = float3(y2 + 0.956 * i2 + 0.621 * q2,
                   y2 - 0.272 * i2 - 0.647 * q2,
                   y2 - 1.106 * i2 + 1.703 * q2);
        c = clamp(c, 0.0, 1.0);
    }

    // ---- 3. hue concentration: iterated flow up a von Mises comb (see
    // palette_quantize.shader for the model), then a rigid phase rotation.
    bool phase_active = (hue_phase > 0.001 && hue_phase < 0.999);
    if (hue_pull > 0.001 || phase_active) {
        float cmax = max(c.r, max(c.g, c.b));
        float cmin = min(c.r, min(c.g, c.b));
        float delta = cmax - cmin;
        float h = hue_of(c, delta, cmax);
        float s = delta / max(cmax, 1e-5);
        float v = cmax;
        if (hue_pull > 0.001) {
            float n = max(hue_peaks, 1e-3);
            #define HUE_STEP { float th_ = TAU * n * h; h = h - hue_pull * sin(th_) / (TAU * n); }
            HUE_STEP
            HUE_STEP
            HUE_STEP
            // the flow concentrates but never fully converges: the top of the
            // range blends in a nearest-peak snap with a narrow AA band
            float snap_amt = smoothstep(0.75, 1.0, hue_pull);
            if (snap_amt > 0.001) {
                float hn = h * n;
                float cell = floor(hn);
                float u = hn - cell;
                float aa = 0.04;
                float hs = (cell + smoothstep(0.5 - aa, 0.5 + aa, u)) / n;
                h = lerp(h, hs, snap_amt);
            }
        }
        h = frac(h + hue_phase);
        c = clamp(hsv2rgb3(h, s, v), 0.0, 1.0);
    }
    // ---- 4. opacity (alpha from the key is preserved, only scaled)
    return float4(c, src.a * fade * opacity);
}
