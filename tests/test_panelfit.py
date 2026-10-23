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
    assert pf["drift_speed"] == plan_mod.PF_DRIFT_SPEED == 0.068
    assert pf["drift_min_travel"] == plan_mod.PF_DRIFT_MIN_TRAVEL == 0.05
    assert pf["drift_max_travel"] == plan_mod.PF_DRIFT_MAX_TRAVEL == 0.13
    assert pf["drift_headroom"] == plan_mod.PF_DRIFT_HEADROOM == 0.03
    assert pf["zoom_rate"] == plan_mod.PF_ZOOM_RATE == 0.05
    assert pf["zoom_rate_impact"] == plan_mod.PF_ZOOM_RATE_IMPACT == 0.088
    assert pf["zoom_max_total"] == plan_mod.PF_ZOOM_MAX_TOTAL == 0.50
    assert pf["max_upscale"] == plan_mod.PF_MAX_UPSCALE == 2.4
    assert pf["still_limit"] == plan_mod.PF_STILL_LIMIT == 0.5
    assert pf["min_shot"] == plan_mod.PF_MIN_SHOT == 0.3
    assert pf["split_above"] == plan_mod.PF_SPLIT_ABOVE == 3.5
    assert pf["split_min_piece"] == plan_mod.PF_SPLIT_MIN_PIECE == 1.2
    assert pf["captions"] is False and plan_mod.PF_CAPTIONS is False
    assert pf["title_cards"] is True and plan_mod.PF_TITLE_CARDS is True
    assert pf["tts_gap_seconds"] == plan_mod.PF_TTS_GAP == 0.20


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
    # travel = clamp(0.068 * 2.0) = 0.13 -> oversize 1 + 0.13 + 0.03 = 1.16
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
    assert panelfit_drift_travel(1.0) == pytest.approx(0.068)   # speed x duration
    assert panelfit_drift_travel(0.5) == pytest.approx(0.05)    # floor
    assert panelfit_drift_travel(10.0) == pytest.approx(0.13)   # cap
    assert panelfit_drift_travel(1.47) == pytest.approx(0.068 * 1.47, abs=0.001)


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
    # drift position directly. Travel is capped at 13% of frame height
    # (140px over the whole 8s shot), NOT a full strip traversal.
    img = Image.new("RGB", (900, 2400), (255, 0, 0))
    ImageDraw.Draw(img).rectangle((0, 1200, 900, 2399), fill=(0, 0, 255))
    r = _renderer(img, panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=8.0), end=8.0)
    ys = [_boundary_y(r.frame_array(t)) for t in (3.2, 4.0, 4.8)]
    assert all(y is not None for y in ys)
    assert ys[0] - ys[1] == pytest.approx(ys[1] - ys[2], abs=3)     # constant velocity
    assert abs(ys[2] - ys[0]) < 40                                   # ~28px in 1.6s, not ~1000px


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
