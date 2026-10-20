"""video.mode: adaptive -- per shot, a naturally wide/landscape panel gets
the wide-mode treatment (full frame height, edge-safe window); a narrower
panel keeps its classic crop/shape but without the equal-area shrink that
otherwise forces every shape onto the same stale, small on-screen size.
Captions always overlay the art, never a dedicated bar, so the look stays
consistent across a video that mixes both treatments shot to shot.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.config import load_config
from manhwa_ocr.video.frames import Format, ShotRenderer, format_from
from manhwa_ocr.video.plan import _adaptive_pass, _Designer, build_plan

VCFG = load_config().video


def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 11):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


PANELS = [
    {"id": "P001-01", "width": 1200, "height": 600, "text": ""},   # aspect 2.0 -- wide-style
    {"id": "P001-02", "width": 700, "height": 500, "text": ""},    # aspect 1.4 -- wide-style (>=1.333)
    {"id": "P001-03", "width": 500, "height": 1400, "text": ""},   # aspect 0.36 -- classic-style, tall
    {"id": "P001-04", "width": 600, "height": 700, "text": ""},    # aspect 0.86 -- classic-style
    {"id": "P001-05", "width": 500, "height": 1300, "text": ""},   # portrait, for pairing with 06
    {"id": "P001-06", "width": 500, "height": 1300, "text": ""},
]


def _adaptive_plan(duration=24.0, panels=PANELS, acfg=None):
    vcfg = {**VCFG, "mode": "adaptive", "adaptive": acfg or {"wide_threshold": 1.333},
           "wide": {"min_width_ratio": 0.375, "max_upscale": 1.5}}
    sentences = [
        {"start": 0.0, "text": "one", "panels": ["P001-01"], "cues": [], "emphasis": []},
        {"start": 4.0, "text": "two", "panels": ["P001-02"], "cues": [], "emphasis": []},
        {"start": 8.0, "text": "three", "panels": ["P001-03"], "cues": [], "emphasis": []},
        {"start": 12.0, "text": "four", "panels": ["P001-04"], "cues": [], "emphasis": []},
    ]
    return build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "a"}, duration=duration,
                      audio=None, vcfg=vcfg, image_for=_img_for, sentences=sentences)


def test_wide_aspect_panel_gets_wide_display_narrow_gets_wide_fill():
    plan = _adaptive_plan()
    cards = {c["panel"]: c for s in plan["shots"] for c in s["cards"]}
    assert cards["P001-01"]["wide_display"] is True
    assert not cards["P001-01"].get("wide_fill")
    assert cards["P001-02"]["wide_display"] is True   # 1.4 >= 1.333 threshold
    assert cards["P001-03"].get("wide_fill") is True
    assert not cards["P001-03"].get("wide_display")
    assert cards["P001-04"].get("wide_fill") is True


def test_captions_always_overlay_regardless_of_per_shot_treatment():
    plan = _adaptive_plan()
    shots = plan["shots"]
    assert shots, "no shots produced"
    # some shots wide-style, some classic-style -- caption_bar is False for ALL of them
    assert any(s.get("wide") for s in shots)
    assert any(not s.get("wide") for s in shots)
    assert all(s["caption_bar"] is False for s in shots)


def test_threshold_is_configurable():
    # raise the threshold past 1.4: P001-02 (aspect 1.4) should fall back to classic-style
    plan = _adaptive_plan(acfg={"wide_threshold": 1.6})
    cards = {c["panel"]: c for s in plan["shots"] for c in s["cards"]}
    assert cards["P001-01"]["wide_display"] is True    # aspect 2.0, still above 1.6
    assert cards["P001-02"].get("wide_fill") is True   # aspect 1.4, now below 1.6
    assert not cards["P001-02"].get("wide_display")


def test_pair_cards_are_never_wide_style_even_if_aspect_crosses_the_threshold():
    # Direct unit test of _adaptive_pass on a hand-built pair shot (classic
    # pairing itself is probabilistic/RNG-gated, so it isn't reliable to
    # force through the full pipeline here): even with an absurdly low
    # threshold that would call every panel "wide", a pair layout must
    # still keep both its cards classic-style (wide_fill), never wide_display.
    fmt = Format(1920, 1080, 30)
    shots = [{"id": "S001", "layout": "pair", "cards": [
        {"panel": "P001-01", "crop": [0, 0, 1, 1], "motion": "push_in"},
        {"panel": "P001-02", "crop": [0, 0, 1, 1], "motion": "push_in"},
    ]}]
    designer = _Designer("1", PANELS, fmt, VCFG, _img_for)
    _adaptive_pass(shots, designer, fmt, {"wide_threshold": 0.1},
                   {"min_width_ratio": 0.375, "max_upscale": 1.5})
    for c in shots[0]["cards"]:
        assert c.get("wide_fill") is True
        assert not c.get("wide_display")
    assert shots[0]["wide"] is False


def test_classic_and_wide_modes_are_untouched_by_adaptive():
    classic = build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "c"}, duration=16.0,
                         audio=None, vcfg=VCFG, image_for=_img_for,
                         sentences=[{"start": 0.0, "text": "one", "panels": ["P001-01"], "cues": [], "emphasis": []},
                                    {"start": 4.0, "text": "two", "panels": ["P001-03"], "cues": [], "emphasis": []}])
    for s in classic["shots"]:
        for c in s["cards"]:
            assert not c.get("wide_display") and not c.get("wide_fill")


def test_portrait_format_is_untouched_by_adaptive_mode():
    vcfg = {**VCFG, "format": "9:16", "mode": "adaptive", "adaptive": {"wide_threshold": 1.333}}
    plan = build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "pp"}, duration=10.0,
                      audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=[{"start": 0.0, "text": "one", "panels": ["P001-01"], "cues": [], "emphasis": []}])
    for s in plan["shots"]:
        for c in s["cards"]:
            assert not c.get("wide_display")


# ------------------------------------------------------------------ frames.Card sizing


def test_wide_fill_card_is_not_shrunk_to_equal_area():
    # The whole point of adaptive's classic-style branch: a card sized to
    # its OWN shape, not forced to the equal-area rectangle every classic
    # shape otherwise shares.
    fmt = Format(1920, 1080, 30)
    panel = Image.new("RGB", (1200, 840), (80, 60, 50))  # 4:3-ish
    plain = {"start": 0, "end": 4, "layout": "single", "background": "blur",
            "cards": [{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "push_in"}],
            "exit": "cut", "effects": []}
    filled = {**plain, "cards": [{**plain["cards"][0], "wide_fill": True}]}
    r_plain = ShotRenderer(plain, {"A": panel}, fmt)
    r_filled = ShotRenderer(filled, {"A": panel}, fmt)
    assert r_filled.cards[0].cw * r_filled.cards[0].ch > r_plain.cards[0].cw * r_plain.cards[0].ch
