// camera_key — luma/chroma key with morphological hole fill, for the stereo-camera layer.
//
// The camera object (statue) is bright on a black background with hard edges,
// but its own deep shadows fall below any luma threshold and the plain luma
// key punches holes in it. This filter keys like OBS's Luma Key, then FILLS
// holes: a pixel that fails the key is kept anyway when foreground is found
// within `fill_radius` px in at least `fill_cover` of 16 directions — true for
// a pocket inside the silhouette, false for open background next to an edge
// (which sees foreground on roughly one side only). The outline is therefore
// still decided by the key; the fill only acts inside it.
//
// THIS FILTER OWNS THE KEY AND NOTHING ELSE. Everything about how the picture
// LOOKS (window, feather, colour, brightness, opacity) lives in camera_out.
// The `key_*` conditioning controls (gain / gamma / contrast / brightness)
// are applied to a private copy of the pixel that only the key measures, so a
// too-dark source can be boosted to get a hard key while the colour that
// leaves this filter is exactly the colour that entered it.
//
// Chain: sbs align → camera key → camera out. Alpha arriving here (sbs
// align's common-region mask) is preserved and multiplied by the key.
// Works on the full-SBS frame: rays never cross the eye seam, so one eye's
// content cannot fill the other's background. Units are px of the surface
// the filter sits on (camera_chain = canvas px).

uniform float key_amount<
    string label = "Key amount (0 = no key / plain mix, 1 = full key)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 1.0;

uniform float key_source<
    string label = "Key on: 0 = luma, 1 = chroma (saturation), 2 = max of both";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 2.0;
    float step = 1.0;
> = 0.0;

uniform float key_gain<
    string label = "Key input: gain (key signal only, not the picture)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 4.0;
    float step = 0.01;
> = 1.0;

uniform float key_gamma<
    string label = "Key input: gamma (>1 lifts shadows into the key)";
    string widget_type = "slider";
    float minimum = 0.2;
    float maximum = 4.0;
    float step = 0.01;
> = 1.0;

uniform float key_contrast<
    string label = "Key input: contrast about mid grey";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 4.0;
    float step = 0.01;
> = 1.0;

uniform float key_brightness<
    string label = "Key input: brightness offset";
    string widget_type = "slider";
    float minimum = -1.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float luma_min<
    string label = "Key: threshold (key signal below this is background)";
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
    string label = "Fill: max hole half-width (px)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 120.0;
    float step = 1.0;
> = 40.0;

uniform float fill_cover<
    string label = "Fill: fraction of directions that must hit foreground";
    string widget_type = "slider";
    float minimum = 0.5;
    float maximum = 1.0;
    float step = 0.01;
> = 0.8;

uniform float fill_steps<
    string label = "Fill: samples per ray (quality vs GPU)";
    string widget_type = "slider";
    float minimum = 2.0;
    float maximum = 24.0;
    float step = 1.0;
> = 10.0;

float luma_of(float3 c) { return dot(c, float3(0.2126, 0.7152, 0.0722)); }
float chroma_of(float3 c) { return max(c.r, max(c.g, c.b)) - min(c.r, min(c.g, c.b)); }

// The quantity the key thresholds. The conditioning here touches ONLY this
// measurement; the pixel that leaves the filter is untouched.
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
    float3 c = image.Sample(textureSampler, uv).rgb;
    return smoothstep(luma_min, luma_min + luma_smooth, key_value(c));
}

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    float4 src = image.Sample(textureSampler, uv);
    if (key_amount < 0.001)
        return src;                                  // bypass: plain mix

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
