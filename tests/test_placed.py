"""Placed mode (video.mode: placed): the Panel Placement Rule —
every panel shown WHOLE at native aspect, centred and scaled to the
MAXIMUM size fitting inside the frame (tall/square panels touch the
top and bottom walls, wider-than-16:9 panels touch the left and
right walls), over its own full-brightness blurred fill, with NO
motion at all (no drift/traverse/pan/zoom; motion hints ignored) and
hard cuts only. See plan.py's PL_* constants and frames.PlacedView.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.config import load_config
from manhwa_ocr.video import build as build_mod
from manhwa_ocr.video import frames as frames_mod
from manhwa_ocr.video import plan as plan_mod
from manhwa_ocr.video.frames import Format, PlacedView, ShotRenderer, format_from
from manhwa_ocr.video.plan import build_plan, placed_fit, placed_spec, validate

VCFG = load_config().video
FMT = format_from("16:9", 30)   # 1920x1080


# --------------------------------------------------------------- constants

def test_constants_pinned_across_config_planner_renderer():
    pl = load_config().video["placed"]
    assert pl["bg_blur"] == plan_mod.PL_BG_BLUR == frames_mod.PLR_BG_BLUR == 50
    # FULL brightness: the backdrop is never dimmed.
    assert pl["bg_opacity"] == plan_mod.PL_BG_OPACITY == frames_mod.PLR_BG_OPACITY == 1.0
    assert pl["shadow_dy"] == plan_mod.PL_SHADOW_DY == frames_mod.PLR_SHADOW_DY == 20
    assert pl["shadow_blur"] == plan_mod.PL_SHADOW_BLUR == frames_mod.PLR_SHADOW_BLUR == 10
    assert pl["shadow_opacity"] == plan_mod.PL_SHADOW_OPACITY \
        == frames_mod.PLR_SHADOW_OPACITY == 0.40
    assert pl["title_cards"] is True and plan_mod.PL_TITLE_CARDS is True
    # No upscale cap in this mode: a cap would show a small panel
    # smaller than the maximum fit — i.e. floating, the defect the
    # mode exists to eliminate.
    assert "max_upscale" not in pl


# --------------------------------------------------------------- fit math

def test_fit_tall_panel_touches_top_and_bottom():
    bw, bh, s, ox, oy, touches = placed_fit(900, 1200, FMT)
    assert (bw, bh) == (810.0, 1080.0)          # height is the limiting axis
    assert s == pytest.approx(0.9)
    assert (ox, oy) == (555.0, 0.0)             # centred, flush with top+bottom
    assert touches == "top_bottom"


def test_fit_square_panel_touches_top_and_bottom():
    bw, bh, s, ox, oy, touches = placed_fit(800, 800, FMT)
    assert (bw, bh) == (1080.0, 1080.0)
    assert (ox, oy) == (420.0, 0.0)
    assert touches == "top_bottom"


def test_fit_wider_than_frame_panel_touches_left_and_right():
    bw, bh, s, ox, oy, touches = placed_fit(2400, 900, FMT)
    assert (bw, bh) == (1920.0, 720.0)          # width is the limiting axis
    assert (ox, oy) == (0.0, 180.0)             # centred, flush with left+right
    assert touches == "left_right"


def test_fit_exact_canvas_panel_touches_both_pairs():
    bw, bh, s, ox, oy, touches = placed_fit(1920, 1080, FMT)
    assert (bw, bh) == (1920.0, 1080.0) and (ox, oy) == (0.0, 0.0)
    assert touches == "both"


@pytest.mark.parametrize("pw,ph", [(900, 1200), (2000, 900), (900, 2400), (700, 2000),
                                   (300, 400), (1600, 900), (640, 640), (900, 901),
                                   (1600, 1000), (1200, 1000), (100, 100), (4000, 100)])
def test_fit_is_the_maximum_inside_the_frame_and_never_stretches(pw, ph):
    bw, bh, s, ox, oy, touches = placed_fit(pw, ph, FMT)
    assert bw <= FMT.width + 1e-6 and bh <= FMT.height + 1e-6    # never exceeds the frame
    assert bw == pytest.approx(FMT.width) or bh == pytest.approx(FMT.height)  # touches a wall pair
    assert bw / bh == pytest.approx(pw / ph, rel=0.005)          # native aspect, never stretched
    assert s == pytest.approx(min(FMT.width / pw, FMT.height / ph))
    assert ox == pytest.approx((FMT.width - bw) / 2) and oy == pytest.approx((FMT.height - bh) / 2)


def test_spec_is_static_and_records_the_placement():
    sp = placed_spec(900, 1200, FMT)
    assert sp["kind"] == "placed" and sp["dir"] == "none" and sp["bg"] is True
    assert (sp["base_w"], sp["base_h"]) == (810.0, 1080.0)
    assert (sp["off_x"], sp["off_y"]) == (555.0, 0.0) and sp["touches"] == "top_bottom"
    # Motion fields pinned neutral: this shot cannot move, by construction.
    assert sp["zoom_rate"] == 0.0 and sp["zoom_end"] == 1.0 and sp["drift_frac"] == 0.0
    assert placed_spec(1920, 1080, FMT)["bg"] is False           # canvas-shaped: no backdrop needed


# --------------------------------------------------------------- validate

def _plan_with_spec(spec):
    return {"version": plan_mod.PLAN_VERSION, "duration": 4.0, "shots": [
        {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single", "background": "blur",
         "exit": "cut", "transition": "cut",
         "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                    "motion": "push_in", "placed": spec}],
         "effects": [], "callouts": []}],
        "chapter_cards": [], "sfx": [], "captions": []}


def test_validate_accepts_placed_specs():
    for pw, ph in ((900, 1200), (800, 800), (2400, 900), (1920, 1080)):
        assert validate(_plan_with_spec(placed_spec(pw, ph, FMT)), {"P001-01"}) == []


def test_validate_rejects_a_moving_or_malformed_placed_spec():
    moving = {**placed_spec(900, 1200, FMT), "zoom_rate": 0.05}
    assert any("placed" in e for e in validate(_plan_with_spec(moving), {"P001-01"}))
    wrong_kind = {**placed_spec(900, 1200, FMT), "kind": "zoom"}
    assert any("placed" in e for e in validate(_plan_with_spec(wrong_kind), {"P001-01"}))


# --------------------------------------------------------------- renderer

def _renderer(panel_img, spec, *, start=0.0, end=4.0):
    shot = {"id": "S001", "start": start, "end": end, "layout": "single", "background": "blur",
            "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                       "motion": "push_in", "placed": spec}],
            "effects": [], "placed": True,
            "sentence": None, "callouts": []}
    return ShotRenderer(shot, {"P001-01": panel_img}, FMT)


def test_render_background_is_the_panels_own_blur_at_full_brightness():
    img = Image.new("RGB", (900, 1200), (255, 0, 0))
    r = _renderer(img, placed_spec(900, 1200, FMT))
    f = r.frame_array(0.1)
    assert tuple(f[540, 960]) == (255, 0, 0)            # centre: the sharp foreground
    corner = f[10, 10].astype(int)                       # corner: blurred fill, NOT dimmed
    assert corner[0] >= 250 and corner[1] <= 6 and corner[2] <= 6


def test_render_is_static_first_frame_equals_last_frame():
    img = Image.new("RGB", (900, 1200), (200, 40, 40))
    ImageDraw.Draw(img).rectangle((300, 400, 600, 800), fill=(40, 40, 200))
    r = _renderer(img, placed_spec(900, 1200, FMT))
    assert np.array_equal(r.frame_array(0.05), r.frame_array(3.95))
    assert np.array_equal(r.frame_array(0.5), r.frame_array(2.0))


def test_render_placed_last_shot_ignores_the_video_end_fade():
    # Regression: ShotRenderer applied the global end_fade (a 1s fade
    # to black on the final shot) to placed shots too, breaking the
    # mode's "first frame is pixel-identical to its last" promise on
    # exactly the shot a viewer ends on. A placed shot must not fade.
    img = Image.new("RGB", (900, 1200), (200, 40, 40))
    ImageDraw.Draw(img).rectangle((300, 400, 600, 800), fill=(40, 40, 200))
    shot = {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single", "background": "blur",
            "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                       "motion": "push_in", "placed": placed_spec(900, 1200, FMT)}],
            "effects": [], "placed": True, "sentence": None, "callouts": [], "exit": "fade_out"}
    r = ShotRenderer(shot, {"P001-01": img}, FMT, video_end=4.0, end_fade=True)
    assert r.fade_from is None
    assert np.array_equal(r.frame_array(0.05), r.frame_array(3.99))


def test_placed_view_place_is_time_invariant_and_touches_the_walls():
    img = Image.new("RGB", (900, 1200), (120, 90, 70))
    card = {"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
            "motion": "push_in"}
    v = PlacedView(img, placed_spec(900, 1200, FMT), card, 0.0, 4.0, FMT)
    assert v._place(0.0) == v._place(3.9) == (555.0, 0.0, 810.0, 1080.0)
    # Even a punch extra-zoom cannot move or grow a placed panel.
    assert v._place(2.0, extra_zoom=0.5) == v._place(0.0)


def test_render_wide_panel_foreground_reaches_the_side_walls_sharp():
    # Fine stripes blur away in the backdrop but stay pure in the
    # foreground: pure stripe colours at BOTH side walls of the
    # centre row prove the foreground touches left and right.
    img = Image.new("RGB", (2400, 900), (255, 0, 0))
    d = ImageDraw.Draw(img)
    for x in range(10, 2400, 20):
        d.rectangle((x, 0, x + 9, 899), fill=(0, 200, 0))
    r = _renderer(img, placed_spec(2400, 900, FMT))
    f = r.frame_array(0.1)
    for x in (2, FMT.width - 3):
        px = f[540, x].astype(int)
        assert (px[0] > 200 and px[1] < 80) or (px[1] > 150 and px[0] < 80)


# --------------------------------------------------------------- build_plan + QA

def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 11):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


PANELS = [
    {"id": "P001-01", "width": 900, "height": 1200, "text": ""},    # tall -> top_bottom
    {"id": "P001-02", "width": 800, "height": 800, "text": ""},     # square -> top_bottom
    {"id": "P001-03", "width": 2400, "height": 900, "text": ""},    # wide -> left_right
]


def _placed_plan(sentences=None, duration=15.0):
    vcfg = {**VCFG, "mode": "placed"}
    sentences = sentences if sentences is not None else [
        {"start": 0.0, "text": "one", "panels": ["P001-01"], "cues": [], "emphasis": []},
        {"start": 5.0, "text": "two", "panels": ["P001-02"], "cues": [], "emphasis": []},
        {"start": 10.0, "text": "three", "panels": ["P001-03"], "cues": [], "emphasis": []},
    ]
    return build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "pl"},
                      duration=duration, audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=sentences)


def test_build_plan_placed_mode_selection():
    plan = _placed_plan()
    assert plan["mode"] == "placed"                 # the plan artifact records the mode
    specs = {c["panel"]: c["placed"] for s in plan["shots"] for c in s["cards"]}
    assert specs["P001-01"]["kind"] == "placed" and specs["P001-01"]["touches"] == "top_bottom"
    assert specs["P001-02"]["touches"] == "top_bottom"
    assert specs["P001-03"]["touches"] == "left_right"
    assert all(s.get("placed") is True for s in plan["shots"])
    # Hard cuts only, and no shot carries an effect or a motion.
    assert all(s["exit"] == "cut" and s["transition"] == "cut" and s["effects"] == []
               for s in plan["shots"])
    assert all(c["placed"]["zoom_rate"] == 0.0 and c["placed"]["drift_frac"] == 0.0
               for s in plan["shots"] for c in s["cards"])
    assert validate(plan, {p["id"] for p in PANELS}) == []
    qa = build_mod.quality_report(plan, {"panels": PANELS}, None, 3.0)
    assert not any("placed" in f for f in qa["failures"])


def test_build_plan_placed_ignores_motion_hints():
    plan = _placed_plan(sentences=[
        {"start": 0.0, "text": "he lifts one hand", "panels": ["P001-01"],
         "cues": [], "emphasis": [], "motion": "zoom_out"}], duration=5.0)
    specs = [c["placed"] for s in plan["shots"] for c in s["cards"]]
    assert specs and all(sp["kind"] == "placed" and sp["zoom_rate"] == 0.0 for sp in specs)
    assert set(plan["placed_hints_ignored"]) == {s["id"] for s in plan["shots"]}
    assert validate(plan, {p["id"] for p in PANELS}) == []


def test_build_plan_placed_shares_the_title_card_machinery():
    plan = _placed_plan(sentences=[
        {"start": 0.0, "text": "He was a nameless servant.", "panels": ["P001-01"],
         "cues": [], "emphasis": []},
        {"start": 4.0, "text": "Age 4. Week 6 of possession.", "panels": ["P001-02"],
         "cues": [], "emphasis": []},
        {"start": 5.5, "text": "The child opened his eyes.", "panels": ["P001-01"],
         "cues": [], "emphasis": []},
    ], duration=9.5)
    cards = [s for s in plan["shots"] if s.get("title_card")]
    assert len(cards) == 1
    assert cards[0]["title_card"]["line"] == "WEEK 6 OF POSSESSION."
    assert cards[0].get("placed") is True and "panelfit" not in cards[0]
    assert plan["placed_title_cards"][0]["id"] == cards[0]["id"]
    spans = [(float(s["start"]), float(s["end"])) for s in plan["shots"]]
    assert spans[0][0] == 0.0 and spans[-1][1] == 9.5
    assert all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1))
    assert validate(plan, {p["id"] for p in PANELS}) == []


def test_qa_flags_a_floating_foreground():
    plan = _placed_plan()
    panels = {"panels": PANELS}
    qa = build_mod.quality_report(plan, panels, None, 3.0)
    assert not any("placed" in f for f in qa["failures"])
    for s in plan["shots"]:
        for c in s["cards"]:
            # Half the maximum size, aspect kept: a floating panel.
            c["placed"] = {**c["placed"], "base_w": c["placed"]["base_w"] * 0.5,
                           "base_h": c["placed"]["base_h"] * 0.5}
    qa = build_mod.quality_report(plan, panels, None, 3.0)
    assert any("placed" in f for f in qa["failures"])


# --------------------------------------------------------------- prep edge-trim
#
# The white-space defect: panels carry BAKED-IN white page margins
# inside the panel image. Placed prep (plan._placed_pass) now shaves
# every unit on all four edges before fit — reusing the wide-mode
# detector crops.trim_blank_edges (rows directly, columns via the
# transposed image) — records the rect in the spec, fits on the
# trimmed bounds, and falls back to the untrimmed unit (flagged,
# never dropped) when no art bounds can be found.


def _textured_rect(d, box, fill=(170, 60, 50), alt=(60, 120, 160), line=(30, 25, 20)):
    # Coarse checkerboard texture (36px cells): the trim detector
    # analyses at 120px wide, where fine line art averages flat and
    # (correctly) reads as no-art — the texture must survive that
    # downscale, like real panel shading does.
    x0, y0, x1, y1 = box
    d.rectangle(box, fill=fill)
    cell = 36
    for yy in range(y0, y1 + 1, cell):
        for xx in range(x0, x1 + 1, cell):
            if ((xx - x0) // cell + (yy - y0) // cell) % 2:
                d.rectangle((xx, yy, min(xx + cell - 1, x1), min(yy + cell - 1, y1)), fill=alt)
    d.rectangle(box, outline=line, width=3)


def _margin_img(w=600, h=800, rect=(60, 200, 540, 700)):
    """A white panel whose textured art sits inside white margins on
    all four sides: rect = (x0, y0, x1, y1) in pixels."""
    img = Image.new("RGB", (w, h), (255, 255, 255))
    _textured_rect(ImageDraw.Draw(img), rect)
    return img


def test_placed_trim_rect_shaves_all_four_edges():
    rect = plan_mod.placed_trim_rect(_margin_img())
    assert rect is not None
    # Art bounds in fractions: (60/600, 200/800, 540/600, 700/800).
    assert rect == pytest.approx((0.10, 0.25, 0.90, 0.875), abs=0.03)


def test_placed_trim_rect_is_zero_when_art_touches_every_edge():
    img = Image.new("RGB", (600, 800), (255, 255, 255))
    _textured_rect(ImageDraw.Draw(img), (0, 0, 599, 799))
    assert plan_mod.placed_trim_rect(img) == (0.0, 0.0, 1.0, 1.0)


def test_placed_trim_rect_none_when_no_art_can_be_found():
    # A mostly-white unit: the detector's no-art guard refuses, so the
    # caller falls back instead of trimming the unit away to nothing.
    assert plan_mod.placed_trim_rect(Image.new("RGB", (600, 800), (255, 255, 255))) is None


def _placed_plan_custom(panels, image_for, sentences, slice_hints=None):
    vcfg = {**VCFG, "mode": "placed"}
    return build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "pltrim"},
                      duration=5.0, audio=None, vcfg=vcfg, image_for=image_for,
                      sentences=sentences, slice_hints=slice_hints)


def _one(pid, text="one"):
    return [{"start": 0.0, "text": text, "panels": [pid], "cues": [], "emphasis": []}]


def test_build_plan_placed_trims_baked_in_margins_before_fit():
    panels = [{"id": "P009-01", "width": 600, "height": 800, "text": ""}]
    plan = _placed_plan_custom(panels, lambda p: _margin_img(), _one("P009-01"))
    spec = plan["shots"][0]["cards"][0]["placed"]
    # The trim rect is recorded in the plan, per unit, as fractions.
    assert spec["trim"] == pytest.approx([0.10, 0.25, 0.90, 0.875], abs=0.03)
    assert spec["trim_fallback"] is False
    # Fit ran on the TRIMMED bounds (480x500 art), not the raw panel
    # (600x800): untrimmed, base_w would be 810; trimmed it is ~1037.
    assert spec["base_w"] == pytest.approx(480 / 500 * 1080, rel=0.06)
    assert spec["base_h"] == pytest.approx(1080.0, rel=0.02)
    assert spec["touches"] == "top_bottom"
    assert plan["placed_trimmed"] == ["P009-01"]
    assert plan["placed_trim_fallback"] == []
    assert validate(plan, {"P009-01"}) == []
    qa = build_mod.quality_report(plan, {"panels": panels}, None, 3.0)
    assert not any("placed" in f for f in qa["failures"])
    assert qa["placed_trimmed"] == ["P009-01"]


def test_build_plan_placed_clean_panel_passes_through_untrimmed():
    panels = [{"id": "P009-03", "width": 600, "height": 800, "text": ""}]
    img = Image.new("RGB", (600, 800), (255, 255, 255))
    _textured_rect(ImageDraw.Draw(img), (0, 0, 599, 799))   # art touches every edge
    plan = _placed_plan_custom(panels, lambda p: img, _one("P009-03"))
    spec = plan["shots"][0]["cards"][0]["placed"]
    assert spec["trim"] == [0.0, 0.0, 1.0, 1.0]
    assert spec["trim_fallback"] is False
    assert plan["placed_trimmed"] == [] and plan["placed_trim_fallback"] == []
    # Zero trim => the spec is exactly the pre-trim placed spec.
    want = placed_spec(600, 800, FMT)
    assert (spec["base_w"], spec["base_h"], spec["touches"]) \
        == (want["base_w"], want["base_h"], want["touches"])


def test_build_plan_placed_no_art_unit_falls_back_flagged_not_dropped():
    panels = [{"id": "P009-02", "width": 600, "height": 800, "text": ""}]
    white = Image.new("RGB", (600, 800), (255, 255, 255))
    plan = _placed_plan_custom(panels, lambda p: white, _one("P009-02"))
    cards = [c for s in plan["shots"] for c in s["cards"]]
    assert [c["panel"] for c in cards] == ["P009-02"]      # kept, never dropped
    spec = cards[0]["placed"]
    assert spec["trim"] == [0.0, 0.0, 1.0, 1.0]            # untrimmed fallback
    assert spec["trim_fallback"] is True
    want = placed_spec(600, 800, FMT)                      # fitted on the whole unit
    assert (spec["base_w"], spec["base_h"]) == (want["base_w"], want["base_h"])
    assert plan["placed_trim_fallback"] == ["P009-02"]
    assert plan["placed_trimmed"] == []
    assert validate(plan, {"P009-02"}) == []
    qa = build_mod.quality_report(plan, {"panels": panels}, None, 3.0)
    assert not any("placed" in f for f in qa["failures"])
    assert qa["placed_trim_fallback"] == ["P009-02"]


def test_build_plan_placed_slice_unit_is_trimmed_after_slicing():
    # Top band: textured art edge to edge. Bottom band: white with an
    # inset art rect — its margins are INSIDE the band, so only a trim
    # applied after slicing (on the band's own image) can shave them.
    img = Image.new("RGB", (600, 1200), (255, 255, 255))
    d = ImageDraw.Draw(img)
    _textured_rect(d, (0, 0, 599, 599), fill=(120, 90, 70))
    _textured_rect(d, (60, 660, 540, 1140))
    panels = [{"id": "P001-01", "width": 600, "height": 1200, "text": ""}]
    plan = _placed_plan_custom(panels, lambda p: img, _one("P001-01"),
                               slice_hints={"P001-01": {"slice": [0.5]}})
    specs = {}
    for s in plan["shots"]:
        for c in s["cards"]:
            if c.get("slice"):
                specs[tuple(c["slice"])] = c["placed"]
    assert set(specs) == {(0.0, 0.5), (0.5, 1.0)}
    assert specs[(0.0, 0.5)]["trim"] == [0.0, 0.0, 1.0, 1.0]
    band = specs[(0.5, 1.0)]
    # Band-relative art bounds: x 60..540 of 600, y 60..540 of 600.
    assert band["trim"] == pytest.approx([0.10, 0.10, 0.90, 0.90], abs=0.04)
    # Trimmed band is ~480x480: fitted square, touching top/bottom.
    assert band["base_w"] == pytest.approx(1080.0, rel=0.08)
    assert band["base_h"] == pytest.approx(1080.0, rel=0.08)
    assert band["touches"] == "top_bottom"
    assert plan["placed_trimmed"] == ["P001-01"]
    assert validate(plan, {"P001-01"}) == []


def test_validate_rejects_a_malformed_placed_trim_rect():
    bad = {**placed_spec(900, 1200, FMT), "trim": [0.5, 0.0, 0.4, 1.0],
           "trim_fallback": False}
    assert any("placed" in e for e in validate(_plan_with_spec(bad), {"P001-01"}))


def test_render_placed_uses_the_trimmed_art_for_foreground_and_backdrop():
    img = _margin_img(rect=(90, 120, 510, 680))   # art 420x560, margins all round
    spec = {**placed_spec(420, 560, FMT), "trim": [0.15, 0.15, 0.85, 0.85],
            "trim_fallback": False}
    r = _renderer(img, spec)
    f = r.frame_array(0.1)
    top_centre = f[2, 960].astype(int)            # foreground touches the top wall
    assert not (top_centre[0] > 240 and top_centre[1] > 240 and top_centre[2] > 240)  # NOT the white margin
    centre = f[540, 960].astype(int)              # an art cell of the checkerboard
    assert centre[0] > 120 and centre[1] < 110    # the red fill cell, not white, not the alt cell
    corner = f[10, 10].astype(int)                # backdrop: blur of the TRIMMED art
    assert not (corner[0] > 240 and corner[1] > 240)     # never the white margin
