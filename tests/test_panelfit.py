"""Panel-fit mode (video.mode: panelfit): the user's rendering spec as a
mode — whole panel at native aspect over its own blurred fill, LINEAR
sub-pixel motion, zero static, impact shake/flash. See
docs/EDITOR_GUIDE.md "Panel-fit mode" and plan.py's PF_* constants.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.config import load_config
from manhwa_ocr.video import build as build_mod
from manhwa_ocr.video import frames as frames_mod
from manhwa_ocr.video import plan as plan_mod
from manhwa_ocr.video.frames import Format, ShotRenderer, format_from
from manhwa_ocr.video.plan import (build_plan, panelfit_fit, panelfit_impact_fx,
                                  panelfit_spec, validate)

VCFG = load_config().video
FMT = format_from("16:9", 30)   # 1920x1080


# --------------------------------------------------------------- constants

def test_constants_pinned_across_config_planner_renderer():
    pf = load_config().video["panelfit"]
    assert pf["bg_blur"] == plan_mod.PF_BG_BLUR == frames_mod.PFR_BG_BLUR == 50
    assert pf["bg_opacity"] == plan_mod.PF_BG_OPACITY == frames_mod.PFR_BG_OPACITY == 0.60
    assert pf["zoom_end"] == plan_mod.PF_ZOOM_END == 1.08
    assert pf["strip_aspect"] == plan_mod.PF_STRIP_ASPECT == 1.9
    assert pf["max_upscale"] == plan_mod.PF_MAX_UPSCALE == 2.4
    assert pf["still_limit"] == plan_mod.PF_STILL_LIMIT == 0.5
    assert pf["fx"] is True and plan_mod.PF_FX is True
    assert pf["shake_seconds"] == plan_mod.PF_SHAKE_SECONDS == frames_mod.PFR_SHAKE_SECONDS == 0.2
    assert pf["shake_amplitude"] == plan_mod.PF_SHAKE_AMPLITUDE == frames_mod.PFR_SHAKE_AMPLITUDE == 10.0
    assert pf["flash_frames"] == plan_mod.PF_FLASH_FRAMES == frames_mod.PFR_FLASH_FRAMES == 3


# --------------------------------------------------------------- fit / spec

def test_fit_portrait_panel_by_height():
    kind, bw, bh, s = panelfit_fit(900, 1200, FMT)
    assert kind == "zoom"
    assert (bw, bh) == (810.0, 1080.0)      # height-fitted, aspect kept exactly


def test_fit_exact_canvas_panel_fills_frame():
    kind, bw, bh, s = panelfit_fit(1920, 1080, FMT)
    assert kind == "zoom" and (bw, bh) == (1920.0, 1080.0)


def test_fit_wider_than_canvas_by_width():
    kind, bw, bh, s = panelfit_fit(2000, 900, FMT)
    assert kind == "zoom" and bw == 1920.0 and bh == pytest.approx(864.0)


def test_fit_strip_scrolls_fitted_by_width():
    kind, bw, bh, s = panelfit_fit(900, 2400, FMT)
    assert kind == "scroll" and bw == 1920.0 and bh == pytest.approx(5120.0)


def test_fit_strip_scale_capped():
    kind, bw, bh, s = panelfit_fit(700, 2000, FMT)
    assert kind == "scroll" and s == 2.4 and (bw, bh) == (1680.0, 4800.0)


def test_fit_tiny_panel_capped_not_enlarged_past_cap():
    kind, bw, bh, s = panelfit_fit(300, 400, FMT)
    assert kind == "zoom" and s == 2.4 and (bw, bh) == (720.0, 960.0)


@pytest.mark.parametrize("pw,ph", [(900, 1200), (2000, 900), (900, 2400), (700, 2000),
                                   (300, 400), (1600, 900), (640, 640), (900, 901)])
def test_fit_never_stretches(pw, ph):
    kind, bw, bh, s = panelfit_fit(pw, ph, FMT)
    assert bw / bh == pytest.approx(pw / ph, rel=0.005)


def test_spec_background_flag_and_directions():
    sp = panelfit_spec(900, 1200, "push_in", FMT, {})
    assert sp["bg"] is True and sp["dir"] == "in" and sp["kind"] == "zoom"
    assert panelfit_spec(1920, 1080, "push_in", FMT, {})["bg"] is False   # exactly canvas-shaped
    assert panelfit_spec(900, 1200, "pull_out", FMT, {})["dir"] == "out"
    sp = panelfit_spec(900, 2400, "pan_down", FMT, {})
    assert sp["kind"] == "scroll" and sp["dir"] == "down"
    assert panelfit_spec(900, 2400, "pan_up", FMT, {})["dir"] == "up"


# --------------------------------------------------------------- impact FX

def _shot(start=10.0, effects=(), sentence=""):
    return {"id": "S001", "start": start, "end": start + 4.0,
            "effects": list(effects), "sentence": sentence}


def test_fx_from_impact_marker():
    fx, rule = panelfit_impact_fx(_shot(effects=[{"type": "shake", "t": 10.6}]), {})
    assert rule == "marker" and fx["shake_t"] == [0.6] and fx["flash_t"] == [0.0]


def test_fx_from_reveal_marker_flashes_at_cut_and_at_marker():
    fx, rule = panelfit_impact_fx(_shot(effects=[{"type": "flash", "t": 12.0}]), {})
    assert rule == "marker" and fx["shake_t"] == [] and fx["flash_t"] == [0.0, 2.0]


def test_fx_fallback_action_sentence_with_impact_verb():
    fx, rule = panelfit_impact_fx(_shot(sentence="He slams the iron gate shut with one hand."), {})
    assert rule == "fallback" and fx == {"shake_t": [0.0], "flash_t": [0.0]}


def test_fx_none_for_internal_sentence_even_with_impact_word():
    fx, rule = panelfit_impact_fx(_shot(sentence="She recalled how he strikes the bell at dawn."), {})
    assert rule is None and fx == {"shake_t": [], "flash_t": []}


def test_fx_none_for_plain_sentence_and_when_disabled():
    fx, rule = panelfit_impact_fx(_shot(sentence="The duke walks into the hall."), {})
    assert rule is None and fx == {"shake_t": [], "flash_t": []}
    fx, rule = panelfit_impact_fx(_shot(effects=[{"type": "shake", "t": 10.5}]), {"fx": False})
    assert rule is None and fx == {"shake_t": [], "flash_t": []}


# --------------------------------------------------------------- validate

def _plan_with_spec(spec):
    return {"version": plan_mod.PLAN_VERSION, "duration": 4.0, "shots": [
        {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single", "background": "blur",
         "exit": "cut", "transition": "cut",
         "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                    "motion": "push_in", "panelfit": spec}],
         "effects": [], "callouts": []}],
        "chapter_cards": [], "sfx": [], "captions": []}


def test_validate_accepts_a_good_panelfit_spec():
    errs = validate(_plan_with_spec(panelfit_spec(900, 1200, "push_in", FMT, {})), {"P001-01"})
    assert errs == []


def test_validate_rejects_a_broken_panelfit_spec():
    bad = {"kind": "spin", "dir": "in", "bg": True, "base_w": 810.0, "base_h": 1080.0, "zoom_end": 1.08}
    errs = validate(_plan_with_spec(bad), {"P001-01"})
    assert any("panelfit" in e for e in errs)


# --------------------------------------------------------------- renderer

def _renderer(panel_img, spec, *, fx=None, start=0.0, end=4.0, motion="push_in"):
    shot = {"id": "S001", "start": start, "end": end, "layout": "single", "background": "blur",
            "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                       "motion": motion, "panelfit": spec}],
            "effects": [], "panelfit": True,
            "panelfit_fx": fx or {"shake_t": [], "flash_t": []},
            "sentence": None, "callouts": []}
    return ShotRenderer(shot, {"P001-01": panel_img}, FMT)


def test_render_foreground_sharp_panel_over_dimmed_blur():
    img = Image.new("RGB", (900, 1200), (255, 0, 0))
    r = _renderer(img, panelfit_spec(900, 1200, "push_in", FMT, {}))
    f = r.frame_array(0.1)
    assert tuple(f[540, 960]) == (255, 0, 0)            # centre: the sharp foreground
    corner = f[10, 10].astype(int)                       # corner: blurred fill at 60% over black
    assert abs(corner[0] - 153) <= 6 and corner[1] <= 6 and corner[2] <= 6


def test_render_zoom_is_never_static():
    img = Image.new("RGB", (900, 1200), (200, 40, 40))
    d = ImageDraw.Draw(img)
    d.rectangle((300, 400, 600, 800), fill=(40, 40, 200))
    r = _renderer(img, panelfit_spec(900, 1200, "push_in", FMT, {}))
    assert not np.array_equal(r.frame_array(0.5), r.frame_array(0.9))
    assert r.is_static(2.0) is False


def _boundary_y(arr):
    col = arr[:, 960]
    blue = np.flatnonzero(col[:, 2] > 128)
    return int(blue[0]) if len(blue) else None


def test_render_scroll_velocity_is_constant():
    # Top half red, bottom half blue: the colour boundary's frame y is a
    # direct readout of the scroll position, and LINEAR means equal steps.
    img = Image.new("RGB", (900, 2400), (255, 0, 0))
    ImageDraw.Draw(img).rectangle((0, 1200, 900, 2399), fill=(0, 0, 255))
    r = _renderer(img, panelfit_spec(900, 2400, "pan_down", FMT, {}), end=8.0)
    ys = [_boundary_y(r.frame_array(t)) for t in (3.2, 4.0, 4.8)]
    assert all(y is not None for y in ys)
    assert ys[0] - ys[1] == pytest.approx(ys[1] - ys[2], abs=3)


def test_render_impact_flash_decays_over_three_frames():
    img = Image.new("RGB", (900, 1200), (120, 60, 60))
    spec = panelfit_spec(900, 1200, "push_in", FMT, {})
    plain = _renderer(img, spec)
    flash = _renderer(img, spec, fx={"shake_t": [], "flash_t": [0.0]})
    means = [float(flash.frame_array(k / 30).mean()) for k in range(4)]
    assert means[0] > means[1] > means[2] > float(plain.frame_array(3 / 30).mean()) - 1.0
    assert means[3] == pytest.approx(float(plain.frame_array(3 / 30).mean()), abs=1.0)


def test_render_impact_shake_moves_the_frame_for_0_2s_only():
    img = Image.new("RGB", (900, 1200), (120, 60, 60))
    ImageDraw.Draw(img).rectangle((350, 400, 550, 800), fill=(240, 240, 240))
    spec = panelfit_spec(900, 1200, "push_in", FMT, {})
    plain = _renderer(img, spec)
    shaken = _renderer(img, spec, fx={"shake_t": [0.5], "flash_t": []})
    assert not np.array_equal(shaken.frame_array(0.53), plain.frame_array(0.53))
    assert np.array_equal(shaken.frame_array(0.9), plain.frame_array(0.9))


# --------------------------------------------------------------- build_plan + QA

def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 11):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


PANELS = [
    {"id": "P001-01", "width": 900, "height": 1200, "text": ""},    # portrait -> zoom, bg
    {"id": "P001-02", "width": 900, "height": 2400, "text": ""},    # strip -> scroll
    {"id": "P001-03", "width": 1500, "height": 900, "text": ""},    # wide-ish -> zoom, bg
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
    assert specs["P001-01"]["kind"] == "zoom" and specs["P001-01"]["bg"] is True
    assert specs["P001-02"]["kind"] == "scroll"
    assert specs["P001-03"]["kind"] == "zoom" and specs["P001-03"]["bg"] is True
    assert all(s.get("panelfit") is True and s["caption_bar"] is False for s in plan["shots"])
    impact = {e["id"]: e for e in plan["panelfit_impact"]}
    assert impact, "the {impact} sentence's shot must carry impact FX"
    assert all(e["rule"] == "marker" for e in impact.values())


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
