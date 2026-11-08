// camera_key — luma key with morphological hole fill, for the stereo-camera layer.
//
// The camera object (statue) is bright on a black background with hard edges,
// but its own deep shadows fall below any luma threshold and the plain luma
// key punches holes in it. This filter keys on luma like OBS's Luma Key, then
// FILLS holes: a pixel that fails the key is kept anyway when foreground is
// found within `fill_radius` px in at least `fill_cover` of 16 directions —
// true for a pocket inside the silhouette, false for open background next to
// an edge (which sees foreground on roughly one side only). The outline is
// therefore still decided by luma; the fill only acts inside it.
//
// Place LAST on the camera chain: the shader filters before it write opaque
// alpha. Works on the full-SBS frame: rays never cross the eye seam, so one
// eye's content cannot fill the other's background.
// Units are px of the surface the filter sits on (camera_chain = canvas px).

uniform float luma_min<
    string label = "Key: luma below this is background";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 0.5;
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

float key_at(float2 uv)
{
    float3 c = image.Sample(textureSampler, uv).rgb;
    return smoothstep(luma_min, luma_min + luma_smooth, luma_of(c));
}

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    float4 src = image.Sample(textureSampler, uv);
    float a = smoothstep(luma_min, luma_min + luma_smooth, luma_of(src.rgb));

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
            return float4(src.rgb, src.a * a);
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
    return float4(src.rgb, src.a * a);
}
