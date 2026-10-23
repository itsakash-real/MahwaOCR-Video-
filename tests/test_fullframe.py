"""Full-frame mode (video.mode: fullframe): no cards, no backdrop -- every
shot is a full-bleed 16:9 camera viewport travelling over the panel.

The planner resolves each card's focal crop into a camera path
(card["viewport"] = {window, from, to}): both viewports exactly 16:9 in
panel pixels, both inside the window, the window inside the panel. Tall
windows pan, wide windows pan sideways, ~16:9 windows zoom. The zoom
guardrail (video.fullframe.max_upscale, default 2.4) floors the viewport
source at frame/2.4 -- 800x450 panel pixels -- whenever the panel can
supply it.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.config import load_config
from manhwa_ocr.video import build as build_mod
from manhwa_ocr.video.frames import Format, ShotRenderer, format_from
from manhwa_ocr.video.plan import (FF_ASPECT, FF_MAX_UPSCALE, FF_PAN_TRAVEL, FF_ZOOM_DEPTH,
                                   build_plan, fullframe_viewport, validate)

VCFG = load_config().video
MIN_W, MIN_H = 800.0, 450.0    # 1920/2.4, 1080/2.4: the default guardrail floor


def _vp(pw, ph, box, motion, **kw):
    kw.setdefault("min_vp_w", MIN_W)
    kw.setdefault("min_vp_h", MIN_H)
    return fullframe_viewport(pw, ph, box, motion, **kw)


def _px(rect, pw, ph):
    return ((rect[2] - rect[0]) * pw, (rect[3] - rect[1]) * ph)


def _assert_path(vp, pw, ph):
    """The invariants every camera path must keep: viewports exactly 16:9
    (panel pixels), inside the window, window inside the panel."""
    win = vp["window"]
    assert 0.0 <= win[0] < win[2] <= 1.0 and 0.0 <= win[1] < win[3] <= 1.0
    for key in ("from", "to"):
        r = vp[key]
        w, h = _px(r, pw, ph)
        assert w / h == pytest.approx(FF_ASPECT, rel=0.01)
        assert r[0] >= win[0] - 1e-9 and r[1] >= win[1] - 1e-9
        assert r[2] <= win[2] + 1e-9 and r[3] <= win[3] + 1e-9


# ------------------------------------------------------------------ pans

def test_tall_focal_becomes_a_downward_scroll():
    # focal 720x1320 on a 900x2400 strip: no 16:9 viewport on this panel
    # can contain it, so even a zoom request resolves to a pan.
    vp, info = _vp(900, 2400, [0.1, 0.2, 0.9, 0.75], "push_in")
    assert info["kind"] == "pan_y" and info["motion"] == "pan_down"
    _assert_path(vp, 900, 2400)
    win, frm, to = vp["window"], vp["from"], vp["to"]
    assert frm[1] == pytest.approx(win[1])            # starts at the window's top
    assert to[3] == pytest.approx(win[3])             # ends at its bottom
    # the window contains the whole focal (it is traversed, never sliced)
    assert win[0] <= 0.1 and win[2] >= 0.9 and win[1] <= 0.2 and win[3] >= 0.75
    assert not info["static"] and not info["over_cap"]


def test_pan_up_starts_at_the_bottom():
    vp, info = _vp(900, 2400, [0.1, 0.2, 0.9, 0.75], "pan_up")
    assert info["kind"] == "pan_y" and info["motion"] == "pan_up"
    _assert_path(vp, 900, 2400)
    assert vp["from"][3] == pytest.approx(vp["window"][3])
    assert vp["to"][1] == pytest.approx(vp["window"][1])


def test_pan_left_starts_on_the_right():
    vp, info = _vp(2400, 900, [0.3, 0.25, 0.7, 0.75], "pan_left")
    assert info["kind"] == "pan_x" and info["motion"] == "pan_left"
    _assert_path(vp, 2400, 900)
    assert vp["from"][2] == pytest.approx(vp["window"][2])   # right end
    assert vp["to"][0] == pytest.approx(vp["window"][0])     # travels left


def test_pan_with_no_travel_falls_back_to_a_zoom():
    # panel only 490 tall: a full-width viewport is 450+ tall already, so
    # there is <12% travel -- pan_down degrades to push_in (wide's swap).
    vp, info = _vp(900, 490, [0.1, 0.1, 0.9, 0.9], "pan_down")
    assert info["kind"] == "zoom" and info["motion"] == "push_in"
    _assert_path(vp, 900, 490)
    assert not info["static"]


# ------------------------------------------------------------------ zooms

def test_zoom_push_in_and_pull_out_are_mirror_images():
    box = [0.3, 0.3, 0.7, 0.6]
    vin, iin = _vp(1600, 1200, box, "push_in")
    vout, iout = _vp(1600, 1200, box, "pull_out")
    _assert_path(vin, 1600, 1200)
    assert iin["motion"] == "push_in" and iout["motion"] == "pull_out"
    assert vin["from"] == vout["to"] and vin["to"] == vout["from"]   # exact mirror
    ow, _ = _px(vin["from"], 1600, 1200)
    iw, _ = _px(vin["to"], 1600, 1200)
    assert ow / iw == pytest.approx(1.18, rel=0.02)                  # full target depth
    # the inner viewport sits inside the outer, centred on the focal
    assert vin["to"][0] >= vin["from"][0] and vin["to"][2] <= vin["from"][2]
    assert not iin["static"] and not iin["over_cap"] and not iin["focal_overflow"]


def test_zoom_depth_is_capped_by_the_guardrail_floor():
    # a small focal on a 900x700 panel: the outer viewport can grow to the
    # panel width only, so depth is capped where the inner viewport would
    # breach the 800x450 floor.
    vp, info = _vp(900, 700, [0.4, 0.4, 0.6, 0.55], "push_in")
    _assert_path(vp, 900, 700)
    iw, ih = _px(vp["to"], 900, 700)
    assert iw == pytest.approx(MIN_W, rel=0.02) and ih == pytest.approx(MIN_H, rel=0.02)
    ow, _ = _px(vp["from"], 900, 700)
    assert ow / iw < 1.18                       # depth was capped, not the floor
    assert not info["over_cap"] and not info["static"]


def test_tiny_panel_is_over_cap_but_still_a_valid_path():
    # the whole panel is smaller than the guardrail floor: the path is
    # still exactly 16:9 and inside the panel, just honestly flagged.
    vp, info = _vp(640, 360, [0.2, 0.2, 0.8, 0.8], "push_in")
    _assert_path(vp, 640, 360)
    assert info["over_cap"] is True
    assert info["static"] is True               # no room to zoom at all


def test_hold_is_a_still_viewport():
    vp, info = _vp(1600, 1200, [0.3, 0.3, 0.7, 0.6], "hold")
    _assert_path(vp, 1600, 1200)
    assert vp["from"] == vp["to"] and info["static"] is True


def test_viewport_edges_avoid_slicing_a_bubble():
    # Mirrors wide's edge test: a lettered white "bubble" band sits just
    # above the subject; the zoom window's top edge must not land inside it.
    panel = Image.new("RGB", (900, 1600), (90, 90, 90))
    d = ImageDraw.Draw(panel)
    d.rectangle((60, 300, 840, 500), fill=(255, 255, 255), outline=(10, 10, 10), width=4)
    for y in range(340, 480, 28):
        d.line((100, y, 800, y), fill=(20, 20, 20), width=8)
    d.rectangle((180, 640, 720, 1300), fill=(150, 90, 70))     # the subject
    box = [0.2, 0.40, 0.8, 0.8125]                            # 540x660 focal
    vp, _info = _vp(900, 1600, box, "push_in", img=panel)
    _assert_path(vp, 900, 1600)
    top_px = vp["window"][1] * 1600
    # the bubble occupies roughly rows 296-504 (incl. margin); the window
    # top edge must not land strictly inside that band.
    assert not (306 < top_px < 494)


# ------------------------------------------------------------------ the pass

PANELS = [
    {"id": "P001-01", "width": 900, "height": 500, "text": ""},    # landscape
    {"id": "P001-02", "width": 900, "height": 2400, "text": ""},   # tall strip
    {"id": "P001-03", "width": 900, "height": 1400, "text": ""},
    {"id": "P001-04", "width": 600, "height": 1200, "text": ""},
]


def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 11):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


def _ff_plan(duration=32.0, panels=PANELS, fmt=None, mode="fullframe"):
    vcfg = {**VCFG, "mode": mode, "fullframe": {"max_upscale": 2.4, "zoom_depth": 1.18, "pan_travel": 1.0}}
    if fmt:
        vcfg["format"] = fmt
    sentences = [
        {"start": 0.0, "text": "one", "panels": ["P001-01"], "cues": [], "emphasis": []},
        {"start": 5.0, "text": "two", "panels": ["P001-02"], "cues": [], "emphasis": []},
        {"start": 11.0, "text": "three", "panels": ["P001-03"], "cues": [], "emphasis": []},
        {"start": 17.0, "text": "four", "panels": ["P001-04"], "cues": [], "emphasis": []},
        {"start": 23.0, "text": "five", "panels": ["P001-02"], "cues": [], "emphasis": []},
    ]
    return build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "f"},
                      duration=duration, audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=sentences)


def test_fullframe_pass_sets_viewports_on_every_card():
    plan = _ff_plan()
    assert plan["mode"] == "fullframe"
    for key in ("fullframe_caption_risk", "fullframe_figure_risk",
                "fullframe_reviewer_focal_ignored", "fullframe_over_cap", "fullframe_static"):
        assert isinstance(plan.get(key), list)
    dims = {p["id"]: (p["width"], p["height"]) for p in PANELS}
    assert plan["shots"], "plan produced no shots"
    for s in plan["shots"]:
        assert s["fullframe"] is True
        assert s["caption_bar"] is False
        assert s["layout"] in ("single", "pair")
        for c in s["cards"]:
            assert c["fullframe"] is True and c["pad_y"] == 0.0
            assert c["crop"] == c["viewport"]["window"]
            _assert_path(c["viewport"], *dims[c["panel"]])
    assert validate(plan, set(dims)) == []


def test_fullframe_timing_and_panels_are_untouched():
    # The sync audit reads shots' start/end/panels/at: a full-frame plan
    # must carry the identical skeleton to the classic plan it derives from.
    ff = _ff_plan()
    classic = _ff_plan(mode="classic")
    skel = lambda plan: [(s["start"], s["end"], [c["panel"] for c in s["cards"]],
                          [c.get("at") for c in s["cards"]]) for s in plan["shots"]]
    assert skel(ff) == skel(classic)


def test_classic_plan_has_no_viewports():
    plan = _ff_plan(mode="classic")
    assert plan["mode"] == "classic"
    assert plan["fullframe_caption_risk"] == [] and plan["fullframe_static"] == []
    for s in plan["shots"]:
        assert not s.get("fullframe")
        for c in s["cards"]:
            assert "viewport" not in c and not c.get("fullframe")


def test_portrait_format_is_untouched_by_fullframe_mode():
    plan = _ff_plan(fmt="9:16")
    for s in plan["shots"]:
        assert not s.get("fullframe")
        for c in s["cards"]:
            assert "viewport" not in c


# ------------------------------------------------------------------ renderer

def _banded_panel():
    """900x1600: red top 600, green middle 400, blue bottom 600."""
    img = Image.new("RGB", (900, 1600), (30, 30, 200))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 900, 600), fill=(200, 30, 30))
    d.rectangle((0, 600, 900, 1000), fill=(30, 180, 30))
    return img


def _ff_shot(cards, start=0.0, end=4.0, layout="single"):
    return {"id": "S001", "start": start, "end": end, "layout": layout, "background": "black",
            "fullframe": True, "caption_bar": False, "cards": cards, "exit": "cut", "effects": []}


def _card(panel, frm, to, window=None, motion="pan_down", at=0.0):
    return {"panel": panel, "crop": window or frm, "viewport": {"window": window or frm, "from": frm, "to": to},
            "enter": "cut", "at": at, "motion": motion, "fullframe": True, "pad_y": 0.0}


FMT = format_from("16:9", 30, preview=True)   # 854x480, still exactly 16:9


def test_rendered_frames_are_the_viewports_full_bleed():
    panel = _banded_panel()
    vp_h = 506.25 / 1600                       # a 900-wide 16:9 viewport, in fractions
    card = _card("A", [0, 0, 1, vp_h], [0, 1 - vp_h, 1, 1], window=[0, 0, 1, 1])
    r = ShotRenderer(_ff_shot([card]), {"A": panel}, FMT)
    assert r.fullframe and r.cards == [] and len(r.views) == 1
    top = r.frame(0.0)                          # p=0: the FROM viewport (red band)
    bottom = r.frame(4.0)                       # p=1: the TO viewport (blue band)
    assert top.size == (854, 480)
    assert top.getpixel((427, 240)) == pytest.approx((200, 30, 30), abs=24)
    assert bottom.getpixel((427, 240)) == pytest.approx((30, 30, 200), abs=24)
    # full-bleed: the CORNERS are art, not backdrop or card border
    assert top.getpixel((2, 2)) == pytest.approx((200, 30, 30), abs=24)
    assert bottom.getpixel((851, 477)) == pytest.approx((30, 30, 200), abs=24)
    assert not r.is_static(2.0)


def test_rendered_pair_cuts_between_two_full_frame_views():
    a = Image.new("RGB", (900, 900), (200, 30, 30))
    b = Image.new("RGB", (900, 900), (30, 30, 200))
    full = [0, 0, 1, 1]
    cards = [_card("A", full, full, motion="hold"),
             _card("B", full, full, motion="hold", at=2.0)]
    r = ShotRenderer(_ff_shot(cards, layout="pair"), {"A": a, "B": b}, FMT)
    assert r.frame(1.0).getpixel((427, 240)) == pytest.approx((200, 30, 30), abs=24)
    assert r.frame(3.0).getpixel((427, 240)) == pytest.approx((30, 30, 200), abs=24)


def test_static_viewport_may_freeze_but_a_moving_one_may_not():
    panel = _banded_panel()
    full = [0, 0, 1, 0.5625]
    still = ShotRenderer(_ff_shot([_card("A", full, full, motion="hold")]), {"A": panel}, FMT)
    assert still.is_static(3.9)
    vp_h = 506.25 / 1600
    moving = ShotRenderer(_ff_shot([_card("A", [0, 0, 1, vp_h], [0, 1 - vp_h, 1, 1], window=[0, 0, 1, 1])]),
                          {"A": panel}, FMT)
    assert not moving.is_static(2.0)


def test_card_mode_is_unaffected_by_viewport_fields():
    # a classic shot (no shot-level fullframe flag) keeps the card path
    # even if a card happens to carry a viewport spec.
    panel = _banded_panel()
    card = _card("A", [0, 0, 1, 0.5], [0, 0.5, 1, 1])
    shot = _ff_shot([card])
    del shot["fullframe"]
    r = ShotRenderer(shot, {"A": panel}, FMT)
    assert not r.fullframe and len(r.cards) == 1


# ------------------------------------------------------------------ QA gate

def _panels_data():
    return {"panels": [{"id": "A", "width": 900, "height": 1600, "text_only": False},
                       {"id": "B", "width": 900, "height": 1600, "text_only": False}],
            "fingerprint": "q"}


def _quality(shots):
    for i, s in enumerate(shots):
        s.setdefault("start", float(i) * 4.0)
        s.setdefault("end", float(i) * 4.0 + 4.0)
    plan = {"format": "16:9", "fps": 30, "shots": shots, "crop_stats": {}}
    return build_mod.quality_report(plan, _panels_data(), None, min_hold=3.0)


def _ff_qa_card(frm, to, window=None):
    return {"panel": "A", "crop": window or frm, "pad_y": 0.0, "fullframe": True,
            "viewport": {"window": window or frm, "from": frm, "to": to}}


def test_qa_gate_accepts_a_true_169_viewport():
    vp_h = 506.25 / 1600
    shots = [{"id": "S001", "layout": "single", "fullframe": True,
              "cards": [_ff_qa_card([0, 0, 1, vp_h], [0, 1 - vp_h, 1, 1], window=[0, 0, 1, 1])]}]
    report = _quality(shots)
    assert report["failures"] == []


def test_qa_gate_rejects_a_non_169_viewport():
    shots = [{"id": "S001", "layout": "single", "fullframe": True,
              "cards": [_ff_qa_card([0, 0, 1, 0.5], [0, 0.5, 1, 1], window=[0, 0, 1, 1])]}]  # 900x800: not 16:9
    report = _quality(shots)
    assert report["failures"] and "allowed shapes" in report["failures"][0]


def test_qa_gate_rejects_a_viewport_outside_its_window():
    vp_h = 506.25 / 1600
    shots = [{"id": "S001", "layout": "single", "fullframe": True,
              "cards": [_ff_qa_card([0, 0, 1, vp_h], [0, 1 - vp_h, 1, 1], window=[0, 0.1, 1, 0.9])]}]
    report = _quality(shots)
    assert report["failures"]


def test_qa_gate_accepts_a_full_fullframe_plan():
    # The production gate itself: make_plan runs quality_report and refuses
    # the build on any failure, so a full-frame plan whose viewports ever
    # drifted off 16:9 (or out of their window) could never reach render.
    plan = _ff_plan()
    report = build_mod.quality_report(plan, {"panels": PANELS, "fingerprint": "f"}, None, min_hold=3.0)
    assert not any("allowed shapes" in f for f in report["failures"])


def test_config_defaults_match_the_code_constants():
    # The HARDCODE-IT pin: the style knobs live in config DEFAULTS and as
    # named code constants, and the two must never drift apart -- if one
    # side is edited without the other, this test fails.
    ff = load_config().video["fullframe"]
    assert ff["max_upscale"] == FF_MAX_UPSCALE
    assert ff["zoom_depth"] == FF_ZOOM_DEPTH
    assert ff["pan_travel"] == FF_PAN_TRAVEL
