// camera_out — the OUTPUT STAGE of the stereo-camera layer.
//
// Chain: sbs align (alignment correctness) → camera key (the key, nothing
// else) → camera out (everything about how the layer LOOKS). Nothing in here
// can change what the key decided: alpha arriving from the key is preserved
// and only multiplied by the window feather and `opacity`.
//
// Order of operations:
//   1. stereo WINDOW (moved here from sbs_align 2026-11-14): the same width in
//      both eyes, moved as a pair by window_shift, slid apart by window_offset
//      (left window right by offset/2, right window left by offset/2: + = the
//      window floats in front of the screen, − = behind), top/bottom crops,
//      and `vignette` px of feather to TRANSPARENT at the window edges so a
//      narrower field of view blends into the layer beneath.
//   2. soften → black level → gain → contrast → gamma → brightness →
//      saturation → hue shift (synth_color's grading, in natural units)
//   3. hue concentration (the hue-only part of palette_quantize: N peaks the
//      colours flow toward, pull strength, phase)
//   4. opacity
// `bypass` skips all of it and passes the key's output straight through.
// Every control at its default is an exact passthrough. Units are px of the
// surface the filter sits on (camera_chain = canvas px, 2x camera px).

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

float glsl_mod(float x, float y) { return x - y * floor(x / y); }

// 4 bilinear corner taps at ±r = a cheap, alias-free disc blur (synth_color).
float3 blur4(float2 uv, float r) {
    float2 o = r * uv_pixel_interval;
    return 0.25 * (image.Sample(textureSampler, uv + float2(-o.x, -o.y)).rgb
                 + image.Sample(textureSampler, uv + float2( o.x, -o.y)).rgb
                 + image.Sample(textureSampler, uv + float2(-o.x,  o.y)).rgb
                 + image.Sample(textureSampler, uv + float2( o.x,  o.y)).rgb);
}

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
        return image.Sample(textureSampler, uv);
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

    float4 src = image.Sample(textureSampler, uv);
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
