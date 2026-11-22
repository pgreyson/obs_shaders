/*
"Stereo Splat" video filter — the oracle's forward splat + z-buffer,
executed natively. Replaces the fragment-shader inverse march
(chromadepth_march.shader), which could only approximate it.

Input  : the depth-bake output (rgb = original color, alpha = field
         depth encoded a = 0.5 + d/2, optionally Gaussian-smoothed by
         the "field smooth h/v" filters upstream).
Output : half-SBS L|R, each eye eye_w = width/2, composed in one
         depth-tested texrender. Backdrop (reveals) = black = infinity.

Geometry (mode "splat"): a static grid of W*SS subsample points per
output row (SS = 4, mirroring stereo_oracle.py's splat density). The
vertex shader fetches the bake texel, decodes depth, applies the edge
taper, computes the destination bin with the oracle's exact arithmetic,
and emits the point at that bin's pixel center. GS_LEQUAL depth test +
ascending-x draw order reproduce the oracle's stable-sort tie-break
(equal depth -> largest source x wins).

Geometry (mode "warp"): one triangle strip per output row over source
pixel centers. Connected geometry: disocclusions STRETCH the surface
between near and far content instead of opening backdrop gaps — the
candidate convention fix for black-pepper/zipper artifacts on textured
analog content. Same depth test resolves folds (near wins).

All constants mirror tools/stereo_oracle.py — change both together.
*/

#include <obs-module.h>
#include <graphics/graphics.h>
#include <plugin-support.h>
#include <math.h>

#define PIC_LO 0.010f
#define PIC_HI 0.985f
#define MAX_PARALLAX 0.08f /* legacy depth^2 curve: settings migration only */
/* The field controls are in CANVAS px at 1920 per eye (the unit of the
   camera layers' sbs align / camera out sliders), whatever the source
   resolution: px / FIELD_EYE_PX = fraction of the eye width. */
#define FIELD_EYE_PX 1920.0f

struct stereo_splat {
	obs_source_t *context;
	gs_effect_t *effect;

	gs_eparam_t *p_image;
	gs_eparam_t *p_dims;
	gs_eparam_t *p_eye_sign;
	gs_eparam_t *p_eye_base;
	gs_eparam_t *p_eye_w;
	gs_eparam_t *p_f0;
	gs_eparam_t *p_vis_lo;
	gs_eparam_t *p_span;
	gs_eparam_t *p_window_on;
	gs_eparam_t *p_fill_px;
	gs_eparam_t *p_mlaa_on;
	gs_eparam_t *p_convergence; /* effect param "shift0" */
	gs_eparam_t *p_inset;
	gs_eparam_t *p_anchor_d;
	gs_eparam_t *p_vig_w;
	gs_eparam_t *p_out_rows;
	gs_eparam_t *p_smooth_sigma;
	gs_eparam_t *p_smooth_step;

	gs_texrender_t *input_rt;
	gs_texrender_t *output_rt;
	gs_texrender_t *fill_rt;
	gs_texrender_t *fillv_rt;
	gs_texrender_t *smh_rt; /* field smoothing, horizontal pass */
	gs_texrender_t *smv_rt; /* field smoothing, vertical pass */
	/* double-buffered: stage frame N, map frame N-1 — a same-frame map
	   right after gs_stage_texture is a full GPU sync; two instances
	   stalling each other measured 51ms/frame. One frame of stream
	   latency instead. */
	gs_stagesurf_t *stage[2];
	int stage_idx;
	bool stage_primed;

	/* Apple's GL-on-Metal returns constant black for ANY texture
	   sampling in the vertex stage (measured; texture/textureLod/
	   texelFetch all fail with verified-correct bindings), and the
	   Metal backend can't compile obs-shaderfilter, so the rig can't
	   move off GL. The splat data therefore arrives as a per-frame
	   streamed COLOR attribute instead of a vertex texture fetch:
	   rgb = splat color, a = the bake's encoded depth. Positions are
	   static; only the color buffer is flushed per frame. */
	gs_vertbuffer_t *splat_vb; /* points: W*SS per row, all rows  */
	gs_vertbuffer_t *warp_vb;  /* tristrip: 2 verts per src col per row */
	uint32_t *splat_colors;    /* owned by splat_vb->data */
	uint32_t *warp_colors;     /* owned by warp_vb->data */
	/* coarse connected warp mesh (mesh_step > 1): vertices every N px,
	   see VSWarpMesh. mesh_col/mesh_y = each vertex's source column and
	   output y, for the per-frame depth fill. */
	gs_vertbuffer_t *mesh_vb;
	uint32_t *mesh_colors;
	uint16_t *mesh_col;
	uint16_t *mesh_y;
	size_t mesh_verts;
	int vb_mesh_step;
	size_t splat_verts;
	size_t warp_verts;
	uint32_t vb_width, vb_height;
	bool vb_ssaa;

	/* Stereo field, displayed disparity (L - R, canvas px, + = in front
	   of the screen) of bake depth dt (1 = near):
	       D(dt) = plane_px + thickness_px * (dt - anchor)
	   thickness = total depth range (CV ch0), plane = where the anchor
	   depth sits, anchor = which depth holds still when thickness moves
	   (1 front, 0.5 middle, 0 back). frame_inset_px = fixed crop margin
	   per side, so framing does not breathe with thickness. */
	float thickness_px;
	float plane_px;
	float anchor;
	float frame_inset_px;
	float vignette_pct; /* depth vignette ramp width, % of frame */
	float smooth_sigma; /* field smoothing of the baked depth, source px
			       (0 = off); replaces the two "field smooth"
			       shader filters */
	float smooth_follow; /* extra smoothing that grows with thickness:
				sigma px per 100 px of thickness_px. The
				sigma used is max(smooth_sigma, this *
				thickness / 100), so a depth step never
				gets steeper on screen as the field opens
				up (steep steps fold in one eye and tear
				in the other). 0 = off. */
	int mode;       /* 0 = splat, 1 = warp */
	int mesh_step;  /* warp: vertex spacing in px (1 = one per pixel) */
	bool window;    /* floating window instead of edge taper */
	int fill;       /* micro-fill width px (0 = off) */
	bool ssaa;      /* 2x vertical supersampling of the splat */
	bool mlaa;      /* reveal-edge MLAA in the present pass */
	bool sync;      /* true = same-frame readback (correct for
			   on-demand/screenshot sources); false = one-frame
			   latency, no GPU sync stall (live chains) */
	int debug;      /* 0 off, 1 depth points, 2 input rgb, 3 input alpha */
};

static const char *splat_get_name(void *unused)
{
	UNUSED_PARAMETER(unused);
	return obs_module_text("StereoSplat");
}

static void splat_update(void *data, obs_data_t *settings)
{
	struct stereo_splat *s = data;
	/* One-time migration from the legacy depth/convergence pair
	   (f0 = depth^2 * MAX_PARALLAX, disparity = 2*(dt - c)*f0/span):
	   the picture is unchanged on first load of an old scene. */
	if (!obs_data_has_user_value(settings, "thickness_px") &&
	    obs_data_has_user_value(settings, "depth")) {
		double depth = obs_data_get_double(settings, "depth");
		double c = obs_data_get_double(settings, "convergence");
		double f0 = depth * depth * MAX_PARALLAX;
		double span = (PIC_HI - f0) - (PIC_LO + f0);
		double t = 2.0 * f0 / span * FIELD_EYE_PX;
		double a = c < 0.0 ? 0.0 : (c > 1.0 ? 1.0 : c);
		obs_data_set_double(settings, "thickness_px", t);
		obs_data_set_double(settings, "anchor", a);
		obs_data_set_double(settings, "plane_px", t * (a - c));
		obs_data_set_double(settings, "frame_inset_px",
				    f0 / span * FIELD_EYE_PX);
		obs_log(LOG_INFO,
			"stereo splat: migrated depth %.3f conv %.3f -> thickness %.1f px, plane %+.1f px, anchor %.2f, inset %.1f px",
			depth, c, t, t * (a - c), a, f0 / span * FIELD_EYE_PX);
	}
	s->thickness_px = (float)obs_data_get_double(settings, "thickness_px");
	s->plane_px = (float)obs_data_get_double(settings, "plane_px");
	s->anchor = (float)obs_data_get_double(settings, "anchor");
	s->frame_inset_px =
		(float)obs_data_get_double(settings, "frame_inset_px");
	s->mode = (int)obs_data_get_int(settings, "mode");
	s->mesh_step = (int)obs_data_get_int(settings, "mesh_step");
	if (s->mesh_step < 1)
		s->mesh_step = 1;
	if (s->mesh_step > 16)
		s->mesh_step = 16;
	s->vignette_pct =
		(float)obs_data_get_double(settings, "vignette_pct");
	s->smooth_sigma =
		(float)obs_data_get_double(settings, "smooth_sigma");
	s->smooth_follow =
		(float)obs_data_get_double(settings, "smooth_follow");
	s->window = obs_data_get_bool(settings, "window");
	s->fill = (int)obs_data_get_int(settings, "fill");
	s->ssaa = obs_data_get_bool(settings, "ssaa");
	s->mlaa = obs_data_get_bool(settings, "mlaa");
	s->sync = obs_data_get_bool(settings, "sync");
	s->debug = (int)obs_data_get_int(settings, "debug");
}

static void splat_defaults(obs_data_t *settings)
{
	obs_data_set_default_double(settings, "thickness_px", 30.0);
	obs_data_set_default_double(settings, "plane_px", 0.0);
	obs_data_set_default_double(settings, "anchor", 1.0);
	obs_data_set_default_double(settings, "frame_inset_px", 48.0);
	obs_data_set_default_double(settings, "vignette_pct", 0.0);
	obs_data_set_default_double(settings, "smooth_sigma", 0.0);
	obs_data_set_default_double(settings, "smooth_follow", 0.0);
	obs_data_set_default_int(settings, "mode", 0);
	obs_data_set_default_int(settings, "mesh_step", 1);
	obs_data_set_default_bool(settings, "window", false);
	obs_data_set_default_int(settings, "fill", 3);
	obs_data_set_default_bool(settings, "ssaa", true);
	obs_data_set_default_bool(settings, "mlaa", false);
	obs_data_set_default_bool(settings, "sync", true);
	obs_data_set_default_int(settings, "debug", 0);
}

static obs_properties_t *splat_properties(void *data)
{
	UNUSED_PARAMETER(data);
	obs_properties_t *props = obs_properties_create();
	obs_properties_add_float_slider(props, "plane_px",
					obs_module_text("Plane"), -300.0,
					300.0, 0.5);
	obs_properties_add_float_slider(props, "thickness_px",
					obs_module_text("Thickness"), 0.0,
					600.0, 0.5);
	obs_properties_add_float_slider(props, "anchor",
					obs_module_text("Anchor"), 0.0, 1.0,
					0.01);
	obs_properties_add_float_slider(props, "vignette_pct",
					obs_module_text("DepthVignette"), 0.0,
					50.0, 0.5);
	obs_properties_add_float_slider(props, "smooth_sigma",
					obs_module_text("FieldSmooth"), 0.0,
					24.0, 0.05);
	obs_properties_add_float_slider(props, "smooth_follow",
					obs_module_text("FieldSmoothFollow"),
					0.0, 12.0, 0.1);
	obs_properties_add_float_slider(props, "frame_inset_px",
					obs_module_text("FrameInset"), 0.0,
					120.0, 1.0);
	obs_property_t *m = obs_properties_add_list(
		props, "mode", obs_module_text("Mode"), OBS_COMBO_TYPE_LIST,
		OBS_COMBO_FORMAT_INT);
	obs_property_list_add_int(m, obs_module_text("ModeSplat"), 0);
	obs_property_list_add_int(m, obs_module_text("ModeWarp"), 1);
	obs_properties_add_int_slider(props, "mesh_step",
				      obs_module_text("MeshStep"), 1, 16, 1);
	obs_properties_add_bool(props, "window",
				obs_module_text("FloatingWindow"));
	obs_properties_add_int_slider(props, "fill",
				      obs_module_text("MicroFill"), 0, 5, 1);
	obs_properties_add_bool(props, "ssaa", obs_module_text("SSAA"));
	obs_properties_add_bool(props, "mlaa", obs_module_text("MLAA"));
	obs_properties_add_bool(props, "sync", obs_module_text("SyncReadback"));
	obs_properties_add_int_slider(props, "debug",
				      obs_module_text("DebugDepth"), 0, 5, 1);
	return props;
}

static void splat_destroy(void *data)
{
	struct stereo_splat *s = data;
	obs_enter_graphics();
	if (s->effect)
		gs_effect_destroy(s->effect);
	if (s->input_rt)
		gs_texrender_destroy(s->input_rt);
	if (s->output_rt)
		gs_texrender_destroy(s->output_rt);
	if (s->fill_rt)
		gs_texrender_destroy(s->fill_rt);
	if (s->fillv_rt)
		gs_texrender_destroy(s->fillv_rt);
	if (s->smh_rt)
		gs_texrender_destroy(s->smh_rt);
	if (s->smv_rt)
		gs_texrender_destroy(s->smv_rt);
	if (s->stage[0])
		gs_stagesurface_destroy(s->stage[0]);
	if (s->stage[1])
		gs_stagesurface_destroy(s->stage[1]);
	if (s->splat_vb)
		gs_vertexbuffer_destroy(s->splat_vb);
	if (s->warp_vb)
		gs_vertexbuffer_destroy(s->warp_vb);
	if (s->mesh_vb)
		gs_vertexbuffer_destroy(s->mesh_vb);
	obs_leave_graphics();
	bfree(s->mesh_col);
	bfree(s->mesh_y);
	bfree(s);
}

static void *splat_create(obs_data_t *settings, obs_source_t *context)
{
	struct stereo_splat *s = bzalloc(sizeof(*s));
	s->context = context;

	char *path = obs_module_file("stereo-splat.effect");
	obs_enter_graphics();
	s->effect = gs_effect_create_from_file(path, NULL);
	s->input_rt = gs_texrender_create(GS_RGBA, GS_ZS_NONE);
	s->output_rt = gs_texrender_create(GS_RGBA, GS_Z24_S8);
	s->fill_rt = gs_texrender_create(GS_RGBA, GS_ZS_NONE);
	s->fillv_rt = gs_texrender_create(GS_RGBA, GS_ZS_NONE);
	s->smh_rt = gs_texrender_create(GS_RGBA, GS_ZS_NONE);
	s->smv_rt = gs_texrender_create(GS_RGBA, GS_ZS_NONE);
	obs_leave_graphics();
	bfree(path);

	if (!s->effect) {
		obs_log(LOG_ERROR, "stereo-splat.effect failed to compile");
		splat_destroy(s);
		return NULL;
	}

	s->p_image = gs_effect_get_param_by_name(s->effect, "image");
	s->p_dims = gs_effect_get_param_by_name(s->effect, "dims");
	s->p_eye_sign = gs_effect_get_param_by_name(s->effect, "eye_sign");
	s->p_eye_base = gs_effect_get_param_by_name(s->effect, "eye_base");
	s->p_eye_w = gs_effect_get_param_by_name(s->effect, "eye_w");
	s->p_f0 = gs_effect_get_param_by_name(s->effect, "f0");
	s->p_vis_lo = gs_effect_get_param_by_name(s->effect, "vis_lo");
	s->p_span = gs_effect_get_param_by_name(s->effect, "span");
	s->p_window_on = gs_effect_get_param_by_name(s->effect, "window_on");
	s->p_fill_px = gs_effect_get_param_by_name(s->effect, "fill_px");
	s->p_mlaa_on = gs_effect_get_param_by_name(s->effect, "mlaa_on");
	s->p_convergence =
		gs_effect_get_param_by_name(s->effect, "shift0");
	s->p_inset = gs_effect_get_param_by_name(s->effect, "inset");
	s->p_anchor_d = gs_effect_get_param_by_name(s->effect, "anchor_d");
	s->p_vig_w = gs_effect_get_param_by_name(s->effect, "vig_w");
	s->p_out_rows = gs_effect_get_param_by_name(s->effect, "out_rows");
	s->p_smooth_sigma =
		gs_effect_get_param_by_name(s->effect, "smooth_sigma");
	s->p_smooth_step =
		gs_effect_get_param_by_name(s->effect, "smooth_step");

	splat_update(s, settings);
	return s;
}

/* Build the static vertex grids for the current source size. Vertex
   encoding (vec3): x = source-x position in [0,1] uv (subsample center
   for splat, pixel center for warp), y = OUTPUT row index, z unused.
   Splat order is row-major, ascending x — required for the LEQUAL
   largest-x tie-break. Already in the graphics context (video_render). */
static void rebuild_vertex_buffers(struct stereo_splat *s, uint32_t w,
				   uint32_t h, bool ssaa)
{
	uint32_t out_h = ssaa ? h * 2 : h;
	if (s->splat_vb) {
		gs_vertexbuffer_destroy(s->splat_vb);
		s->splat_vb = NULL;
	}
	if (s->warp_vb) {
		gs_vertexbuffer_destroy(s->warp_vb);
		s->warp_vb = NULL;
	}
	for (int i = 0; i < 2; i++) {
		if (s->stage[i]) {
			gs_stagesurface_destroy(s->stage[i]);
			s->stage[i] = NULL;
		}
		s->stage[i] = gs_stagesurface_create(w, h, GS_RGBA);
	}
	s->stage_idx = 0;
	s->stage_primed = false;

	/* splat (v19 pixel-footprint lines): per source pixel one GS_LINES
	   pair spanning its dest footprint — x = pixel EDGE u, z = pixel
	   CENTER u (PIC crop + z-key taper). GS_DYNAMIC because the colors
	   stream every frame (positions upload once at create and are
	   never flushed again — flush_direct sends colors only). */
	size_t n = (size_t)w * 2 * out_h;
	struct gs_vb_data *vbd = gs_vbdata_create();
	vbd->num = n;
	vbd->points = bmalloc(n * sizeof(struct vec3));
	vbd->colors = bzalloc(n * sizeof(uint32_t));
	size_t k = 0;
	for (uint32_t row = 0; row < out_h; row++) {
		for (uint32_t col = 0; col < w; col++) {
			float uc = ((float)col + 0.5f) / (float)w;
			vbd->points[k].x = (float)col / (float)w;
			vbd->points[k].y = (float)row;
			vbd->points[k].z = uc;
			k++;
			vbd->points[k].x = ((float)col + 1.0f) / (float)w;
			vbd->points[k].y = (float)row;
			vbd->points[k].z = uc;
			k++;
		}
	}
	s->splat_colors = vbd->colors;
	s->splat_vb = gs_vertexbuffer_create(vbd, GS_DYNAMIC);
	s->splat_verts = n;

	/* warp: per row, a 1px-tall quad strip across all source pixel
	   centers (2 verts per column: top y=row, bottom y=row+1 encoded
	   via z flag), rows stitched with degenerate verts. */
	size_t row_verts = (size_t)w * 2;
	size_t wn = (row_verts + 2) * h; /* +2 degenerates per row */
	vbd = gs_vbdata_create();
	vbd->num = wn;
	vbd->points = bmalloc(wn * sizeof(struct vec3));
	vbd->colors = bzalloc(wn * sizeof(uint32_t));
	k = 0;
	for (uint32_t row = 0; row < h; row++) {
		for (uint32_t col = 0; col < w; col++) {
			float u = ((float)col + 0.5f) / (float)w;
			vbd->points[k].x = u;
			vbd->points[k].y = (float)row;
			vbd->points[k].z = 0.0f; /* top edge */
			k++;
			vbd->points[k].x = u;
			vbd->points[k].y = (float)row;
			vbd->points[k].z = 1.0f; /* bottom edge */
			k++;
		}
		/* degenerate stitch: repeat last vert, then first of next */
		vbd->points[k] = vbd->points[k - 1];
		k++;
		if (row + 1 < h) {
			vbd->points[k].x = 0.5f / (float)w;
			vbd->points[k].y = (float)(row + 1);
			vbd->points[k].z = 0.0f;
		} else {
			vbd->points[k] = vbd->points[k - 1];
		}
		k++;
	}
	s->warp_colors = vbd->colors;
	s->warp_vb = gs_vertexbuffer_create(vbd, GS_DYNAMIC);
	s->warp_verts = wn;

	/* coarse warp mesh: one tristrip per band of N rows, 2 verts per
	   vertex column (top y_j, bottom y_j+1), bands stitched with
	   degenerates. Last column / row clamp to the frame edge. */
	if (s->mesh_vb) {
		gs_vertexbuffer_destroy(s->mesh_vb);
		s->mesh_vb = NULL;
	}
	bfree(s->mesh_col);
	bfree(s->mesh_y);
	s->mesh_col = NULL;
	s->mesh_y = NULL;
	s->mesh_verts = 0;
	uint32_t step = (uint32_t)(s->mesh_step < 1 ? 1 : s->mesh_step);
	if (step > 1) {
		uint32_t nc = (w - 1 + step - 1) / step + 1;
		uint32_t nr = (h + step - 1) / step + 1;
		size_t mn = ((size_t)nc * 2 + 2) * (nr - 1);
		vbd = gs_vbdata_create();
		vbd->num = mn;
		vbd->points = bmalloc(mn * sizeof(struct vec3));
		vbd->colors = bzalloc(mn * sizeof(uint32_t));
		s->mesh_col = bmalloc(mn * sizeof(uint16_t));
		s->mesh_y = bmalloc(mn * sizeof(uint16_t));
		k = 0;
		for (uint32_t j = 0; j + 1 < nr; j++) {
			uint32_t y0 = j * step, y1 = (j + 1) * step;
			if (y1 > h)
				y1 = h;
			for (uint32_t i = 0; i < nc; i++) {
				uint32_t col = i * step;
				if (col > w - 1)
					col = w - 1;
				float u = ((float)col + 0.5f) / (float)w;
				for (int e = 0; e < 2; e++) {
					uint32_t y = e ? y1 : y0;
					vbd->points[k].x = u;
					vbd->points[k].y = (float)y;
					vbd->points[k].z = 0.0f;
					s->mesh_col[k] = (uint16_t)col;
					s->mesh_y[k] = (uint16_t)y;
					k++;
				}
			}
			/* stitch: repeat the last vert, then the first of the
			   next band (or the last again on the final band) */
			vbd->points[k] = vbd->points[k - 1];
			s->mesh_col[k] = s->mesh_col[k - 1];
			s->mesh_y[k] = s->mesh_y[k - 1];
			k++;
			if (j + 2 < nr) {
				vbd->points[k].x = 0.5f / (float)w;
				vbd->points[k].y = (float)y1;
				vbd->points[k].z = 0.0f;
				s->mesh_col[k] = 0;
				s->mesh_y[k] = (uint16_t)y1;
			} else {
				vbd->points[k] = vbd->points[k - 1];
				s->mesh_col[k] = s->mesh_col[k - 1];
				s->mesh_y[k] = s->mesh_y[k - 1];
			}
			k++;
		}
		s->mesh_colors = vbd->colors;
		s->mesh_vb = gs_vertexbuffer_create(vbd, GS_DYNAMIC);
		s->mesh_verts = mn;
	}
	s->vb_mesh_step = s->mesh_step;

	s->vb_width = w;
	s->vb_height = h;
	s->vb_ssaa = ssaa;
	obs_log(LOG_INFO,
		"stereo splat: rebuilt grids %ux%u (splat %zu pts, warp %zu verts, mesh step %d: %zu verts)",
		w, h, s->splat_verts, s->warp_verts, s->mesh_step,
		s->mesh_verts);
}

/* Oracle vertical crop: output row -> source row (the CPU color fill
   applies it, so the vertex shader needs no per-row source lookup). */
static inline uint32_t vy_row(uint32_t out_row, uint32_t out_h,
			      uint32_t src_h, float f0)
{
	float sr = floorf((f0 +
			   ((float)out_row + 0.5f) / (float)out_h *
				   (1.0f - 2.0f * f0)) *
			  (float)src_h);
	if (sr < 0.0f)
		sr = 0.0f;
	if (sr > (float)(src_h - 1))
		sr = (float)(src_h - 1);
	return (uint32_t)sr;
}

/* Stream the staged input into the active buffer's COLOR array:
   rgb = splat color, a = the bake's encoded depth (a = 0.5 + d/2). */
static bool fill_colors(struct stereo_splat *s, bool warp, bool mesh,
			uint32_t w, uint32_t h, float f0)
{
	uint8_t *px;
	uint32_t linesize;
	/* map the surface staged LAST frame (no sync stall) */
	if (!gs_stagesurface_map(s->stage[s->stage_idx ^ 1], &px, &linesize))
		return false;

	if (mesh) {
		/* coarse mesh: one texel (depth in alpha) per vertex */
		uint32_t *dst = s->mesh_colors;
		for (size_t k = 0; k < s->mesh_verts; k++) {
			uint32_t y = s->mesh_y[k];
			if (y > h - 1)
				y = h - 1;
			const uint32_t *src =
				(const uint32_t *)(px +
						   (size_t)vy_row(y, h, h, f0) *
							   linesize);
			dst[k] = src[s->mesh_col[k]];
		}
	} else if (!warp) {
		uint32_t out_h = s->vb_ssaa ? h * 2 : h;
		uint32_t *dst = s->splat_colors;
		for (uint32_t row = 0; row < out_h; row++) {
			const uint32_t *src = (const uint32_t
						       *)(px +
							  (size_t)vy_row(
								  row, out_h,
								  h, f0) *
								  linesize);
			for (uint32_t col = 0; col < w; col++) {
				uint32_t c = src[col];
				dst[0] = c;
				dst[1] = c;
				dst += 2;
			}
		}
	} else {
		uint32_t *dst = s->warp_colors;
		for (uint32_t row = 0; row < h; row++) {
			const uint32_t *src = (const uint32_t
						       *)(px +
							  (size_t)vy_row(
								  row, h, h,
								  f0) *
								  linesize);
			for (uint32_t col = 0; col < w; col++) {
				uint32_t c = src[col];
				dst[0] = c;
				dst[1] = c;
				dst += 2;
			}
			/* degenerate stitch verts: value irrelevant */
			dst[0] = dst[-1];
			dst[1] = dst[-1];
			dst += 2;
		}
	}
	gs_stagesurface_unmap(s->stage[s->stage_idx ^ 1]);
	return true;
}

static void splat_render(void *data, gs_effect_t *unused_effect)
{
	UNUSED_PARAMETER(unused_effect);
	struct stereo_splat *s = data;

	obs_source_t *target = obs_filter_get_target(s->context);
	obs_source_t *parent = obs_filter_get_parent(s->context);
	if (!target || !parent || !s->effect) {
		obs_source_skip_video_filter(s->context);
		return;
	}
	uint32_t w = obs_source_get_base_width(target);
	uint32_t h = obs_source_get_base_height(target);
	if (!w || !h) {
		obs_source_skip_video_filter(s->context);
		return;
	}

	if (!s->splat_vb || s->vb_width != w || s->vb_height != h ||
	    s->vb_ssaa != s->ssaa || s->vb_mesh_step != s->mesh_step)
		rebuild_vertex_buffers(s, w, h, s->ssaa);

	/* ---- pass 1: grab the filter input (bake output) ---- */
	gs_texrender_reset(s->input_rt);
	gs_blend_state_push();
	gs_blend_function(GS_BLEND_ONE, GS_BLEND_ZERO);
	if (gs_texrender_begin(s->input_rt, w, h)) {
		uint32_t flags = obs_source_get_output_flags(target);
		bool custom = (flags & OBS_SOURCE_CUSTOM_DRAW) != 0;
		bool async = (flags & OBS_SOURCE_ASYNC) != 0;
		struct vec4 clear;
		vec4_zero(&clear);
		gs_clear(GS_CLEAR_COLOR, &clear, 0.0f, 0);
		gs_ortho(0.0f, (float)w, 0.0f, (float)h, -100.0f, 100.0f);
		if (target == parent && !custom && !async)
			obs_source_default_render(target);
		else
			obs_source_video_render(target);
		gs_texrender_end(s->input_rt);
	}
	gs_blend_state_pop();

	gs_texture_t *input_tex = gs_texrender_get_texture(s->input_rt);
	if (!input_tex) {
		obs_source_skip_video_filter(s->context);
		return;
	}

	/* ---- pass 1a: field smoothing (blur of the baked depth in alpha,
	   h then v; rgb untouched). Everything downstream reads the result
	   as its input. ---- */
	float sigma_eff = s->smooth_follow * fabsf(s->thickness_px) / 100.0f;
	if (sigma_eff < s->smooth_sigma)
		sigma_eff = s->smooth_sigma;
	if (sigma_eff > 30.0f) /* the 21-tap kernel's useful limit */
		sigma_eff = 30.0f;
	if (sigma_eff > 0.0f && s->debug != 2) {
		gs_texrender_t *rts[2] = {s->smh_rt, s->smv_rt};
		struct vec2 sdims;
		vec2_set(&sdims, (float)w, (float)h);
		gs_blend_state_push();
		gs_blend_function(GS_BLEND_ONE, GS_BLEND_ZERO);
		for (int pass = 0; pass < 2; pass++) {
			struct vec2 step;
			vec2_set(&step, pass == 0 ? 1.0f / (float)w : 0.0f,
				 pass == 0 ? 0.0f : 1.0f / (float)h);
			gs_texrender_reset(rts[pass]);
			if (!gs_texrender_begin(rts[pass], w, h))
				break;
			gs_ortho(0.0f, (float)w, 0.0f, (float)h, -100.0f,
				 100.0f);
			gs_effect_set_texture(s->p_image, input_tex);
			gs_effect_set_vec2(s->p_dims, &sdims);
			gs_effect_set_float(s->p_smooth_sigma, sigma_eff);
			gs_effect_set_vec2(s->p_smooth_step, &step);
			while (gs_effect_loop(s->effect, "Smooth"))
				gs_draw_sprite(input_tex, 0, w, h);
			gs_texrender_end(rts[pass]);
			gs_texture_t *t = gs_texrender_get_texture(rts[pass]);
			if (t)
				input_tex = t;
		}
		gs_blend_state_pop();
	}

	/* ---- pass 1b: stage the input and stream the COLOR attribute
	   (vertex texture fetch is broken on Apple GL — see header) ---- */
	/* fi = frame inset in source-u units: fi/span = inset_px/eye. With
	   span = (PIC_HI - PIC_LO) - 2*fi  ->  fi = r*(PIC_HI-PIC_LO)/(1+2r).
	   k  = per-eye displacement per unit depth (source u),
	   s0 = per-eye constant shift, both from the px controls. */
	float r_in = s->frame_inset_px / FIELD_EYE_PX;
	float f0 = r_in * (PIC_HI - PIC_LO) / (1.0f + 2.0f * r_in);
	float span_f = (PIC_HI - f0) - (PIC_LO + f0);
	float disp_k = s->thickness_px * span_f / (2.0f * FIELD_EYE_PX);
	float disp_s0 = (s->anchor * s->thickness_px - s->plane_px) * span_f /
			(2.0f * FIELD_EYE_PX);
	bool warp_mode = (s->mode == 1 && s->debug == 0);
	bool mesh_mode = warp_mode && s->mesh_step > 1 && s->mesh_vb;
	if (s->debug < 2 || s->debug == 5) {
		gs_stage_texture(s->stage[s->stage_idx], input_tex);
		if (s->sync) {
			/* same-frame map (GPU sync): required for sources
			   that render on demand (screenshots/harness) */
			s->stage_idx ^= 1; /* fill maps idx^1 = just staged */
			s->stage_primed = true;
		} else if (!s->stage_primed) {
			/* first frame: nothing staged yet to map */
			s->stage_primed = true;
			s->stage_idx ^= 1;
			obs_source_skip_video_filter(s->context);
			return;
		}
		if (!fill_colors(s, warp_mode, mesh_mode, w, h, f0)) {
			obs_source_skip_video_filter(s->context);
			return;
		}
		if (!s->sync)
			s->stage_idx ^= 1;
		struct gs_vb_data stream = {0};
		stream.num = mesh_mode ? s->mesh_verts
				       : warp_mode ? s->warp_verts
						   : s->splat_verts;
		stream.colors = mesh_mode ? s->mesh_colors
					  : warp_mode ? s->warp_colors
						      : s->splat_colors;
		gs_vertexbuffer_flush_direct(mesh_mode ? s->mesh_vb
						       : warp_mode ? s->warp_vb
								   : s->splat_vb,
					     &stream);
	}

	/* ---- pass 2: depth-tested splat into the output ---- */
	float vis_lo = PIC_LO + f0;
	float span = (PIC_HI - f0) - vis_lo;
	float eye_w = (float)w * 0.5f;

	uint32_t out_h = warp_mode ? h : (s->ssaa ? h * 2 : h);
	gs_texrender_reset(s->output_rt);
	if (gs_texrender_begin(s->output_rt, w, out_h)) {
		struct vec4 clear;
		/* alpha 0 = unwritten-reveal sentinel for the fill pass */
		vec4_set(&clear, 0.0f, 0.0f, 0.0f, 0.0f);
		gs_clear(GS_CLEAR_COLOR | GS_CLEAR_DEPTH, &clear, 1.0f, 0);
		gs_ortho(0.0f, (float)w, 0.0f, (float)out_h, -100.0f,
			 100.0f);

		gs_blend_state_push();
		gs_enable_blending(false);
		gs_enable_depth_test(true);
		gs_depth_function(GS_LEQUAL);

		struct vec2 dims;
		vec2_set(&dims, (float)w, (float)h);

		if (s->debug == 2 || s->debug == 3) {
			/* blit the grabbed input: 2 = rgb, 3 = alpha gray */
			gs_enable_depth_test(false);
			gs_effect_t *blit = s->effect;
			gs_effect_set_texture(s->p_image, input_tex);
			const char *bt = s->debug == 2 ? "BlitRGB" : "BlitA";
			while (gs_effect_loop(blit, bt))
				gs_draw_sprite(input_tex, 0, w, out_h);
			gs_blend_state_pop();
			gs_texrender_end(s->output_rt);
			goto present;
		}

		const char *tech_name;
		if (s->debug == 5)
			tech_name = "DebugQ";
		else if (s->debug == 1)
			tech_name = "Debug";
		else if (mesh_mode)
			tech_name = "WarpMesh";
		else if (warp_mode)
			tech_name = "Warp";
		else
			tech_name = "Splat";
		gs_technique_t *tech =
			gs_effect_get_technique(s->effect, tech_name);

		gs_vertbuffer_t *vb = mesh_mode ? s->mesh_vb
						: warp_mode ? s->warp_vb
							    : s->splat_vb;
		size_t nverts = mesh_mode ? s->mesh_verts
					  : warp_mode ? s->warp_verts
						      : s->splat_verts;
		enum gs_draw_mode dm = warp_mode ? GS_TRISTRIP : GS_LINES;

		for (int eye = 0; eye < 2; eye++) {
			/* per-eye scissor: clipped/folded lines must never
			   spill into the sibling eye's half */
			struct gs_rect sc = {.x = eye == 0 ? 0 : (int)eye_w,
					     .y = 0,
					     .cx = (int)eye_w,
					     .cy = (int)out_h};
			gs_set_scissor_rect(&sc);
			/* ALL params re-set per pass: the effect upload is
			   changed-only and resets flags after each technique
			   — a texture set once goes stale on the second eye
			   (measured: warp R eye rendered black fragments). */
			gs_effect_set_texture(s->p_image, input_tex);
			gs_effect_set_vec2(s->p_dims, &dims);
			gs_effect_set_float(s->p_f0, disp_k);
			gs_effect_set_float(s->p_inset, f0);
			gs_effect_set_float(s->p_vis_lo, vis_lo);
			gs_effect_set_float(s->p_span, span);
			gs_effect_set_float(s->p_eye_w, eye_w);
			gs_effect_set_float(s->p_window_on,
					    s->window ? 1.0f : 0.0f);
			gs_effect_set_float(s->p_convergence, disp_s0);
			gs_effect_set_float(s->p_anchor_d, s->anchor);
			gs_effect_set_float(s->p_vig_w,
					    s->vignette_pct * 0.01f);
			gs_effect_set_float(s->p_out_rows, (float)out_h);
			gs_effect_set_float(s->p_eye_sign,
					    eye == 0 ? 1.0f : -1.0f);
			gs_effect_set_float(s->p_eye_base,
					    eye == 0 ? 0.0f : eye_w);
			gs_technique_begin(tech);
			gs_technique_begin_pass(tech, 0);
			gs_load_vertexbuffer(vb);
			gs_load_indexbuffer(NULL);
			gs_draw(dm, 0, (uint32_t)nverts);
			gs_technique_end_pass(tech);
			gs_technique_end(tech);
			gs_set_scissor_rect(NULL);
		}

		/* floating window: black strips, L eye left edge / R eye
		   right edge, width = f0-equivalent in eye pixels. Drawn
		   nearest (depth cleared region untouched elsewhere). */
		if (s->window && f0 > 0.0f) {
			float strip_w = f0 / span * eye_w;
			gs_effect_t *solid =
				obs_get_base_effect(OBS_EFFECT_SOLID);
			gs_eparam_t *color = gs_effect_get_param_by_name(
				solid, "color");
			struct vec4 black;
			vec4_set(&black, 0.0f, 0.0f, 0.0f, 1.0f);
			gs_effect_set_vec4(color, &black);
			gs_enable_depth_test(false);
			while (gs_effect_loop(solid, "Solid")) {
				gs_matrix_push();
				gs_matrix_identity();
				gs_matrix_translate3f(0.0f, 0.0f, 0.0f);
				gs_draw_sprite(NULL, 0, (uint32_t)strip_w,
					       out_h);
				gs_matrix_pop();
				gs_matrix_push();
				gs_matrix_identity();
				gs_matrix_translate3f((float)w - strip_w,
						      0.0f, 0.0f);
				gs_draw_sprite(NULL, 0, (uint32_t)strip_w,
					       out_h);
				gs_matrix_pop();
			}
		}

		gs_enable_depth_test(false);
		gs_blend_state_pop();
		gs_texrender_end(s->output_rt);
	}

present:;
	gs_texture_t *out_tex = gs_texrender_get_texture(s->output_rt);
	if (!out_tex) {
		obs_source_skip_video_filter(s->context);
		return;
	}

	/* ---- pass 2b: micro-fill (splat mode only; reveal runs <= fill
	   px continue the FARTHER flank, oracle micro_fill) ---- */
	if (s->fill > 0 && !warp_mode && s->debug < 2) {
		struct vec2 dims;
		vec2_set(&dims, (float)w, (float)out_h);
		gs_blend_state_push();
		gs_blend_function(GS_BLEND_ONE, GS_BLEND_ZERO);
		/* horizontal pass */
		gs_texrender_reset(s->fill_rt);
		if (gs_texrender_begin(s->fill_rt, w, out_h)) {
			gs_ortho(0.0f, (float)w, 0.0f, (float)out_h, -100.0f,
				 100.0f);
			gs_effect_set_texture(s->p_image, out_tex);
			gs_effect_set_vec2(s->p_dims, &dims);
			gs_effect_set_float(s->p_fill_px, (float)s->fill);
			while (gs_effect_loop(s->effect, "Fill"))
				gs_draw_sprite(out_tex, 0, w, out_h);
			gs_texrender_end(s->fill_rt);
			gs_texture_t *ft =
				gs_texrender_get_texture(s->fill_rt);
			if (ft)
				out_tex = ft;
		}
		/* vertical pass (row-shear slits on fine texture) */
		gs_texrender_reset(s->fillv_rt);
		if (gs_texrender_begin(s->fillv_rt, w, out_h)) {
			gs_ortho(0.0f, (float)w, 0.0f, (float)out_h, -100.0f,
				 100.0f);
			gs_effect_set_texture(s->p_image, out_tex);
			gs_effect_set_vec2(s->p_dims, &dims);
			gs_effect_set_float(s->p_fill_px, (float)s->fill);
			while (gs_effect_loop(s->effect, "FillV"))
				gs_draw_sprite(out_tex, 0, w, out_h);
			gs_texrender_end(s->fillv_rt);
			gs_texture_t *fv =
				gs_texrender_get_texture(s->fillv_rt);
			if (fv)
				out_tex = fv;
		}
		gs_blend_state_pop();
	}

	/* ---- pass 3: present (downsamples 2x-tall buffers via one
	   linear tap at the output row center; forces alpha to 1; or
	   reveal-edge MLAA on 1x buffers) ---- */
	bool use_mlaa = s->mlaa && out_h == h && !warp_mode && s->debug < 2;
	struct vec2 pdims;
	vec2_set(&pdims, (float)w, (float)out_h);
	gs_effect_set_texture(s->p_image, out_tex);
	gs_effect_set_vec2(s->p_dims, &pdims);
	gs_effect_set_float(s->p_mlaa_on, use_mlaa ? 1.0f : 0.0f);
	while (gs_effect_loop(s->effect, "Present"))
		gs_draw_sprite(out_tex, 0, w, h);
}

static uint32_t splat_width(void *data)
{
	struct stereo_splat *s = data;
	obs_source_t *target = obs_filter_get_target(s->context);
	return target ? obs_source_get_base_width(target) : 0;
}

static uint32_t splat_height(void *data)
{
	struct stereo_splat *s = data;
	obs_source_t *target = obs_filter_get_target(s->context);
	return target ? obs_source_get_base_height(target) : 0;
}

struct obs_source_info stereo_splat_filter = {
	.id = "stereo_splat_filter",
	.type = OBS_SOURCE_TYPE_FILTER,
	.output_flags = OBS_SOURCE_VIDEO | OBS_SOURCE_CUSTOM_DRAW,
	.get_name = splat_get_name,
	.create = splat_create,
	.destroy = splat_destroy,
	.update = splat_update,
	.get_defaults = splat_defaults,
	.get_properties = splat_properties,
	.video_render = splat_render,
	.get_width = splat_width,
	.get_height = splat_height,
};
