"""Wide edit mode (video.mode: wide): full-height cards, min 6/16 wide,
captions overlay the art. See docs/AGENT_WAY.md and the wide-mode spec.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.config import load_config
from manhwa_ocr.video import build as build_mod
from manhwa_ocr.video.crops import extend_figure_range, head_of, _panel_maps
from manhwa_ocr.video.frames import Format, ShotRenderer, format_from
from manhwa_ocr.video.plan import WIDE_MIN_ASPECT, build_plan, wide_crop

VCFG = load_config().video


def _wcfg(**over):
    return {"min_width_ratio": 0.375, "max_upscale": 1.5, **over}


# ------------------------------------------------------------------ wide_crop


def test_window_contains_the_focal_subject():
    # subject in the lower-middle third of a tall, narrow panel
    box = [0.2, 0.55, 0.8, 0.75]
    crop, info = wide_crop(900, 2406, box, min_win_h=720)
    x0, y0, x1, y1 = crop
    assert x0 <= box[0] + 1e-6 and x1 >= box[2] - 1e-6
    assert y0 <= box[1] + 1e-6 and y1 >= box[3] - 1e-6
    assert not info["whole"]


def test_very_tall_panel_window_is_exactly_6_over_16():
    # a subject spanning most of the panel forces the single floor:
    # window height = panel width / WIDE_MIN_ASPECT (6/16 at full height).
    crop, info = wide_crop(900, 2406, [0.0, 0.0, 1.0, 1.0], min_win_h=720)
    x0, y0, x1, y1 = crop
    win_w, win_h = (x1 - x0) * 900, (y1 - y0) * 2406
    assert win_h == pytest.approx(900 / WIDE_MIN_ASPECT, rel=0.01)
    assert win_w / win_h == pytest.approx(WIDE_MIN_ASPECT, rel=0.02)
    assert not info["whole"]


def test_panel_aspect_at_or_above_6_over_16_is_shown_whole():
    crop, info = wide_crop(720, 1014, [0.3, 0.3, 0.7, 0.7], min_win_h=720)  # aspect 0.71
    assert crop == [0.0, 0.0, 1.0, 1.0]
    assert info["whole"]


def test_super_wide_panel_is_also_shown_whole():
    crop, info = wide_crop(1800, 600, [0.3, 0.2, 0.7, 0.8], min_win_h=720)  # aspect 3.0
    assert crop == [0.0, 0.0, 1.0, 1.0]
    assert info["whole"]


def test_bottom_anchored_subject_window_stays_inside_the_panel():
    box = [0.1, 0.92, 0.9, 0.99]       # subject hugs the very bottom
    crop, _ = wide_crop(900, 2400, box, min_win_h=900)
    x0, y0, x1, y1 = crop
    assert 0.0 <= y0 and y1 <= 1.0 + 1e-9
    assert y1 >= box[3] - 1e-6 and y0 <= box[1] + 1e-6


def test_pair_window_aspect_is_exactly_0_889():
    crop, _ = wide_crop(600, 1800, [0.1, 0.3, 0.9, 0.6], min_win_h=720, pair_aspect=0.889)
    x0, y0, x1, y1 = crop
    win_w, win_h = (x1 - x0) * 600, (y1 - y0) * 1800
    assert win_w / win_h == pytest.approx(0.889, abs=0.005)


def test_pair_window_clamps_to_panel_width_when_narrower_than_the_window():
    # a panel narrower than the pair window: width wins, height follows the aspect.
    crop, _ = wide_crop(400, 2000, [0.2, 0.4, 0.8, 0.6], min_win_h=900, pair_aspect=0.889)
    x0, y0, x1, y1 = crop
    win_w, win_h = (x1 - x0) * 400, (y1 - y0) * 2000
    assert win_w == pytest.approx(400, abs=1)
    assert win_w / win_h == pytest.approx(0.889, abs=0.01)


def test_min_win_h_floor_is_respected_until_the_upscale_cap():
    # a small subject in a tall, narrow panel: the window still grows to the
    # min_win_h floor (the upscale cap), not just to the subject's own size.
    crop, _ = wide_crop(600, 2000, [0.3, 0.3, 0.7, 0.4], min_win_h=720)
    x0, y0, x1, y1 = crop
    win_h = (y1 - y0) * 2000
    assert win_h == pytest.approx(720, abs=2)


def test_window_edge_avoids_slicing_a_bubble():
    # A bright, lettered "bubble" sits just above the subject. A window
    # top-anchored exactly at the subject's top would land its own top
    # edge inside the bubble; the edge-safe nudge must clear it instead.
    panel = Image.new("RGB", (300, 800), (90, 90, 90))
    d = ImageDraw.Draw(panel)
    d.rectangle((20, 120, 280, 200), fill=(255, 255, 255), outline=(10, 10, 10), width=3)
    for i, y in enumerate(range(140, 190, 14)):
        d.line((40, y, 260, y), fill=(20, 20, 20), width=4)
    d.rectangle((60, 260, 240, 560), fill=(150, 90, 70))  # the "subject"
    box = [0.2, 0.33, 0.8, 0.70]  # subject box, top sitting right under the bubble
    crop, _ = wide_crop(300, 800, box, min_win_h=400, img=panel)
    x0, y0, x1, y1 = crop
    top_px, bottom_px = y0 * 800, y1 * 800
    # the bubble occupies roughly rows 118-203 (incl. the 2px margin bubble_boxes
    # adds); the window's top edge must not land strictly inside that band.
    assert not (122 < top_px < 198)


# ------------------------------------------------------------------ full-body hardening


def _long_figure_panel(w=300, h=1200, body_top=100, body_bottom=1000):
    """A tall panel: flat background, with a textured, skin-toned body from
    body_top to body_bottom -- simulating a figure a head-only crop misses."""
    img = Image.new("RGB", (w, h), (235, 235, 235))
    d = ImageDraw.Draw(img)
    for y in range(body_top, body_bottom, 6):
        d.line((20, y, w - 20, y), fill=(205, 145, 100))
        d.line((20 + (y % 12), y + 2, w - 20, y + 2), fill=(170, 110, 70))
    return img


def test_extend_figure_range_grows_to_cover_the_whole_body():
    img = _long_figure_panel()
    # a head-only range (just the first slice of the textured body)
    top, bottom = extend_figure_range(img, 100 / 1200, 160 / 1200)
    # must grow to roughly cover the drawn body (100-1000), not stay at 160
    assert bottom >= 900 / 1200
    assert top <= 100 / 1200


def test_extend_figure_range_stops_at_a_real_empty_gap():
    img = _long_figure_panel(body_top=100, body_bottom=400)
    # a second, separate textured block far below, with a long empty gap
    d = ImageDraw.Draw(img)
    for y in range(900, 1100, 6):
        d.line((20, y, 280, y), fill=(90, 60, 50))
    top, bottom = extend_figure_range(img, 100 / 1200, 160 / 1200)
    # must not jump the empty gap to swallow the unrelated second block
    assert bottom < 900 / 1200


def test_extend_figure_range_never_shrinks_the_input():
    img = Image.new("RGB", (300, 800), (255, 255, 255))   # nothing drawn at all
    top, bottom = extend_figure_range(img, 0.3, 0.5)
    assert top <= 0.3 and bottom >= 0.5


def test_wide_crop_flags_figure_risk_when_the_subject_cannot_fit():
    # a subject spanning almost the whole height of a panel whose width
    # forces a much shorter window (upscale cap): cannot be fully contained.
    crop, info = wide_crop(300, 3000, [0.1, 0.02, 0.9, 0.98], min_win_h=200)
    assert info["figure_risk"] is True
    x0, y0, x1, y1 = crop
    assert 0.0 <= y0 < y1 <= 1.0  # still a valid window, just flagged


def test_wide_crop_no_figure_risk_when_the_subject_fits():
    crop, info = wide_crop(300, 900, [0.1, 0.3, 0.9, 0.6], min_win_h=400)
    assert info["figure_risk"] is False


def test_wide_pass_recovers_a_full_body_crop_the_classic_box_missed():
    # A panel where the classic focal crop (simulated) is just the head's
    # own narrow strip; head_of should find the whole textured figure below
    # it, and wide_crop's window should end up covering most of it --
    # the regression this hardening pass fixes (dragons cut mid-body).
    img = _long_figure_panel(w=600, h=2400, body_top=200, body_bottom=2100)
    classic_box = [0.0, 200 / 2400, 1.0, 320 / 2400]   # head-only, like a bad classic pick
    head = head_of(_panel_maps(img, {}))
    box = list(classic_box)
    if head.get("figure"):
        top, bottom = extend_figure_range(img, float(head["top"]), float(head["bottom"]))
        box[1] = min(box[1], top)
        box[3] = max(box[3], bottom)
    crop, info = wide_crop(600, 2400, box, min_win_h=720)
    x0, y0, x1, y1 = crop
    # the window must now reach well past the head-only classic box -- up to
    # the max height a single-card aspect (6/16) allows for this panel width
    assert y1 - y0 > (320 - 200) / 2400 * 3   # much taller than the head-only strip
    assert y1 - y0 == pytest.approx((600 / WIDE_MIN_ASPECT) / 2400, rel=0.02)
    # the figure (200-2100) is taller than any single window this panel
    # width allows, so it is honestly flagged rather than silently cut.
    assert info["figure_risk"] is True


# ------------------------------------------------------------------ _wide_pass integration


PANELS = [
    {"id": "P001-01", "width": 900, "height": 500, "text": ""},   # whole-panel candidate (1.8 aspect)
    {"id": "P001-02", "width": 500, "height": 1400, "text": ""},  # narrow: windowed
    {"id": "P001-03", "width": 500, "height": 1400, "text": ""},
    {"id": "P001-04", "width": 600, "height": 1200, "text": ""},
]


def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 11):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


def _wide_plan(duration=24.0, panels=PANELS, mode_cfg=None):
    vcfg = {**VCFG, "mode": "wide", "wide": mode_cfg or _wcfg()}
    sentences = [
        {"start": 0.0, "text": "one", "panels": ["P001-01"], "cues": [], "emphasis": []},
        {"start": 4.0, "text": "two", "panels": ["P001-02"], "cues": [], "emphasis": []},
        {"start": 8.0, "text": "three", "panels": ["P001-03"], "cues": [], "emphasis": []},
        {"start": 12.0, "text": "four", "panels": ["P001-04"], "cues": [], "emphasis": []},
    ]
    return build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "w"}, duration=duration,
                      audio=None, vcfg=vcfg, image_for=_img_for, sentences=sentences)


def test_wide_pass_sets_wide_fields_on_every_card():
    plan = _wide_plan()
    assert plan["mode"] == "wide"
    assert isinstance(plan.get("wide_caption_risk"), list)
    assert isinstance(plan.get("wide_figure_risk"), list)
    shots = plan["shots"]
    assert shots, "plan produced no shots"
    for s in shots:
        assert s["wide"] is True
        assert s["caption_bar"] is False
        assert s["layout"] in ("single", "pair")
        for c in s["cards"]:
            assert c["wide_display"] is True
            assert c["pad_y"] == 0.0
            x0, y0, x1, y1 = c["crop"]
            assert 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0


def test_classic_plan_is_untouched_by_wide_fields():
    plan = build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "c"}, duration=16.0,
                      audio=None, vcfg=VCFG, image_for=_img_for,
                      sentences=[{"start": 0.0, "text": "one", "panels": ["P001-01"], "cues": [], "emphasis": []},
                                 {"start": 4.0, "text": "two", "panels": ["P001-02"], "cues": [], "emphasis": []}])
    assert plan.get("mode", "classic") == "classic"
    for s in plan["shots"]:
        assert not s.get("wide")
        for c in s["cards"]:
            assert not c.get("wide_display")


def test_portrait_format_is_untouched_by_wide_mode():
    vcfg = {**VCFG, "format": "9:16", "mode": "wide", "wide": _wcfg()}
    plan = build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "p"}, duration=10.0,
                      audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=[{"start": 0.0, "text": "one", "panels": ["P001-01"], "cues": [], "emphasis": []}])
    for s in plan["shots"]:
        assert not s.get("wide")


# ------------------------------------------------------------------ frames.Card sizing


def _wide_shot(panel_aspect, layout="single", cards=None):
    return {
        "start": 0, "end": 4, "layout": layout, "background": "blur", "wide": True, "caption_bar": False,
        "cards": cards or [{"panel": "A", "crop": [0.1, 0.1, 0.9, 0.9], "enter": "cut", "at": 0,
                            "motion": "push_in", "wide_display": True, "pad_y": 0.0}],
        "exit": "cut", "effects": [],
    }


def test_single_wide_card_is_at_least_6_over_16_wide():
    fmt = Format(1920, 1080, 30)
    panel = Image.new("RGB", (400, 1600), (80, 40, 40))  # tall, narrow -> hits the min-width floor
    shot = _wide_shot(0.25)
    r = ShotRenderer(shot, {"A": panel}, fmt)
    card = r.cards[0]
    assert card.wide_display
    assert card.ch == fmt.height
    assert card.cw >= round(0.375 * fmt.width)


def test_pair_wide_cards_are_exactly_half_the_frame_each():
    fmt = Format(1920, 1080, 30)
    panel = Image.new("RGB", (900, 1200), (60, 60, 90))
    cards = [
        {"panel": "A", "crop": [0.1, 0.1, 0.9, 0.9], "enter": "cut", "at": 0, "motion": "push_in",
         "wide_display": True, "pad_y": 0.0},
        {"panel": "A", "crop": [0.0, 0.2, 1.0, 0.8], "enter": "cut", "at": 1.0, "motion": "push_in",
         "wide_display": True, "pad_y": 0.0},
    ]
    shot = _wide_shot(None, layout="pair", cards=cards)
    r = ShotRenderer(shot, {"A": panel}, fmt)
    for card in r.cards:
        assert card.ch == fmt.height
        assert card.cw == fmt.width // 2


def test_wide_zoom_amplitude_is_halved():
    fmt = Format(1920, 1080, 30)
    panel = Image.new("RGB", (900, 1200), (60, 60, 90))
    cards = [{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "push_in",
             "wide_display": True, "pad_y": 0.0, "wide_zoom_scale": 0.5}]
    wide_shot = _wide_shot(None, cards=cards)
    classic_cards = [{**cards[0], "wide_display": False}]
    classic_shot = {**wide_shot, "wide": False, "caption_bar": True, "cards": classic_cards}
    rw = ShotRenderer(wide_shot, {"A": panel}, fmt)
    rc = ShotRenderer(classic_shot, {"A": panel}, fmt)
    # both push_in from 1.0; at the same point in the shot the wide card's
    # viewport has zoomed in by about half as much as the classic one.
    _, _, vw_wide, _ = rw.cards[0]._viewport(2.0, 0.0)
    _, _, vw_classic, _ = rc.cards[0]._viewport(2.0, 0.0)
    sw = rw.cards[0].src.size[0]
    sc = rc.cards[0].src.size[0]
    zoom_wide = sw / vw_wide
    zoom_classic = sc / vw_classic
    assert (zoom_wide - 1.0) == pytest.approx((zoom_classic - 1.0) * 0.5, rel=0.05)


def test_whole_panel_edge_text_gets_extra_zoom_protection():
    cards_normal = [{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "push_in",
                     "wide_display": True, "pad_y": 0.0, "wide_zoom_scale": 0.5}]
    cards_capped = [{**cards_normal[0], "wide_zoom_scale": 0.12}]
    fmt = Format(1920, 1080, 30)
    panel = Image.new("RGB", (1800, 900), (60, 60, 90))
    r1 = ShotRenderer(_wide_shot(None, cards=cards_normal), {"A": panel}, fmt)
    r2 = ShotRenderer(_wide_shot(None, cards=cards_capped), {"A": panel}, fmt)
    _, _, vw1, _ = r1.cards[0]._viewport(2.0, 0.0)
    _, _, vw2, _ = r2.cards[0]._viewport(2.0, 0.0)
    sw = r1.cards[0].src.size[0]
    assert (sw / vw2 - 1.0) < (sw / vw1 - 1.0)


# ------------------------------------------------------------------ build.py QA gate


def _panels_data():
    return {"panels": [{"id": "A", "width": 500, "height": 1600, "text_only": False},
                       {"id": "B", "width": 500, "height": 1600, "text_only": False}]}


def _quality(shots, fmt="16:9"):
    for i, s in enumerate(shots):
        s.setdefault("start", float(i) * 4.0)
        s.setdefault("end", float(i) * 4.0 + 4.0)
    plan = {"format": fmt, "fps": 30, "shots": shots, "crop_stats": {}}
    return build_mod.quality_report(plan, _panels_data(), None, min_hold=3.0)


def test_qa_gate_wide_single_window_passes_the_wide_floor():
    shots = [{"id": "S001", "wide": True, "layout": "single",
             "cards": [{"panel": "A", "crop": [0.1, 0.1, 0.9, 0.55], "pad_y": 0.0}]}]
    # aspect = 0.8*500 / (0.45*1600) = 400/720 = 0.555 -- below 0.66, should FAIL
    report = _quality(shots)
    assert any("A" in bad for bad in report["failures"][0:1]) or report["failures"]


def test_qa_gate_wide_single_window_above_floor_passes():
    shots = [{"id": "S001", "wide": True, "layout": "single",
             "cards": [{"panel": "A", "crop": [0.0, 0.1, 1.0, 0.63], "pad_y": 0.0}]}]
    # aspect = 1.0*500 / (0.53*1600) = 500/848 = 0.59 -- still under 0.66; use a wider one
    shots[0]["cards"][0]["crop"] = [0.0, 0.3, 1.0, 0.65]
    # aspect = 500 / (0.35*1600) = 500/560 = 0.89 -- passes
    report = _quality(shots)
    assert report["failures"] == []


def test_qa_gate_wide_pair_requires_0_889_aspect():
    good = [{"id": "S001", "wide": True, "layout": "pair",
            "cards": [{"panel": "A", "crop": [0.0, 0.3, 1.0, 0.925], "pad_y": 0.0},   # 500/(0.625*1600)=0.5 -> no
                     {"panel": "B", "crop": [0.0, 0.3, 1.0, 0.925], "pad_y": 0.0}]}]
    # pick a crop whose aspect really is 0.889: width/height = 0.889 ->
    # height = width/0.889; with pw=500 -> height=562.4px -> frac=562.4/1600=0.3515
    good[0]["cards"][0]["crop"] = [0.0, 0.0, 1.0, 0.3515]
    good[0]["cards"][1]["crop"] = [0.0, 0.0, 1.0, 0.3515]
    report = _quality(good)
    assert report["failures"] == []

    bad = [{"id": "S002", "wide": True, "layout": "pair",
           "cards": [{"panel": "A", "crop": [0.0, 0.0, 1.0, 0.5], "pad_y": 0.0},
                    {"panel": "B", "crop": [0.0, 0.0, 1.0, 0.5], "pad_y": 0.0}]}]
    report_bad = _quality(bad)
    assert report_bad["failures"]


def test_qa_gate_classic_plan_still_uses_classic_shapes():
    fmt = format_from("16:9", 30)
    allowed = build_mod.shapes_for("single", fmt)
    # a crop whose aspect is NOT one of the classic single shapes must fail
    bad_aspect = allowed[0] * 1.4
    h_frac = 1.0 / bad_aspect * (500 / 1600)
    shots = [{"id": "S001", "layout": "single",
             "cards": [{"panel": "A", "crop": [0.0, 0.0, 1.0, min(0.99, h_frac)], "pad_y": 0.0}]}]
    report = _quality(shots)
    assert report["failures"]
