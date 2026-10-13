"""The deterministic video rebuild: fixed card shapes, one card area per layout,
a minimum hold per shot, every drawn panel shown, and a QA gate that stops a
build which would skip one. No audio or ffmpeg needed."""

import math

import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.util import UserError
from manhwa_ocr.video import align_engine
from manhwa_ocr.video.build import quality_report
from manhwa_ocr.video.crops import FIGURE_MIN, face_top, figure_share, shape_crop, choose_shape, _panel_maps
from manhwa_ocr.video.frames import LAYOUT_SHAPES, fit, format_from, layout_area, card_box, shapes_for
from manhwa_ocr.video.plan import MIN_HOLD, build_plan, even_units, hold_groups


def _art(w, h, base=(80, 100, 130)):
    """Drawn-looking art: a base colour with fine, deterministic texture."""
    import numpy as np

    rng = np.random.default_rng(7)
    blocks = rng.normal(0, 22, (h // 8 + 1, w // 8 + 1, 1)).repeat(8, axis=0).repeat(8, axis=1)[:h, :w]
    arr = np.clip(np.array(base, dtype=np.float32) + blocks, 0, 255).astype("uint8")
    return Image.fromarray(arr, "RGB")


def _blank_top(w, h, band):
    img = _art(w, h)
    ImageDraw.Draw(img).rectangle((0, 0, w, int(band * h)), fill=(255, 255, 255))
    return img


def _face(w, h, top):
    img = _art(w, h, base=(60, 60, 90))
    ImageDraw.Draw(img).rectangle((int(0.3 * w), int(top * h), int(0.7 * w), int((top + 0.25) * h)),
                                  fill=(230, 180, 150))
    return img


def _aspect(box, w, h):
    x0, y0, x1, y1 = box
    return ((x1 - x0) * w) / ((y1 - y0) * h)


@pytest.mark.parametrize("target", [0.5625, 0.75, 1.0, 1.333, 1.777])
def test_crop_is_exactly_the_target_shape(target):
    for img in (_art(900, 2400), _art(1600, 1000), _art(900, 900)):
        box, _, _ = shape_crop(img, target)
        assert _aspect(box, img.width, img.height) == pytest.approx(target, rel=0.01)


def test_crop_avoids_a_white_caption_band_at_the_top():
    img = _blank_top(900, 2000, band=0.3)
    box, _, _ = shape_crop(img, 0.75)
    assert box[1] >= 0.29          # the window starts below the white band


def test_crop_never_cuts_a_detected_head():
    img = _face(900, 2000, top=0.35)
    maps = _panel_maps(img)
    fe = face_top(maps)
    assert fe is not None and 0.3 < fe < 0.4
    for target in (0.75, 1.0, 1.333):
        box, _, ok = shape_crop(img, target, maps)
        assert ok and box[1] <= fe - 0.04 + 1e-6


def test_choose_shape_returns_an_allowed_shape():
    img = _art(900, 2400)
    shape, box, _ = choose_shape(img, shapes_for("single", format_from("16:9", 30)))
    assert shape in LAYOUT_SHAPES[False]["single"]


@pytest.mark.parametrize("layout", ["single", "wide", "pair"])
def test_every_shape_in_a_layout_has_the_same_card_area(layout):
    fmt = format_from("16:9", 30)
    area = layout_area(layout, fmt)
    bw, bh = card_box(layout, fmt)
    for a in shapes_for(layout, fmt):
        cw, ch = fit(a, (bw, bh), area)
        assert cw <= bw + 1 and ch <= bh + 1
        assert cw * ch == pytest.approx(area, rel=0.03)


def test_hold_groups_gives_every_shot_the_minimum_and_keeps_every_panel():
    units = [{"start": 0.0, "end": 1.0, "panels": ["A"], "cues": [], "emphasis": [], "sentence": "a",
              "sentence_starts": [0.0]},
             {"start": 1.0, "end": 2.0, "panels": ["B"], "cues": [], "emphasis": [], "sentence": "b",
              "sentence_starts": [1.0]},
             {"start": 2.0, "end": 9.0, "panels": ["C"], "cues": [], "emphasis": [], "sentence": "c",
              "sentence_starts": [2.0]}]
    shots, stats = hold_groups(units, MIN_HOLD)
    assert all(s["end"] - s["start"] >= MIN_HOLD - 1e-6 for s in shots)
    assert {p for s in shots for p in s["panels"]} == {"A", "B", "C"}
    assert shots[0]["start"] == 0.0 and shots[-1]["end"] == pytest.approx(9.0)
    assert all(len(s["panels"]) <= 2 for s in shots)


def test_hold_groups_refuses_when_the_narration_cannot_hold_the_panels():
    units = [{"start": float(i), "end": float(i + 1), "panels": [f"P{i}"], "cues": [], "emphasis": [],
              "sentence": None, "sentence_starts": [float(i)]} for i in range(5)]
    with pytest.raises(UserError, match="too short"):
        hold_groups(units, MIN_HOLD)      # 5 s of narration cannot hold 5 panels for 3 s each


def test_even_timing_shows_every_drawn_panel_and_skips_only_text():
    panels = [{"id": f"P001-{i:02d}", "width": 300, "height": 500, "text": ""} for i in range(1, 9)]
    panels[3]["text_only"] = True
    plan = build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "x"}, duration=60.0,
                      audio=None, vcfg={"min_hold": 3.0, "format": "16:9", "fps": 30},
                      image_for=lambda p: _art(300, 500))
    shown = [c["panel"] for s in plan["shots"] for c in s["cards"]]
    assert sorted(set(shown)) == sorted(p["id"] for p in panels if not p.get("text_only"))
    assert all(s["end"] - s["start"] >= 3.0 - 1e-6 for s in plan["shots"])


def _segments(n_shows, panels_order):
    return [{"sentence": i + 1, "selected_panel": panels_order[i], "display_action": "SHOW",
             "start": 3.0 * i, "end": 3.0 * (i + 1)} for i in range(n_shows)]


def test_coverage_places_every_unshown_art_panel_in_story_order():
    panels = [{"id": f"P001-{i:02d}", "text_only": False} for i in range(1, 7)]
    segs = _segments(2, ["P001-01", "P001-05"])
    cov = align_engine.place_coverage(segs, panels, 12.0, 3.0)
    assert cov["uncovered"] == []
    assert sorted(cov["placed_by_order"]) == ["P001-02", "P001-03", "P001-04", "P001-06"]
    for s in segs:
        assert s["sequence"] == sorted(s["sequence"])
    shown = {p for s in segs for p in s["sequence"]}
    assert shown == {p["id"] for p in panels}


def test_coverage_never_places_text_only_panels():
    panels = [{"id": "P001-01", "text_only": False}, {"id": "P001-02", "text_only": True},
              {"id": "P001-03", "text_only": False}]
    segs = _segments(1, ["P001-01"])
    cov = align_engine.place_coverage(segs, panels, 6.0, 3.0)
    assert "P001-02" not in segs[0]["sequence"]
    assert cov["placed_by_order"] == ["P001-03"]


def test_coverage_reports_panels_the_narration_has_no_time_for():
    panels = [{"id": f"P001-{i:02d}", "text_only": False} for i in range(1, 12)]
    segs = _segments(1, ["P001-01"])
    cov = align_engine.place_coverage(segs, panels, 6.0, 3.0)   # 6 s holds at most 4 panels
    assert len(cov["uncovered"]) > 0
    assert len(cov["placed_by_order"]) + 1 + len(cov["uncovered"]) == 11


def test_quality_gate_stops_a_build_that_would_skip_a_panel():
    fmt = format_from("16:9", 30)
    panels = {"panels": [{"id": "A", "width": 300, "height": 300, "text_only": False},
                         {"id": "B", "width": 300, "height": 300, "text_only": False}]}
    plan = {"format": "16:9", "fps": 30, "chapter": "1", "shots": [
        {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single",
         "cards": [{"panel": "A", "crop": [0, 0, 1, 1]}]}]}
    qa = quality_report(plan, panels, None, 3.0)
    assert qa["missing"] == ["B"]
    assert any("would never appear" in f for f in qa["failures"])


def test_quality_gate_flags_a_shot_under_the_minimum_and_a_wrong_shape():
    panels = {"panels": [{"id": "A", "width": 300, "height": 300, "text_only": False}]}
    plan = {"format": "16:9", "fps": 30, "chapter": "1", "shots": [
        {"id": "S001", "start": 0.0, "end": 1.0, "layout": "single",
         "cards": [{"panel": "A", "crop": [0, 0, 1, 0.5]}]}]}     # 2:1 card: not an allowed shape
    qa = quality_report(plan, panels, None, 3.0)
    assert any("allowed shapes" in f for f in qa["failures"])
    assert any("under the 3s minimum" in f for f in qa["failures"])


def test_figure_share_sees_a_character_and_not_a_blank_page():
    assert figure_share(_face(400, 800, top=0.3)) > FIGURE_MIN
    assert figure_share(Image.new("RGB", (400, 800), (255, 255, 255))) < FIGURE_MIN
