"""Panel-fit mode (video.mode: panelfit): the MEASURED reference style
as a mode — whole panel at native aspect over its own full-brightness
blurred fill, gentle LINEAR drift or constant-rate zoom-in, wide
panels full-bleed, NO captions, NO shake/flash, split-flap title
cards on timeline cues. See docs/EDITOR_GUIDE.md "Panel-fit mode",
plan.py's PF_* constants and ~/workspace/your_files/
reference-video-measured-spec.md for the measurements.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.config import load_config
from manhwa_ocr.video import build as build_mod
from manhwa_ocr.video import frames as frames_mod
from manhwa_ocr.video import plan as plan_mod
from manhwa_ocr.video import syncaudit
from manhwa_ocr.video.frames import Format, ShotRenderer, format_from
from manhwa_ocr.video.plan import (build_plan, panelfit_drift_travel, panelfit_fit,
                                  panelfit_is_impact, panelfit_spec, validate)

VCFG = load_config().video
FMT = format_from("16:9", 30)   # 1920x1080


# --------------------------------------------------------------- constants

def test_constants_pinned_across_config_planner_renderer():
    pf = load_config().video["panelfit"]
    assert pf["bg_blur"] == plan_mod.PF_BG_BLUR == frames_mod.PFR_BG_BLUR == 50
    # FULL brightness: the measured reference has no dimming layer.
    assert pf["bg_opacity"] == plan_mod.PF_BG_OPACITY == frames_mod.PFR_BG_OPACITY == 1.0
    assert pf["fill_aspect"] == plan_mod.PF_FILL_ASPECT == 1.6
    assert pf["drift_aspect_max"] == plan_mod.PF_DRIFT_ASPECT_MAX == 0.72
    assert pf["drift_speed"] == plan_mod.PF_DRIFT_SPEED == 0.0658
    assert pf["drift_min_travel"] == plan_mod.PF_DRIFT_MIN_TRAVEL == 0.05
    assert pf["drift_max_travel"] == plan_mod.PF_DRIFT_MAX_TRAVEL == 0.13
    assert pf["drift_headroom"] == plan_mod.PF_DRIFT_HEADROOM == 0.03
    assert pf["zoom_rate"] == plan_mod.PF_ZOOM_RATE == 0.05
    assert pf["zoom_rate_impact"] == plan_mod.PF_ZOOM_RATE_IMPACT == 0.088
    assert pf["zoom_max_total"] == plan_mod.PF_ZOOM_MAX_TOTAL == 0.50
    assert pf["max_upscale"] == plan_mod.PF_MAX_UPSCALE == 2.4
    assert pf["still_limit"] == plan_mod.PF_STILL_LIMIT == 0.5
    assert pf["min_shot"] == plan_mod.PF_MIN_SHOT == 0.3
    assert pf["split_above"] == plan_mod.PF_SPLIT_ABOVE == 3.0
    assert pf["split_min_piece"] == plan_mod.PF_SPLIT_MIN_PIECE == 1.2
    assert pf["captions"] is False and plan_mod.PF_CAPTIONS is False
    assert pf["title_cards"] is True and plan_mod.PF_TITLE_CARDS is True
    assert pf["tts_gap_seconds"] == plan_mod.PF_TTS_GAP == 0.11
    # Analysis fixes (user's second-by-second analysis, 2026-10-23):
    # the foreground drop shadow (fix 4) and gutter segment splitting
    # (fix 1), pinned across config / planner / renderer like the rest.
    assert pf["shadow_dy"] == plan_mod.PF_SHADOW_DY == frames_mod.PFR_SHADOW_DY == 20
    assert pf["shadow_blur"] == plan_mod.PF_SHADOW_BLUR == frames_mod.PFR_SHADOW_BLUR == 10
    assert pf["shadow_opacity"] == plan_mod.PF_SHADOW_OPACITY \
        == frames_mod.PFR_SHADOW_OPACITY == 0.40
    assert pf["gutter_segments"] is True and plan_mod.PF_GUTTER_SEGMENTS is True


def test_no_fx_constants_remain():
    # The measured reference has no shake and no flash; the mode must
    # not carry the old FX constants/config at all.
    for mod in (plan_mod, frames_mod):
        assert not hasattr(mod, "PF_SHAKE_SECONDS") and not hasattr(mod, "PFR_SHAKE_SECONDS")
        assert not hasattr(mod, "PF_FLASH_FRAMES") and not hasattr(mod, "PFR_FLASH_FRAMES")
    pf = load_config().video["panelfit"]
    assert not any(k in pf for k in ("fx", "shake_seconds", "shake_amplitude",
                                     "flash_frames", "zoom_end", "strip_aspect"))


# --------------------------------------------------------------- fit / spec

def test_fit_portrait_panel_by_height_zooms():
    kind, bw, bh, s = panelfit_fit(900, 1200, FMT)
    assert kind == "zoom"                       # aspect 0.75 >= drift band
    assert (bw, bh) == (810.0, 1080.0)          # height-fitted, aspect kept


def test_fit_narrow_pillar_drifts_oversized():
    kind, bw, bh, s = panelfit_fit(900, 2400, FMT, dur=2.0)
    assert kind == "drift"                      # aspect 0.375 < 0.72
    # travel = clamp(0.0658 * 2.0) = 0.13 -> oversize 1 + 0.13 + 0.03 = 1.16
    assert bh == pytest.approx(1080.0 * 1.16) and bw == pytest.approx(405.0 * 1.16)


def test_fit_exact_canvas_panel_fills_frame():
    kind, bw, bh, s = panelfit_fit(1920, 1080, FMT)
    assert kind == "fill" and (bw, bh) == (1920.0, 1080.0)


def test_fit_wide_panel_full_bleed():
    kind, bw, bh, s = panelfit_fit(2000, 900, FMT)
    assert kind == "fill" and (bw, bh) == (2400.0, 1080.0)   # cover: height exact, width over
    kind, bw, bh, s = panelfit_fit(1600, 1000, FMT)          # aspect exactly 1.6
    assert kind == "fill" and (bw, bh) == (1920.0, 1200.0)   # cover: width exact, height over


def test_fit_mid_panel_height_fit_not_fill():
    kind, bw, bh, s = panelfit_fit(1200, 1000, FMT)          # aspect 1.2
    assert kind == "zoom" and (bw, bh) == (1296.0, 1080.0)


def test_fit_wide_but_tiny_panel_cannot_fill_falls_back_to_pillar():
    # P039-01 of the real ch1 is 742x242 (aspect 3.07): its cover scale
    # is 4.46, past the 2.4 upscale cap, so a "fill" would not actually
    # fill. It must degrade to the capped pillar fit over blur.
    kind, bw, bh, s = panelfit_fit(741.8, 242.0, FMT)
    assert kind == "zoom" and s == 2.4
    assert bw <= FMT.width and bh <= FMT.height
    assert bw / bh == pytest.approx(741.8 / 242.0, rel=0.005)


def test_fit_tiny_panel_capped_not_enlarged_past_cap():
    kind, bw, bh, s = panelfit_fit(300, 400, FMT)
    assert kind == "zoom" and s == 2.4 and (bw, bh) == (720.0, 960.0)


@pytest.mark.parametrize("pw,ph", [(900, 1200), (2000, 900), (900, 2400), (700, 2000),
                                   (300, 400), (1600, 900), (640, 640), (900, 901),
                                   (1600, 1000), (1200, 1000)])
def test_fit_never_stretches(pw, ph):
    kind, bw, bh, s = panelfit_fit(pw, ph, FMT)
    assert bw / bh == pytest.approx(pw / ph, rel=0.005)


def test_drift_travel_clamped_to_measured_band():
    assert panelfit_drift_travel(1.0) == pytest.approx(0.0658)   # speed x duration
    assert panelfit_drift_travel(0.5) == pytest.approx(0.05)    # floor
    assert panelfit_drift_travel(10.0) == pytest.approx(0.13)   # cap
    assert panelfit_drift_travel(1.47) == pytest.approx(0.0658 * 1.47, abs=0.001)


def test_spec_zoom_is_a_constant_rate_not_a_fixed_total():
    sp = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=2.0)
    assert sp["kind"] == "zoom" and sp["dir"] == "in" and sp["bg"] is True
    assert sp["zoom_rate"] == 0.05 and sp["zoom_end"] == pytest.approx(1.10)
    sp10 = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=10.0)
    assert sp10["zoom_rate"] == 0.05 and sp10["zoom_end"] == pytest.approx(1.50)
    # pull_out no longer reverses: the reference never zooms out.
    assert panelfit_spec(900, 1200, "pull_out", FMT, {})["dir"] == "in"
    assert panelfit_spec(1920, 1080, "push_in", FMT, {})["bg"] is False   # canvas-shaped


def test_spec_zoom_total_is_capped():
    # A shot long enough that rate x duration would pass +50%: the
    # total clamps at the cap; the RATE itself is untouched (the
    # renderer holds the scale once the cap is reached).
    sp = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=20.0)
    assert sp["zoom_rate"] == 0.05 and sp["zoom_end"] == pytest.approx(1.50)
    hit = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=10.0, impact=True)
    assert hit["zoom_rate"] == 0.088 and hit["zoom_end"] == pytest.approx(1.50)


def test_spec_drift_and_impact_rates():
    sp = panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=2.0)
    assert sp["kind"] == "drift" and sp["drift_frac"] == pytest.approx(0.13)
    assert sp["zoom_rate"] == 0.0 and sp["zoom_end"] == 1.0
    hit = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=2.0, impact=True)
    assert hit["zoom_rate"] == 0.088 and hit["zoom_end"] == pytest.approx(1.176)


# --------------------------------------------------------------- impact

def _shot(start=10.0, effects=(), sentence=""):
    return {"id": "S001", "start": start, "end": start + 4.0,
            "effects": list(effects), "sentence": sentence}


def test_impact_from_marker():
    hit, rule = panelfit_is_impact(_shot(effects=[{"type": "shake", "t": 10.6}]))
    assert hit is True and rule == "marker"


def test_impact_fallback_action_sentence_with_impact_verb():
    hit, rule = panelfit_is_impact(_shot(sentence="He slams the iron gate shut with one hand."))
    assert hit is True and rule == "fallback"


def test_impact_none_for_internal_sentence_even_with_impact_word():
    hit, rule = panelfit_is_impact(_shot(sentence="She recalled how he strikes the bell at dawn."))
    assert hit is False and rule is None


def test_impact_none_for_plain_sentence():
    hit, rule = panelfit_is_impact(_shot(sentence="The duke walks into the hall."))
    assert hit is False and rule is None


# --------------------------------------------------------------- validate

def _plan_with_spec(spec):
    return {"version": plan_mod.PLAN_VERSION, "duration": 4.0, "shots": [
        {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single", "background": "blur",
         "exit": "cut", "transition": "cut",
         "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                    "motion": "push_in", "panelfit": spec}],
         "effects": [], "callouts": []}],
        "chapter_cards": [], "sfx": [], "captions": []}


def test_validate_accepts_good_specs_of_every_kind():
    for spec in (panelfit_spec(900, 1200, "push_in", FMT, {}),
                 panelfit_spec(900, 2400, "pan_down", FMT, {}),
                 panelfit_spec(2000, 900, "push_in", FMT, {})):
        assert validate(_plan_with_spec(spec), {"P001-01"}) == []


def test_validate_rejects_broken_specs():
    bad = {"kind": "spin", "dir": "in", "bg": True, "base_w": 810.0, "base_h": 1080.0,
           "zoom_rate": 0.05, "zoom_end": 1.1, "drift_frac": 0.0}
    assert any("panelfit" in e for e in validate(_plan_with_spec(bad), {"P001-01"}))
    driftless = {"kind": "drift", "dir": "down", "bg": True, "base_w": 470.0,
                 "base_h": 1253.0, "zoom_rate": 0.0, "zoom_end": 1.0, "drift_frac": 0.0}
    assert any("panelfit" in e for e in validate(_plan_with_spec(driftless), {"P001-01"}))
    overcapped = {"kind": "zoom", "dir": "in", "bg": True, "base_w": 810.0,
                  "base_h": 1080.0, "zoom_rate": 0.05, "zoom_end": 1.9, "drift_frac": 0.0}
    assert any("panelfit" in e for e in validate(_plan_with_spec(overcapped), {"P001-01"}))


# --------------------------------------------------------------- renderer

def _renderer(panel_img, spec, *, fx=None, start=0.0, end=4.0, motion="push_in"):
    shot = {"id": "S001", "start": start, "end": end, "layout": "single", "background": "blur",
            "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                       "motion": motion, "panelfit": spec}],
            "effects": [], "panelfit": True,
            "sentence": None, "callouts": []}
    if fx is not None:
        shot["panelfit_fx"] = fx   # legacy key: the renderer must ignore it
    return ShotRenderer(shot, {"P001-01": panel_img}, FMT)


def test_render_background_is_full_brightness():
    img = Image.new("RGB", (900, 1200), (255, 0, 0))
    r = _renderer(img, panelfit_spec(900, 1200, "push_in", FMT, {}))
    f = r.frame_array(0.1)
    assert tuple(f[540, 960]) == (255, 0, 0)            # centre: the sharp foreground
    corner = f[10, 10].astype(int)                       # corner: blurred fill, NOT dimmed
    assert corner[0] >= 250 and corner[1] <= 6 and corner[2] <= 6


def test_render_zoom_is_never_static():
    img = Image.new("RGB", (900, 1200), (200, 40, 40))
    ImageDraw.Draw(img).rectangle((300, 400, 600, 800), fill=(40, 40, 200))
    r = _renderer(img, panelfit_spec(900, 1200, "push_in", FMT, {}))
    assert not np.array_equal(r.frame_array(0.5), r.frame_array(0.9))
    assert r.is_static(2.0) is False


def _square_half_width(arr):
    row = arr[540]
    blue = np.flatnonzero((row[:, 2] > 128) & (row[:, 0] < 128))
    return (blue[-1] - blue[0]) / 2.0 if len(blue) else None


def test_render_zoom_grows_at_the_measured_rate():
    # A 4s zoom at +5.0%/s: scale 1.05 at t=1, 1.10 at t=2 — the
    # marker square's width ratio is ~1.048, NOT the old fixed +8%
    # total (which would give ~1.020 over the same span).
    img = Image.new("RGB", (900, 1200), (200, 40, 40))
    ImageDraw.Draw(img).rectangle((300, 400, 600, 800), fill=(40, 40, 200))
    r = _renderer(img, panelfit_spec(900, 1200, "push_in", FMT, {}, dur=4.0), end=4.0)
    w1 = _square_half_width(r.frame_array(1.0))
    w2 = _square_half_width(r.frame_array(2.0))
    assert w1 and w2 and w2 / w1 == pytest.approx(1.10 / 1.05, abs=0.015)


def test_render_zoom_holds_at_the_cap():
    # 12s at +5.0%/s: the +50% cap is reached at t=10 — the scale at
    # t=10.2 and t=11.8 must be the same (a hold, not a slowed rate),
    # and clearly larger than at t=5.
    img = Image.new("RGB", (900, 1200), (200, 40, 40))
    ImageDraw.Draw(img).rectangle((300, 400, 600, 800), fill=(40, 40, 200))
    r = _renderer(img, panelfit_spec(900, 1200, "push_in", FMT, {}, dur=12.0), end=12.0)
    w5 = _square_half_width(r.frame_array(5.0))
    w10 = _square_half_width(r.frame_array(10.2))
    w11 = _square_half_width(r.frame_array(11.8))
    assert w5 and w10 and w11
    assert w10 == pytest.approx(w11, abs=2)
    assert w10 > w5 * 1.15


def _boundary_y(arr):
    col = arr[:, 960]
    blue = np.flatnonzero(col[:, 2] > 128)
    return int(blue[0]) if len(blue) else None


def test_render_drift_velocity_is_constant_and_small():
    # Top half red, bottom half blue: the boundary's frame y reads the
    # drift position directly. The drift runs at the pinned SPEED
    # (0.0658 frame-heights/s ≈ 71 px/s at 1080p), NOT a full strip
    # traversal and NOT a slow stretch of the travel over the shot.
    img = Image.new("RGB", (900, 2400), (255, 0, 0))
    ImageDraw.Draw(img).rectangle((0, 1200, 900, 2399), fill=(0, 0, 255))
    r = _renderer(img, panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=8.0), end=8.0)
    ys = [_boundary_y(r.frame_array(t)) for t in (0.3, 1.1, 1.9)]
    assert all(y is not None for y in ys)
    assert ys[1] - ys[0] == pytest.approx(ys[2] - ys[1], abs=3)     # constant velocity
    assert ys[2] - ys[0] == pytest.approx(plan_mod.PF_DRIFT_SPEED * 1080 * 1.6, abs=6)


def test_render_drift_holds_at_the_travel_cap():
    # F2: travel = speed x span, hard-capped at 13% of frame height;
    # past the cap (~1.98s into an 8s span) the position HOLDS — it
    # neither reverses nor creeps at a slowed rate.
    img = Image.new("RGB", (900, 2400), (255, 0, 0))
    ImageDraw.Draw(img).rectangle((0, 1200, 900, 2399), fill=(0, 0, 255))
    r = _renderer(img, panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=8.0), end=8.0)
    y12 = _boundary_y(r.frame_array(1.2))
    y32 = _boundary_y(r.frame_array(3.2))
    y48 = _boundary_y(r.frame_array(4.8))
    assert y32 == pytest.approx(y48, abs=2)                        # held, exactly
    assert y32 - y12 == pytest.approx((0.065 - (-0.065 + plan_mod.PF_DRIFT_SPEED * 1.2)) * 1080,
                                      abs=5)                       # only the remaining travel


def test_render_fill_shows_the_sharp_panel_at_the_corners():
    # Fine stripes blur away in a pillar's background but stay pure in
    # a full-bleed foreground.
    img = Image.new("RGB", (2000, 900), (255, 0, 0))
    d = ImageDraw.Draw(img)
    for x in range(10, 2000, 20):
        d.rectangle((x, 0, x + 9, 899), fill=(0, 200, 0))
    r = _renderer(img, panelfit_spec(2000, 900, "push_in", FMT, {}))
    px = r.frame_array(0.1)[10, 10].astype(int)
    assert (px[0] > 200 and px[1] < 80) or (px[1] > 150 and px[0] < 80)


def test_render_has_no_flash_or_shake_even_with_a_legacy_fx_key():
    img = Image.new("RGB", (900, 1200), (120, 60, 60))
    ImageDraw.Draw(img).rectangle((350, 400, 550, 800), fill=(240, 240, 240))
    spec = panelfit_spec(900, 1200, "push_in", FMT, {})
    plain = _renderer(img, spec)
    legacy = _renderer(img, spec, fx={"shake_t": [0.0], "flash_t": [0.0]})
    for t in (0.0, 0.05, 0.5):
        assert np.array_equal(legacy.frame_array(t), plain.frame_array(t))
    assert float(plain.frame_array(0.0).mean()) < 200   # never a white flash frame


def test_render_drop_shadow_under_a_floating_panel():
    # Analysis fix 4: a panel floating over its own blur (here a small
    # panel held at the upscale cap, so it sits inside the frame)
    # carries a black shadow (Y-offset 20px, blur 10px, 40% opacity)
    # under its bottom edge — while the background elsewhere stays
    # FULL brightness (the mode never dims the blur; the shadow is
    # the only contrast device).
    img = Image.new("RGB", (400, 300), (255, 0, 0))
    r = _renderer(img, panelfit_spec(400, 300, "push_in", FMT, {}))
    f = r.frame_array(0.1).astype(int)
    # Capped fit: 960x720, centred -> fg spans y 180..900.
    assert tuple(f[850, 960]) == (255, 0, 0)          # the fg itself, untouched
    assert 90 < f[910, 960][0] < 235                  # the shadow band below it
    assert f[100, 960][0] >= 250                      # bg above: undimmed blur


def test_render_slice_shows_only_the_segment_band():
    # Analysis fix 1: a card with a `slice` renders only that band of
    # the panel, fitted as a picture of the segment's own height.
    img = Image.new("RGB", (900, 1800), (200, 30, 30))
    ImageDraw.Draw(img).rectangle((0, 900, 900, 1800), fill=(30, 30, 200))
    shot = {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single", "background": "blur",
            "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                       "motion": "push_in", "panelfit": panelfit_spec(900, 900, "push_in", FMT, {}),
                       "slice": [0.5, 1.0]}],
            "effects": [], "panelfit": True, "sentence": None, "callouts": []}
    r = ShotRenderer(shot, {"P001-01": img}, FMT)
    f = r.frame_array(0.5)
    centre = f[540, 960].astype(int)
    assert centre[2] > 150 and centre[0] < 80         # the blue (bottom) segment
    plain = _renderer(img, panelfit_spec(900, 1800, "push_in", FMT, {}))
    assert not np.array_equal(f, plain.frame_array(0.5))


# --------------------------------------------------------------- build_plan + QA

def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 11):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


PANELS = [
    {"id": "P001-01", "width": 900, "height": 1200, "text": ""},    # pillar -> zoom
    {"id": "P001-02", "width": 900, "height": 2400, "text": ""},    # narrow pillar -> drift
    {"id": "P001-03", "width": 1500, "height": 900, "text": ""},    # aspect 1.67 -> fill
]


def _panelfit_plan():
    vcfg = {**VCFG, "mode": "panelfit"}
    sentences = [
        {"start": 0.0, "text": "one", "panels": ["P001-01"], "cues": [], "emphasis": []},
        {"start": 5.0, "text": "two", "panels": ["P001-02"], "cues": [], "emphasis": []},
        {"start": 10.0, "text": "three", "panels": ["P001-03"], "cues": ["impact"], "emphasis": []},
    ]
    return build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "pf"},
                      duration=15.0, audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=sentences)


def test_build_plan_panelfit_mode():
    plan = _panelfit_plan()
    assert plan["mode"] == "panelfit"
    assert plan["panelfit_static"] == []
    specs = {c["panel"]: c["panelfit"] for s in plan["shots"] for c in s["cards"]}
    assert specs["P001-01"]["kind"] == "zoom" and specs["P001-01"]["zoom_rate"] == 0.05
    assert specs["P001-02"]["kind"] == "drift"
    assert specs["P001-03"]["kind"] == "fill" and specs["P001-03"]["bg"] is True
    assert all(s.get("panelfit") is True and s["caption_bar"] is False for s in plan["shots"])
    # No burned-in captions in this mode, even with captions on globally.
    assert plan["captions"] == [] and plan["captions_enabled"] is False
    impact = {e["id"]: e for e in plan["panelfit_impact"]}
    assert impact, "the {impact} sentence's shot must be flagged impact"
    assert all(e["rule"] == "marker" and e["zoom_rate"] == 0.088 for e in impact.values())
    hit = next(s for s in plan["shots"] if s["id"] in impact)
    assert hit["cards"][0]["panelfit"]["zoom_rate"] == 0.088
    assert validate(plan, {p["id"] for p in PANELS}) == []


def test_build_plan_rhythm_split_and_caps():
    vcfg = {**VCFG, "mode": "panelfit"}
    sentences = [
        {"start": 0.0, "text": "a long opening sentence", "panels": ["P001-01"],
         "cues": [], "emphasis": []},
        {"start": 5.0, "text": "short", "panels": ["P001-02"], "cues": [], "emphasis": []},
    ]
    plan = build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "pf"},
                      duration=6.5, audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=sentences)
    durs = [round(float(s["end"]) - float(s["start"]), 3) for s in plan["shots"]]
    assert len(durs) >= 3, "the 5s sentence must split into two shots"
    assert all(d >= plan_mod.PF_SPLIT_MIN_PIECE - 1e-9 for d in durs)
    # A sub-3s shot is legal in this mode (floor 0.3s, not classic 3.0s);
    # the 1.5s sentence's shot runs 1.65s (it opens `lead` early).
    assert min(durs) < 3.0
    assert plan["pacing"]["min_shot"] == 0.3
    assert plan["pacing"]["max_shots"] == int(6.5 // 1.0)
    assert validate(plan, {p["id"] for p in PANELS}) == []


SEG_PANELS = [
    {"id": "P001-01", "width": 900, "height": 1200, "text": ""},
    {"id": "P001-02", "width": 900, "height": 2000, "text": "",
     "gutter_segments": [[0.0, 0.4], [0.4, 1.0]]},
    {"id": "P001-03", "width": 900, "height": 2000, "text": "",
     "gutter_segments": [[0.0, 0.34], [0.34, 0.67], [0.67, 1.0]]},
]


def _seg_plan(pcfg_extra=None):
    vcfg = {**VCFG, "mode": "panelfit"}
    if pcfg_extra:
        vcfg = {**vcfg, "panelfit": {**(VCFG.get("panelfit") or {}), **pcfg_extra}}
    sentences = [
        {"start": 0.0, "text": "before", "panels": ["P001-01"], "cues": [], "emphasis": []},
        {"start": 5.0, "text": "the feather floated down", "panels": ["P001-02"],
         "cues": [], "emphasis": []},
        {"start": 7.9, "text": "after", "panels": ["P001-01"], "cues": [], "emphasis": []},
    ]
    return build_plan(chapter="1", panels_data={"panels": SEG_PANELS, "fingerprint": "seg"},
                      duration=12.9, audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=sentences)


def test_build_plan_splits_a_shot_at_its_gutter_segments():
    # Analysis fix 1: the stacked panel's shot becomes one sub-shot
    # per segment, sequenced inside the shot's own time, durations
    # proportional to the segment heights.
    plan = _seg_plan()
    seg_shots = [s for s in plan["shots"] if any(c.get("slice") for c in s["cards"])]
    assert len(seg_shots) == 2
    assert [s["cards"][0]["slice"] for s in seg_shots] == [[0.0, 0.4], [0.4, 1.0]]
    d0 = float(seg_shots[0]["end"]) - float(seg_shots[0]["start"])
    d1 = float(seg_shots[1]["end"]) - float(seg_shots[1]["start"])
    assert d0 / (d0 + d1) == pytest.approx(0.4, abs=0.02)
    # The pieces tile exactly the span the unsplit shot would have run.
    control = _seg_plan({"gutter_segments": False})
    plain = [s for s in control["shots"] if s["cards"][0]["panel"] == "P001-02"]
    assert len(plain) == 1
    assert float(seg_shots[0]["start"]) == pytest.approx(float(plain[0]["start"]))
    assert float(seg_shots[-1]["end"]) == pytest.approx(float(plain[0]["end"]))
    assert plan["panelfit_segments"] == ["P001-02"]
    # The spec is fitted to the SEGMENT's height (2000 x 0.4 = 800),
    # not the whole panel's: a 900x800 picture, height-fitted.
    spec0 = seg_shots[0]["cards"][0]["panelfit"]
    assert spec0["base_h"] == pytest.approx(1080.0)
    assert spec0["base_w"] == pytest.approx(900 / 800 * 1080)
    assert validate(plan, {p["id"] for p in SEG_PANELS}) == []
    ids = [s["id"] for s in plan["shots"]]
    assert ids == [f"S{i + 1:03d}" for i in range(len(ids))]


def test_build_plan_keeps_a_too_short_shot_whole():
    # Three segments need 3 x 0.3s of floor; a ~0.85s shot stays whole.
    vcfg = {**VCFG, "mode": "panelfit"}
    sentences = [
        {"start": 0.0, "text": "quick", "panels": ["P001-03"], "cues": [], "emphasis": []},
        {"start": 0.6, "text": "next", "panels": ["P001-01"], "cues": [], "emphasis": []},
    ]
    plan = build_plan(chapter="1", panels_data={"panels": SEG_PANELS, "fingerprint": "seg"},
                      duration=4.0, audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=sentences)
    assert not any(c.get("slice") for s in plan["shots"] for c in s["cards"])
    assert plan["panelfit_segments"] == []


def test_qa_judges_a_segment_card_against_its_segment():
    # Analysis fix 1: a sliced card's spec is fitted to the segment
    # band, so QA must judge it against the SEGMENT's aspect — and
    # the very same spec on the unsliced card must still fail.
    panels = {"panels": [{"id": "P001-01", "width": 900, "height": 2000, "text": ""}]}
    spec = dict(panelfit_spec(900, 800, "push_in", FMT, {}))
    card = {"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
            "motion": "push_in", "panelfit": spec, "slice": [0.0, 0.4]}
    plan = {"chapter": "1", "format": "16:9", "fps": 30, "duration": 4.0,
            "shots": [{"id": "S001", "start": 0.0, "end": 4.0, "layout": "single",
                       "background": "blur", "cards": [card]}]}
    qa = build_mod.quality_report(plan, panels, None, 3.0)
    assert not any("panel-fit" in f for f in qa["failures"])
    bare = {k: v for k, v in card.items() if k != "slice"}
    plan2 = {**plan, "shots": [{**plan["shots"][0], "cards": [bare]}]}
    qa2 = build_mod.quality_report(plan2, panels, None, 3.0)
    assert any("panel-fit" in f for f in qa2["failures"])


def test_qa_flags_a_stretched_foreground():
    plan = _panelfit_plan()
    panels = {"panels": PANELS}
    qa = build_mod.quality_report(plan, panels, None, 3.0)
    assert not any("panel-fit" in f for f in qa["failures"])
    assert qa["panelfit_static"] == []
    for s in plan["shots"]:
        for c in s["cards"]:
            c["panelfit"] = {**c["panelfit"], "base_w": c["panelfit"]["base_w"] * 2}
    qa = build_mod.quality_report(plan, panels, None, 3.0)
    assert any("panel-fit" in f for f in qa["failures"])


def test_qa_flags_a_drift_oversize_beyond_the_band():
    plan = _panelfit_plan()
    panels = {"panels": PANELS}
    for s in plan["shots"]:
        for c in s["cards"]:
            if c["panelfit"]["kind"] == "drift":
                c["panelfit"] = {**c["panelfit"], "base_h": FMT.height * 1.5,
                                 "base_w": FMT.height * 1.5 * (900 / 2400)}
    qa = build_mod.quality_report(plan, panels, None, 3.0)
    assert any("panel-fit" in f for f in qa["failures"])


# --------------------------------------------------------------- sync gate

def _seg(n, start, end, tags):
    sel = tags[0] if tags else None
    return {"sentence": n, "text": f"sentence {n}", "start": float(start), "end": float(end),
            "tagged_panels": list(tags), "display_action": "SHOW",
            "selected_panel": sel, "selected_by_align": sel,
            "sequence": [sel] if sel else []}


def _pf_shot(i, start, end, pid):
    return {"id": f"S{i:03d}", "start": start, "end": end, "layout": "single",
            "background": "blur", "exit": "cut", "effects": [], "sentence": None,
            "cards": [{"panel": pid, "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                       "motion": "push_in", "panelfit": panelfit_spec(900, 1200, "push_in", FMT, {})}]}


def test_sync_gate_uses_the_mode_floor_for_panelfit_plans():
    segs = [_seg(1, 0.0, 6.0, ["P1"])]
    shots = [_pf_shot(1, 0.0, 1.5, "P1"), _pf_shot(2, 1.5, 6.0, "P1")]
    plan = {"chapter": "1", "duration": 6.0, "mode": "panelfit", "shots": shots}
    rep = syncaudit.audit(plan, {"segments": segs}, [])
    assert rep["min_shot"] == 0.3 and rep["short_shots"] == []
    assert rep["gate"]["pass"] is True
    assert syncaudit.gate_from_report(rep)["pass"] is True
    # The same plan as a classic-mode plan fails the 3.0s floor.
    rep2 = syncaudit.audit({**plan, "mode": "classic"}, {"segments": segs}, [])
    assert rep2["short_shots"] == [{"shot": "S001", "duration": 1.5}]
    assert rep2["gate"]["pass"] is False


# ------------------------------------------------- B1d: span motion (F1/F2/F3/F5)

def _one_sentence_plan(pid, seconds, text="a very long sentence that must split",
                       panels=None):
    vcfg = {**VCFG, "mode": "panelfit"}
    sentences = [{"start": 0.0, "text": text, "panels": [pid], "cues": [], "emphasis": []}]
    return build_plan(chapter="1",
                      panels_data={"panels": panels or PANELS, "fingerprint": "pf"},
                      duration=seconds, audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=sentences)


def test_split_sentence_drift_is_one_continuous_span():
    # F1: a 7.7s sentence on a narrow panel splits into pieces; the
    # pieces must slice ONE drift — same direction throughout, each
    # piece opening exactly where the previous one ended.
    plan = _one_sentence_plan("P001-02", 7.7)
    shots = plan["shots"]
    assert len(shots) >= 3
    specs = [s["cards"][0]["panelfit"] for s in shots]
    assert all(sp["kind"] == "drift" for sp in specs)
    assert len({sp["dir"] for sp in specs}) == 1            # never a reversal
    assert len({sp["drift_rate"] for sp in specs}) == 1    # one constant speed
    for a, b in zip(specs, specs[1:]):
        end_a = a["drift_from"] + a["drift_frac"]
        assert b["drift_from"] == pytest.approx(end_a, abs=1e-3)   # seam-exact (±1 frame)
    # The span's whole travel is the pinned speed x span, capped at
    # 13% — the later pieces sit in the cap hold, reported as such.
    assert sum(sp["drift_frac"] for sp in specs) == pytest.approx(
        plan_mod.PF_DRIFT_MAX_TRAVEL * (1 if specs[0]["dir"] == "down" else -1), abs=0.01)
    assert plan["panelfit_static"] == []
    assert shots[0]["id"] not in plan["panelfit_hold"]
    assert {s["id"] for s in shots[1:]} <= set(plan["panelfit_hold"])


def test_split_sentence_zoom_is_one_continuous_span():
    # F1 for zooms: piece 2 opens at the scale piece 1 reached
    # (zoom_start), and every piece shares the span's zoom_end.
    plan = _one_sentence_plan("P001-01", 7.7)
    shots = plan["shots"]
    assert len(shots) >= 3
    specs = [s["cards"][0]["panelfit"] for s in shots]
    assert all(sp["kind"] == "zoom" for sp in specs)
    assert specs[0]["zoom_start"] == pytest.approx(1.0)
    assert len({sp["zoom_end"] for sp in specs}) == 1
    elapsed = 0.0
    for s, sp in zip(shots, specs):
        assert sp["zoom_start"] == pytest.approx(
            min(1.0 + sp["zoom_rate"] * elapsed, sp["zoom_end"]), abs=2e-3)
        elapsed += float(s["end"]) - float(s["start"])


def test_consecutive_sentences_on_one_panel_keep_its_drift_direction():
    # F1: a panel's established drift direction persists into the next
    # sentence on the same panel; a fresh panel alternates instead.
    panels = PANELS + [{"id": "P001-04", "width": 800, "height": 2000, "text": ""}]
    vcfg = {**VCFG, "mode": "panelfit"}
    sentences = [
        {"start": 0.0, "text": "first", "panels": ["P001-02"], "cues": [], "emphasis": []},
        {"start": 4.0, "text": "second", "panels": ["P001-02"], "cues": [], "emphasis": []},
        {"start": 8.0, "text": "third", "panels": ["P001-04"], "cues": [], "emphasis": []},
    ]
    plan = build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "pf"},
                      duration=12.0, audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=sentences)
    dirs = {}
    for s in plan["shots"]:
        c = s["cards"][0]
        if c["panelfit"]["kind"] == "drift":
            dirs.setdefault(c["panel"], set()).add(c["panelfit"]["dir"])
    assert len(dirs["P001-02"]) == 1                        # never flips mid-panel
    assert dirs["P001-02"] != dirs["P001-04"]               # alternation still lives


def test_no_panelfit_shot_carries_horizontal_motion():
    # F3: the mode's motion grammar is vertical drift + zoom-in only —
    # no spec may encode a horizontal pan, in any plan the pass writes.
    plan = _panelfit_plan()
    for s in plan["shots"]:
        for c in s["cards"]:
            sp = c["panelfit"]
            assert sp["kind"] in ("drift", "zoom", "fill")
            assert sp["dir"] in ("down", "up", "in")
    # Renderer level: on a drift shot a marker's x is pinned while its
    # y travels at the pinned speed — the foreground never slides
    # sideways. Thin cross bars as markers: in the blurred background
    # they wash out to nothing, so the profiles read the sharp
    # foreground alone.
    img = Image.new("RGB", (900, 2400), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 1000, 899, 1008), fill=(255, 255, 255))   # horizontal bar
    d.rectangle((400, 0, 408, 2399), fill=(255, 255, 255))    # vertical bar
    spec = panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=4.0)
    r = _renderer(img, spec, end=4.0)

    def marker(arr):
        crop = arr[:, 700:1220].astype(float).mean(axis=2)
        return (700 + int(np.argmax(crop.mean(axis=0))),     # vertical bar's x
                int(np.argmax(crop.mean(axis=1))))           # horizontal bar's y

    x1, y1 = marker(r.frame_array(0.3))
    x2, y2 = marker(r.frame_array(1.5))
    assert x2 == pytest.approx(x1, abs=1)                    # no horizontal motion
    assert y2 - y1 == pytest.approx(plan_mod.PF_DRIFT_SPEED * 1080 * 1.2, abs=8)


def _sharpness(arr):
    g = np.asarray(arr, dtype=np.float64)
    return float(np.abs(np.diff(g, axis=1)).mean())


def test_split_pieces_first_frames_are_never_background_only():
    # F5 regression: plan times are rounded to 3 decimals but frames
    # are drawn on the f/fps grid — piece 2 of this split starts at
    # 2.567s while its first frame is drawn at 77/30 = 2.5667s. That
    # frame used to render as the blurred background alone (measured
    # sharpness ~0.5% of its neighbours in the B1c sample).
    plan = _one_sentence_plan("P001-02", 7.7)
    assert len(plan["shots"]) >= 3
    imgs = {p["id"]: _img_for(p) for p in PANELS}
    for s in plan["shots"]:
        r = ShotRenderer(s, imgs, FMT)
        f0 = int(round(float(s["start"]) * FMT.fps))
        first = _sharpness(r.frame_array(f0 / FMT.fps))
        second = _sharpness(r.frame_array((f0 + 1) / FMT.fps))
        assert first >= 0.5 * second, (s["id"], first, second)


def test_split_sentence_render_is_position_continuous_at_the_seam():
    # F1 at the renderer: across a mid-motion seam the foreground's
    # boundary moves by about one frame's worth of travel, in the SAME
    # direction — no jump back to the span start, no reversal.
    plan = _one_sentence_plan("P001-02", 3.4)
    shots = plan["shots"]
    assert len(shots) == 2
    img = Image.new("RGB", (900, 2400), (255, 0, 0))
    ImageDraw.Draw(img).rectangle((0, 1200, 900, 2399), fill=(0, 0, 255))
    imgs = {"P001-02": img}
    a, b = (ShotRenderer(s, imgs, FMT) for s in shots)
    fa = int(round(float(shots[0]["end"]) * FMT.fps))
    y_last_a = _boundary_y(a.frame_array((fa - 1) / FMT.fps))
    y_first_b = _boundary_y(b.frame_array(fa / FMT.fps))
    y_mid_b = _boundary_y(b.frame_array(float(shots[1]["start"]) + 0.2))
    assert y_first_b - y_last_a == pytest.approx(
        plan_mod.PF_DRIFT_SPEED * 1080 / FMT.fps, abs=3)
    assert y_mid_b > y_first_b                              # still the same direction


def test_validate_accepts_span_slices_and_rejects_opposing_drift():
    sp = panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=1.7,
                       span_dur=3.4, elapsed=1.7, direction="down")
    assert sp["drift_frac"] > 0
    assert validate(_plan_with_spec(sp), {"P001-01"}) == []
    hold = panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=1.7,
                         span_dur=7.7, elapsed=5.13, direction="down")
    assert hold["drift_frac"] == 0.0 and "drift_from" in hold   # a cap-hold slice
    assert validate(_plan_with_spec(hold), {"P001-01"}) == []
    bad = dict(sp)
    bad["drift_frac"] = -abs(sp["drift_frac"])                  # opposes its dir
    assert any("panelfit" in e for e in validate(_plan_with_spec(bad), {"P001-01"}))


def test_advance_slice_moves_a_drift_slice_to_the_span_position():
    sp = panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=1.7,
                       span_dur=3.4, elapsed=0.0, direction="down")
    adv = plan_mod.panelfit_advance_slice(sp, 0.88)
    want = plan_mod.panelfit_drift_offset(plan_mod.panelfit_drift_travel(3.4), 0.88, "down")
    assert adv["drift_from"] == pytest.approx(want, abs=1e-3)
    assert adv["drift_frac"] == pytest.approx(sp["drift_frac"] - (want - sp["drift_from"]),
                                              abs=1e-3)
    # A hold slice stays held; a zoom slice opens mid-zoom, capped.
    hold = panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=1.7,
                         span_dur=7.7, elapsed=5.13, direction="down")
    adv_hold = plan_mod.panelfit_advance_slice(hold, 1.0)
    assert adv_hold["drift_from"] == hold["drift_from"] and adv_hold["drift_frac"] == 0.0
    zoom = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=2.0,
                         span_dur=10.0, elapsed=2.0)
    adv_zoom = plan_mod.panelfit_advance_slice(zoom, 3.0)
    assert adv_zoom["zoom_start"] == pytest.approx(
        min(zoom["zoom_start"] + zoom["zoom_rate"] * 3.0, zoom["zoom_end"]), abs=1e-3)


def test_clip_starting_mid_span_keeps_the_motion_phase():
    # F1 on the clip path (build._clip_plan): a clip that opens inside
    # a split sentence must not restart the span's motion on its first
    # piece — measured on the B1d proof sample as a 65px jump at the
    # first seam before this fix.
    plan = _one_sentence_plan("P001-02", 3.4)
    assert len(plan["shots"]) == 2
    clip = build_mod._clip_plan(plan, 0.88, 3.4, "16:9")
    shots = clip["shots"]
    assert len(shots) == 2
    img = Image.new("RGB", (900, 2400), (255, 0, 0))
    ImageDraw.Draw(img).rectangle((0, 1200, 900, 2399), fill=(0, 0, 255))
    imgs = {"P001-02": img}
    a, b = (ShotRenderer(s, imgs, FMT) for s in shots)
    seam_frame = int(round(float(shots[1]["start"]) * FMT.fps))
    y_last_a = _boundary_y(a.frame_array((seam_frame - 1) / FMT.fps))
    y_first_b = _boundary_y(b.frame_array(seam_frame / FMT.fps))
    assert y_first_b - y_last_a == pytest.approx(
        plan_mod.PF_DRIFT_SPEED * 1080 / FMT.fps, abs=3)
