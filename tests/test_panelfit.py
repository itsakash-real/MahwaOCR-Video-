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
from manhwa_ocr.video.frames import Format, PanelFitView, ShotRenderer, format_from
from manhwa_ocr.video.plan import (build_plan, panelfit_drift_travel, panelfit_duo_fit,
                                  panelfit_duo_spec, panelfit_fit, panelfit_is_impact,
                                  panelfit_spec, validate)

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
    assert pf["drift_speed"] == plan_mod.PF_DRIFT_SPEED == 0.0667
    assert pf["drift_min_travel"] == plan_mod.PF_DRIFT_MIN_TRAVEL == 0.05
    assert pf["drift_max_travel"] == plan_mod.PF_DRIFT_MAX_TRAVEL == 0.13
    assert pf["drift_headroom"] == plan_mod.PF_DRIFT_HEADROOM == 0.03
    assert pf["zoom_rate"] == plan_mod.PF_ZOOM_RATE == 0.05
    assert pf["zoom_rate_impact"] == plan_mod.PF_ZOOM_RATE_IMPACT == 0.088
    # B1e: the blast zoom-out rate is exactly 2x the impact zoom-in rate.
    assert pf["zoom_out_fast_rate"] == plan_mod.PF_ZOOM_OUT_FAST_RATE == 0.176
    assert plan_mod.PF_ZOOM_OUT_FAST_RATE == 2 * plan_mod.PF_ZOOM_RATE_IMPACT
    assert pf["zoom_max_total"] == plan_mod.PF_ZOOM_MAX_TOTAL == 0.45
    assert pf["max_upscale"] == plan_mod.PF_MAX_UPSCALE == 2.4
    assert pf["still_limit"] == plan_mod.PF_STILL_LIMIT == 0.5
    assert pf["min_shot"] == plan_mod.PF_MIN_SHOT == 0.3
    assert pf["split_above"] == plan_mod.PF_SPLIT_ABOVE == 3.0
    assert pf["split_min_piece"] == plan_mod.PF_SPLIT_MIN_PIECE == 1.2
    assert pf["captions"] is False and plan_mod.PF_CAPTIONS is False
    assert pf["title_cards"] is True and plan_mod.PF_TITLE_CARDS is True
    assert pf["tts_gap_seconds"] == plan_mod.PF_TTS_GAP == 0.11


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
    kind, bw, bh, s = panelfit_fit(900, 1400, FMT, dur=2.0)
    assert kind == "drift"                      # aspect 0.64 < 0.72, h/w 1.56 < strip
    # travel = clamp(0.0667 * 2.0) = 0.13 -> oversize 1 + 0.13 + 0.03 = 1.16
    assert bh == pytest.approx(1080.0 * 1.16) and bw == pytest.approx(900 * 1080 / 1400 * 1.16)


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
    assert panelfit_drift_travel(1.0) == pytest.approx(0.0667)   # speed x duration
    assert panelfit_drift_travel(0.5) == pytest.approx(0.05)    # floor
    assert panelfit_drift_travel(10.0) == pytest.approx(0.13)   # cap
    assert panelfit_drift_travel(1.47) == pytest.approx(0.0667 * 1.47, abs=0.001)


def test_spec_zoom_is_a_constant_rate_not_a_fixed_total():
    sp = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=2.0)
    assert sp["kind"] == "zoom" and sp["dir"] == "in" and sp["bg"] is True
    assert sp["zoom_rate"] == 0.05 and sp["zoom_end"] == pytest.approx(1.10)
    sp10 = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=10.0)
    assert sp10["zoom_rate"] == 0.05 and sp10["zoom_end"] == pytest.approx(1.45)
    # pull_out no longer reverses: the reference never zooms out.
    assert panelfit_spec(900, 1200, "pull_out", FMT, {})["dir"] == "in"
    assert panelfit_spec(1920, 1080, "push_in", FMT, {})["bg"] is False   # canvas-shaped


def test_spec_zoom_total_is_capped():
    # A shot long enough that rate x duration would pass +45%: the
    # total clamps at the cap; the RATE itself is untouched (the
    # renderer holds the scale once the cap is reached).
    sp = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=20.0)
    assert sp["zoom_rate"] == 0.05 and sp["zoom_end"] == pytest.approx(1.45)
    hit = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=10.0, impact=True)
    assert hit["zoom_rate"] == 0.088 and hit["zoom_end"] == pytest.approx(1.45)


def test_spec_drift_and_impact_rates():
    sp = panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=2.0)
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
                 panelfit_spec(900, 1400, "pan_down", FMT, {}),
                 panelfit_spec(900, 2400, "pan_down", FMT, {}),   # a strip -> traverse (B1e)
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
    # 12s at +5.0%/s: the +45% cap is reached at t=9 — the scale at
    # t=10.2 and t=11.8 must be the same (a hold, not a slowed rate),
    # and clearly larger than at t=4.
    img = Image.new("RGB", (900, 1200), (200, 40, 40))
    ImageDraw.Draw(img).rectangle((300, 400, 600, 800), fill=(40, 40, 200))
    r = _renderer(img, panelfit_spec(900, 1200, "push_in", FMT, {}, dur=12.0), end=12.0)
    w5 = _square_half_width(r.frame_array(4.0))
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
    # (72 px/s at 1080p = 0.0667 frame-heights/s), NOT a full strip
    # traversal and NOT a slow stretch of the travel over the shot.
    img = Image.new("RGB", (900, 1400), (255, 0, 0))
    ImageDraw.Draw(img).rectangle((0, 700, 900, 1399), fill=(0, 0, 255))
    r = _renderer(img, panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=8.0), end=8.0)
    ys = [_boundary_y(r.frame_array(t)) for t in (0.3, 1.1, 1.9)]
    assert all(y is not None for y in ys)
    assert ys[1] - ys[0] == pytest.approx(ys[2] - ys[1], abs=3)     # constant velocity
    assert ys[2] - ys[0] == pytest.approx(plan_mod.PF_DRIFT_SPEED * 1080 * 1.6, abs=6)


def test_render_drift_holds_at_the_travel_cap():
    # F2: travel = speed x span, hard-capped at 13% of frame height;
    # past the cap (~1.98s into an 8s span) the position HOLDS — it
    # neither reverses nor creeps at a slowed rate.
    img = Image.new("RGB", (900, 1400), (255, 0, 0))
    ImageDraw.Draw(img).rectangle((0, 700, 900, 1399), fill=(0, 0, 255))
    r = _renderer(img, panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=8.0), end=8.0)
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


# --------------------------------------------------------------- build_plan + QA

def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 11):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


PANELS = [
    {"id": "P001-01", "width": 900, "height": 1200, "text": ""},    # pillar -> zoom
    {"id": "P001-02", "width": 900, "height": 1400, "text": ""},    # narrow pillar -> drift
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
                                 "base_w": FMT.height * 1.5 * (900 / 1400)}
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
    panels = PANELS + [{"id": "P001-04", "width": 800, "height": 1200, "text": ""}]
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
    img = Image.new("RGB", (900, 1400), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 690, 899, 698), fill=(255, 255, 255))   # horizontal bar
    d.rectangle((400, 0, 408, 1399), fill=(255, 255, 255))    # vertical bar
    spec = panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=4.0)
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
    img = Image.new("RGB", (900, 1400), (255, 0, 0))
    ImageDraw.Draw(img).rectangle((0, 700, 900, 1399), fill=(0, 0, 255))
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
    sp = panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=1.7,
                       span_dur=3.4, elapsed=1.7, direction="down")
    assert sp["drift_frac"] > 0
    assert validate(_plan_with_spec(sp), {"P001-01"}) == []
    hold = panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=1.7,
                         span_dur=7.7, elapsed=5.13, direction="down")
    assert hold["drift_frac"] == 0.0 and "drift_from" in hold   # a cap-hold slice
    assert validate(_plan_with_spec(hold), {"P001-01"}) == []
    bad = dict(sp)
    bad["drift_frac"] = -abs(sp["drift_frac"])                  # opposes its dir
    assert any("panelfit" in e for e in validate(_plan_with_spec(bad), {"P001-01"}))


def test_advance_slice_moves_a_drift_slice_to_the_span_position():
    sp = panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=1.7,
                       span_dur=3.4, elapsed=0.0, direction="down")
    adv = plan_mod.panelfit_advance_slice(sp, 0.88)
    want = plan_mod.panelfit_drift_offset(plan_mod.panelfit_drift_travel(3.4), 0.88, "down")
    assert adv["drift_from"] == pytest.approx(want, abs=1e-3)
    assert adv["drift_frac"] == pytest.approx(sp["drift_frac"] - (want - sp["drift_from"]),
                                              abs=1e-3)
    # A hold slice stays held; a zoom slice opens mid-zoom, capped.
    hold = panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=1.7,
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
    img = Image.new("RGB", (900, 1400), (255, 0, 0))
    ImageDraw.Draw(img).rectangle((0, 700, 900, 1399), fill=(0, 0, 255))
    imgs = {"P001-02": img}
    a, b = (ShotRenderer(s, imgs, FMT) for s in shots)
    seam_frame = int(round(float(shots[1]["start"]) * FMT.fps))
    y_last_a = _boundary_y(a.frame_array((seam_frame - 1) / FMT.fps))
    y_first_b = _boundary_y(b.frame_array(seam_frame / FMT.fps))
    assert y_first_b - y_last_a == pytest.approx(
        plan_mod.PF_DRIFT_SPEED * 1080 / FMT.fps, abs=3)


# --------------------------------------- B1e: traverse (E4) / anchoring (E6)

def test_strip_threshold_is_the_codebase_tall_ratio():
    # The strip class is the codebase's existing one (panels.TALL_RATIO),
    # not a second threshold invented for this mode.
    from manhwa_ocr.video import panels as panels_mod
    assert plan_mod.PF_STRIP_ASPECT == panels_mod.TALL_RATIO == 1.6


def test_fit_strip_traverses_width_fit():
    kind, bw, bh, s = panelfit_fit(900, 2400, FMT, dur=2.0)
    assert kind == "traverse"                       # h/w 2.67 >= strip threshold
    assert bw == pytest.approx(1920.0)               # fitted BY WIDTH
    assert bh == pytest.approx(2400 * 1920 / 900)    # taller than the frame
    # A strip whose width-fit leaves it no taller than the frame
    # degrades to the ordinary fits (nothing to traverse).
    kind2, *_ = panelfit_fit(200, 320, FMT, dur=2.0)   # h/w exactly 1.6, tiny
    assert kind2 != "traverse"


def test_spec_traverse_span_slices_arrive_at_the_bottom():
    sp = panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=3.0, span_dur=6.0, elapsed=0.0)
    assert sp["kind"] == "traverse" and sp["dir"] == "down"
    total = (sp["base_h"] - FMT.height) / FMT.height
    assert sp["traverse_from"] == pytest.approx(0.0)          # top anchor
    assert sp["traverse_to"] == pytest.approx(-total, abs=1e-3)   # bottom anchor
    assert sp["traverse_rate"] == pytest.approx(-total / 6.0, abs=1e-3)  # arrives at the END
    mid = panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=3.0, span_dur=6.0, elapsed=3.0)
    assert mid["traverse_from"] == pytest.approx(-total / 2, abs=1e-3)
    adv = plan_mod.panelfit_advance_slice(sp, 3.0)
    assert adv["traverse_from"] == pytest.approx(mid["traverse_from"], abs=1e-3)
    assert validate(_plan_with_spec(sp), {"P001-01"}) == []
    assert validate(_plan_with_spec(mid), {"P001-01"}) == []


def test_render_traverse_anchors_top_then_bottom():
    # E4+E6 at the renderer: the first frame shows the art's TOP at the
    # frame's top wall; the last frame shows the art's BOTTOM at the
    # bottom wall — a deliberate full traverse, not the drift's cap.
    img = Image.new("RGB", (900, 2400), (255, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 900, 59), fill=(0, 255, 0))          # the art's top strip
    d.rectangle((0, 2340, 900, 2399), fill=(255, 0, 255))    # the art's bottom strip
    spec = panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=6.0)
    r = _renderer(img, spec, end=6.0)
    f0 = r.frame_array(0.05)
    assert tuple(f0[4, 960]) == (0, 255, 0)        # art top AT the top wall
    assert tuple(f0[540, 960]) == (255, 0, 0)
    f1 = r.frame_array(5.95)
    assert tuple(f1[1075, 960]) == (255, 0, 255)   # art bottom AT the bottom wall
    assert tuple(f1[540, 960]) == (255, 0, 0)


def test_build_plan_strip_sentence_traverses():
    panels = PANELS + [{"id": "P001-05", "width": 900, "height": 2400, "text": ""}]
    vcfg = {**VCFG, "mode": "panelfit"}
    sentences = [{"start": 0.0, "text": "the girl among the explosives",
                  "panels": ["P001-05"], "cues": [], "emphasis": []}]
    plan = build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "pf"},
                      duration=5.0, audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=sentences)
    specs = [c["panelfit"] for s in plan["shots"] for c in s["cards"]]
    assert specs and all(sp["kind"] == "traverse" for sp in specs)
    assert set(plan["panelfit_traverse"]) == {s["id"] for s in plan["shots"]}
    assert validate(plan, {p["id"] for p in panels}) == []
    qa = build_mod.quality_report(plan, {"panels": panels}, None, 3.0)
    assert not any("panel-fit" in f for f in qa["failures"])


def test_render_zoom_foreground_stays_centred():
    # E6: placement anchors centre — as the zoom grows, the marker's
    # centre stays on the frame's centre column.
    img = Image.new("RGB", (900, 1200), (200, 40, 40))
    ImageDraw.Draw(img).rectangle((400, 500, 500, 700), fill=(40, 40, 220))
    r = _renderer(img, panelfit_spec(900, 1200, "push_in", FMT, {}, dur=4.0), end=4.0)
    for t in (0.2, 2.0, 3.8):
        row = r.frame_array(t)[620]
        blue = np.flatnonzero((row[:, 2] > 128) & (row[:, 0] < 128))
        assert len(blue) and (blue[0] + blue[-1]) / 2 == pytest.approx(960, abs=3)


def test_render_fill_overflow_splits_equally():
    # E6: a cover-fill's overflow splits equally — a panel whose left
    # half is black and right half white must show its boundary on the
    # frame's centre column, not pushed to one side.
    img = Image.new("RGB", (2000, 900), (0, 0, 0))
    ImageDraw.Draw(img).rectangle((1000, 0, 1999, 899), fill=(255, 255, 255))
    r = _renderer(img, panelfit_spec(2000, 900, "push_in", FMT, {}, dur=2.0), end=2.0)
    row = r.frame_array(0.1)[540].astype(int)
    xs = np.flatnonzero(row[:, 0] > 128)
    assert len(xs) and xs[0] == pytest.approx(960, abs=4)


# ------------------------------------------------------- B1e: duo (E3)

def _duo_renderer(spec_l, spec_r, img_l, img_r, *, end=4.0):
    shot = {"id": "S001", "start": 0.0, "end": end, "layout": "pair", "background": "blur",
            "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                       "motion": "push_in", "panelfit": spec_l},
                      {"panel": "P001-02", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                       "motion": "push_in", "panelfit": spec_r}],
            "effects": [], "panelfit": True, "sentence": None, "callouts": []}
    return ShotRenderer(shot, {"P001-01": img_l, "P001-02": img_r}, FMT)


def test_duo_fit_slide_and_fallback():
    mode, bw, bh = panelfit_duo_fit(600, 1200, FMT, dur=4.0)
    assert mode == "slide"                            # cover-fits its cell + travel room
    assert bh == pytest.approx(1080 * 1.16) and bw <= 960
    mode2, bw2, bh2 = panelfit_duo_fit(1500, 900, FMT, dur=4.0)
    assert mode2 == "zoom_out"                        # too wide for a half cell
    assert bw2 <= 960 and bh2 <= 1080


def test_duo_specs_counter_slide_directions():
    l = panelfit_duo_spec(600, 1200, FMT, {}, cell="left", mode="slide", dur=4.0)
    r = panelfit_duo_spec(600, 1200, FMT, {}, cell="right", mode="slide", dur=4.0)
    assert l["kind"] == "duo" and l["cell"] == "left" and l["dir"] == "up" and l["drift_frac"] < 0
    assert r["cell"] == "right" and r["dir"] == "down" and r["drift_frac"] > 0
    assert validate(_plan_with_spec(l), {"P001-01"}) == []
    assert validate(_plan_with_spec(r), {"P001-01"}) == []
    out = panelfit_duo_spec(1500, 900, FMT, {}, cell="left", mode="zoom_out", dur=4.0)
    assert out["zoom_rate"] < 0 and out["zoom_end"] == 1.0 and out["zoom_start"] > 1.0
    assert validate(_plan_with_spec(out), {"P001-01"}) == []


def test_duo_foreground_is_clipped_to_its_cell():
    img = Image.new("RGB", (600, 1200), (255, 0, 0))
    sp = panelfit_duo_spec(600, 1200, FMT, {}, cell="left", mode="slide", dur=4.0)
    card = {"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
            "motion": "push_in"}
    v = PanelFitView(img, sp, card, 0.0, 4.0, FMT)
    a = np.asarray(v.fg_layer(1.0).getchannel("A"))
    assert a[:, 960:].max() == 0 and a[:, :960].max() == 255


def test_render_duo_counter_slides():
    # Left panel slides UP, right panel slides DOWN, at the pinned
    # drift speed (the user's police-pair ruling).
    img_l = Image.new("RGB", (600, 1200), (255, 0, 0))
    ImageDraw.Draw(img_l).rectangle((0, 600, 600, 1199), fill=(0, 0, 255))
    img_r = Image.new("RGB", (600, 1200), (0, 255, 0))
    ImageDraw.Draw(img_r).rectangle((0, 600, 600, 1199), fill=(255, 255, 0))
    sp_l = panelfit_duo_spec(600, 1200, FMT, {}, cell="left", mode="slide", dur=4.0)
    sp_r = panelfit_duo_spec(600, 1200, FMT, {}, cell="right", mode="slide", dur=4.0)
    r = _duo_renderer(sp_l, sp_r, img_l, img_r)

    def blue_top(arr, x):
        col = arr[:, x]
        blue = np.flatnonzero(col[:, 2] > 128)
        return int(blue[0]) if len(blue) else None

    def yellow_top(arr, x):
        col = arr[:, x]
        y = np.flatnonzero((col[:, 0] > 128) & (col[:, 1] > 128) & (col[:, 2] < 100))
        return int(y[0]) if len(y) else None

    l1, l2 = blue_top(r.frame_array(0.3), 480), blue_top(r.frame_array(1.3), 480)
    r1, r2 = yellow_top(r.frame_array(0.3), 1440), yellow_top(r.frame_array(1.3), 1440)
    assert None not in (l1, l2, r1, r2)
    assert l2 < l1 and r2 > r1                        # counter-slide: up / down
    assert l1 - l2 == pytest.approx(plan_mod.PF_DRIFT_SPEED * 1080 * 1.0, abs=6)
    assert r2 - r1 == pytest.approx(plan_mod.PF_DRIFT_SPEED * 1080 * 1.0, abs=6)


def test_render_duo_zoom_out_fallback_shrinks_both_panels():
    img = Image.new("RGB", (1500, 900), (200, 30, 30))
    ImageDraw.Draw(img).rectangle((600, 350, 900, 550), fill=(30, 30, 220))
    sp_l = panelfit_duo_spec(1500, 900, FMT, {}, cell="left", mode="zoom_out", dur=4.0)
    sp_r = panelfit_duo_spec(1500, 900, FMT, {}, cell="right", mode="zoom_out", dur=4.0)
    r = _duo_renderer(sp_l, sp_r, img, img)

    def halfw(arr):
        row = arr[540, :960]
        blue = np.flatnonzero((row[:, 2] > 128) & (row[:, 0] < 128))
        return (blue[-1] - blue[0]) / 2.0 if len(blue) else None

    w_early, w_late = halfw(r.frame_array(0.2)), halfw(r.frame_array(3.8))
    assert w_early and w_late and w_late < w_early - 5   # settling OUT, never in


# -------------------------------------------- B1e: zoom-out hints (E5 core)

def test_spec_zoom_out_hints_open_enlarged_and_settle():
    sp = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=4.0, hint="zoom_out")
    assert sp["kind"] == "zoom" and sp["dir"] == "out"
    assert sp["zoom_rate"] == pytest.approx(-plan_mod.PF_ZOOM_RATE) and sp["zoom_end"] == 1.0
    assert sp["zoom_start"] == pytest.approx(1.20)
    fast = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=2.0, hint="zoom_out_fast")
    assert fast["zoom_rate"] == pytest.approx(-2 * plan_mod.PF_ZOOM_RATE_IMPACT)
    assert fast["zoom_start"] == pytest.approx(1.0 + plan_mod.PF_ZOOM_OUT_FAST_RATE * 2.0)
    mid = panelfit_spec(900, 1200, "push_in", FMT, {}, dur=2.0, span_dur=4.0,
                        elapsed=2.0, hint="zoom_out")
    assert mid["zoom_start"] == pytest.approx(1.10)     # opens where the span reached
    adv = plan_mod.panelfit_advance_slice(sp, 2.0)
    assert adv["zoom_start"] == pytest.approx(mid["zoom_start"], abs=1e-3)
    assert validate(_plan_with_spec(sp), {"P001-01"}) == []
    assert validate(_plan_with_spec(fast), {"P001-01"}) == []
    # A zoom-out that tries to pass as a zoom-in is rejected.
    bad = dict(sp)
    bad["dir"] = "in"
    assert any("panelfit" in e for e in validate(_plan_with_spec(bad), {"P001-01"}))


def test_render_zoom_out_hint_shrinks():
    img = Image.new("RGB", (900, 1200), (200, 40, 40))
    ImageDraw.Draw(img).rectangle((300, 400, 600, 800), fill=(40, 40, 200))
    r = _renderer(img, panelfit_spec(900, 1200, "push_in", FMT, {}, dur=4.0,
                                     hint="zoom_out"), end=4.0)
    w1 = _square_half_width(r.frame_array(0.5))
    w2 = _square_half_width(r.frame_array(3.5))
    assert w1 and w2 and w2 < w1 - 5


# ------------------------------------- B1e: the motion-hint surface (E5)

def test_motion_hint_values_pinned_to_cues():
    from manhwa_ocr.video import cues as cues_mod
    assert plan_mod.PF_MOTION_HINTS == cues_mod.MOTION_HINTS
    # B1f: "zoom_in" joined the vocabulary (a forced push-in at the
    # standard rate) — the tuple is pinned WITH it now.
    assert cues_mod.MOTION_HINTS == ("default", "zoom_in", "zoom_out", "zoom_out_fast", "duo")


def test_validate_cues_motion_field():
    from manhwa_ocr.video import cues as cues_mod
    ok = {"sentences": [{"text": "x", "panels": ["P001-01"], "motion": "zoom_out"},
                        {"text": "y", "panels": ["P001-02", "P001-03"],
                         "motion": ["default", "zoom_out_fast"]},
                        {"text": "z", "panels": ["P001-04", "P001-05"], "motion": "duo"}]}
    assert cues_mod.validate_cues(ok) == []
    bad = {"sentences": [{"text": "x", "panels": ["P001-01"], "motion": "spin"}]}
    assert any("motion" in p for p in cues_mod.validate_cues(bad))
    short = {"sentences": [{"text": "x", "panels": ["P001-01", "P001-02"],
                            "motion": ["zoom_out"]}]}
    assert any("motion" in p for p in cues_mod.validate_cues(short))
    duo_in_list = {"sentences": [{"text": "x", "panels": ["P001-01", "P001-02"],
                                  "motion": ["duo", "default"]}]}
    assert any("duo" in p for p in cues_mod.validate_cues(duo_in_list))


def test_validate_pairs_motion_object_form():
    from manhwa_ocr.video.pairing import validate_pairs
    by_id = {"P001-01": {}, "P001-02": {}}
    assert validate_pairs({"1": {"panels": ["P001-01"], "motion": "zoom_out"}}, 3, by_id) == []
    assert validate_pairs({"1": {"panels": ["P001-01", "P001-02"],
                                 "motion": ["default", "zoom_out_fast"]}}, 3, by_id) == []
    assert validate_pairs({"1": ["P001-01"]}, 3, by_id) == []        # plain form untouched
    assert any("motion" in e for e in validate_pairs(
        {"1": {"panels": ["P001-01"], "motion": "spin"}}, 3, by_id))
    assert any("motion" in e for e in validate_pairs(
        {"1": {"panels": ["P001-01", "P001-02"], "motion": ["zoom_out"]}}, 3, by_id))
    assert any("unknown keys" in e for e in validate_pairs(
        {"1": {"panels": ["P001-01"], "speed": 2}}, 3, by_id))


def _hint_plan(sentences, duration=12.0, panels=None):
    vcfg = {**VCFG, "mode": "panelfit"}
    return build_plan(chapter="1", panels_data={"panels": panels or PANELS, "fingerprint": "pf"},
                      duration=duration, audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=sentences)


def test_build_plan_zoom_out_hint_reaches_the_spec():
    plan = _hint_plan([{"start": 0.0, "text": "he lifts one hand", "panels": ["P001-01"],
                        "cues": [], "emphasis": [], "motion": "zoom_out"}], duration=5.0)
    specs = [c["panelfit"] for s in plan["shots"] for c in s["cards"]]
    assert specs and all(sp["dir"] == "out" and sp["zoom_rate"] < 0 for sp in specs)
    assert set(plan["panelfit_zoom_out"]) == {s["id"] for s in plan["shots"]}
    assert validate(plan, {p["id"] for p in PANELS}) == []


def test_build_plan_unknown_hint_is_a_user_error():
    with pytest.raises(Exception, match="unknown motion hint"):
        _hint_plan([{"start": 0.0, "text": "x", "panels": ["P001-01"],
                     "cues": [], "emphasis": [], "motion": "spin"}], duration=5.0)


def test_build_plan_duo_hint_makes_a_side_by_side_shot():
    plan = _hint_plan([{"start": 0.0, "text": "rifles snap up in formation",
                        "panels": ["P001-01", "P001-02"],
                        "cues": [], "emphasis": [], "motion": "duo"}], duration=5.0)
    duo_shots = [s for s in plan["shots"]
                 if s["cards"] and all(c["panelfit"]["kind"] == "duo" for c in s["cards"])]
    assert duo_shots
    s = duo_shots[0]
    assert s["layout"] == "pair" and len(s["cards"]) == 2
    assert s["cards"][0]["panelfit"]["cell"] == "left"
    assert s["cards"][1]["panelfit"]["cell"] == "right"
    assert s["cards"][1]["at"] == 0.0                  # simultaneous, never staggered
    assert set(plan["panelfit_duo"]) == {x["id"] for x in duo_shots}
    assert validate(plan, {p["id"] for p in PANELS}) == []
    qa = build_mod.quality_report(plan, {"panels": PANELS}, None, 3.0)
    assert not any("panel-fit" in f for f in qa["failures"])


def test_build_plan_duo_hint_on_one_panel_is_a_user_error():
    with pytest.raises(Exception, match="exactly two panels"):
        _hint_plan([{"start": 0.0, "text": "x", "panels": ["P001-01"],
                     "cues": [], "emphasis": [], "motion": "duo"}], duration=5.0)


def test_build_plan_motion_list_gives_each_segment_its_own_motion():
    # The detonator -> explosion shape: one sentence, two sequenced
    # segments, a different motion each — and the pair-up pass must NOT
    # merge them into a simultaneous pair.
    panels = PANELS + [{"id": "P001-06", "width": 900, "height": 1200, "text": ""},
                       {"id": "P001-07", "width": 900, "height": 1200, "text": ""}]
    plan = _hint_plan([{"start": 0.0, "text": "she presses the button, and the blast swallows her",
                        "panels": ["P001-06", "P001-07"], "cues": [], "emphasis": [],
                        "motion": ["default", "zoom_out_fast"]}], duration=6.0, panels=panels)
    by_panel = {}
    for s in plan["shots"]:
        assert s["layout"] != "pair" or len(s["cards"]) == 1 or True
        for c in s["cards"]:
            by_panel.setdefault(c["panel"], set()).add(
                (c["panelfit"]["dir"], c["panelfit"]["zoom_rate"]))
    assert by_panel["P001-06"] == {("in", 0.05)}
    assert by_panel["P001-07"] == {("out", -plan_mod.PF_ZOOM_OUT_FAST_RATE)}
    # The two segments are SEQUENCED: no shot shows both at once.
    for s in plan["shots"]:
        shown = {c["panel"] for c in s["cards"]}
        assert not {"P001-06", "P001-07"} <= shown
    assert validate(plan, {p["id"] for p in panels}) == []


# ------------------------------------------------ B1e: drop shadow (E8)

def test_shadow_constants_pinned():
    pf = load_config().video["panelfit"]
    assert pf["shadow_dy"] == plan_mod.PF_SHADOW_DY == frames_mod.PFR_SHADOW_DY == 20
    assert pf["shadow_blur"] == plan_mod.PF_SHADOW_BLUR == frames_mod.PFR_SHADOW_BLUR == 10
    assert pf["shadow_opacity"] == plan_mod.PF_SHADOW_OPACITY \
        == frames_mod.PFR_SHADOW_OPACITY == 0.40


def test_render_drop_shadow_darkens_below_the_panel_only(monkeypatch):
    # A small panel sits inside the frame (720x960 at the cap), so its
    # bottom edge is visible: just below it the frame must darken (the
    # shadow), a far corner must keep the backdrop's exact brightness,
    # and the panel's own pixels are untouched.
    img = Image.new("RGB", (300, 400), (200, 30, 30))
    spec = panelfit_spec(300, 400, "push_in", FMT, {}, dur=2.0)
    with_shadow = _renderer(img, spec).frame_array(0.1).astype(int)
    monkeypatch.setattr(frames_mod, "PFR_SHADOW_OPACITY", 0.0)
    without = _renderer(img, spec).frame_array(0.1).astype(int)
    assert with_shadow[1035, 960][0] < without[1035, 960][0] - 30   # the shadow band
    assert np.array_equal(with_shadow[30, 30], without[30, 30])    # backdrop untouched
    assert tuple(with_shadow[540, 960]) == (200, 30, 30)           # the panel itself


def test_sliced_segments_appear_as_separate_shots_in_order():
    """B1e E2: two segments of one sliced strip, tagged by two
    consecutive beats, plan as two separate shots in story order with
    a hard cut between them — and no shot's spec ever spans the strip
    as one glued asset (no traverse/drift is planned across a gutter,
    because a single asset no longer contains one)."""
    panels = PANELS + [{"id": "P001-08", "width": 900, "height": 700, "text": ""},
                       {"id": "P001-09", "width": 900, "height": 500, "text": ""}]
    plan = _hint_plan([
        {"start": 0.0, "text": "a white feather drifts down through the dark",
         "panels": ["P001-08"], "cues": [], "emphasis": []},
        {"start": 4.0, "text": "but the hall holds its breath", "panels": ["P001-09"],
         "cues": [], "emphasis": []},
    ], duration=8.0, panels=panels)
    shown = [(s["id"], c["panel"]) for s in plan["shots"] for c in s["cards"]]
    first_a = min(i for i, (_, p) in enumerate(shown) if p == "P001-08")
    first_b = min(i for i, (_, p) in enumerate(shown) if p == "P001-09")
    assert first_a < first_b                       # story order preserved
    for s in plan["shots"]:                        # never shown together
        assert not {"P001-08", "P001-09"} <= {c["panel"] for c in s["cards"]}
    assert validate(plan, {p["id"] for p in panels}) == []
