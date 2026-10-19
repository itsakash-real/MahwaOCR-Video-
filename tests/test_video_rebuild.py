"""The deterministic video rebuild: fixed card shapes, one card area per layout,
a minimum hold per shot, every drawn panel shown, and a QA gate that stops a
build which would skip one. No audio or ffmpeg needed."""

import math

import pytest
from PIL import Image, ImageDraw

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
        box, _, _, _ = shape_crop(img, target)
        assert _aspect(box, img.width, img.height) == pytest.approx(target, rel=0.01)


def test_crop_avoids_a_white_caption_band_at_the_top():
    img = _blank_top(900, 2000, band=0.3)
    box, _, _, _ = shape_crop(img, 0.75)
    assert box[1] >= 0.29          # the window starts below the white band


def test_crop_never_cuts_a_detected_head():
    img = _face(900, 2000, top=0.35)
    maps = _panel_maps(img)
    fe = face_top(maps)
    assert fe is not None and 0.3 < fe < 0.4
    for target in (0.75, 1.0, 1.333):
        box, _, ok, _ = shape_crop(img, target, maps)
        assert ok and box[1] <= fe - 0.04 + 1e-6


def test_choose_shape_returns_an_allowed_shape():
    img = _art(900, 2400)
    shape, box, _, _ = choose_shape(img, shapes_for("single", format_from("16:9", 30)))
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


def test_hold_groups_reaches_the_short_floor_and_keeps_every_panel():
    units = [{"start": 0.0, "end": 1.0, "panels": ["A"], "cues": [], "emphasis": [], "sentence": "a",
              "sentence_starts": [0.0]},
             {"start": 1.0, "end": 2.0, "panels": ["B"], "cues": [], "emphasis": [], "sentence": "b",
              "sentence_starts": [1.0]},
             {"start": 2.0, "end": 9.0, "panels": ["C"], "cues": [], "emphasis": [], "sentence": "c",
              "sentence_starts": [2.0]}]
    shots, stats = hold_groups(units, MIN_HOLD)
    # A and B pair into one 2.3s shot (nudged to the cut budget, still under
    # the 3.0s floor: there is no shorter floor anymore, so it is counted as
    # a short shot and the sync gate fails on it); C keeps its 6.7s.
    assert shots[0]["panels"] == ["A", "B"]
    assert shots[0]["end"] - shots[0]["start"] == pytest.approx(2.3)
    assert shots[1]["end"] - shots[1]["start"] == pytest.approx(6.7)
    assert {p for s in shots for p in s["panels"]} == {"A", "B", "C"}
    assert shots[0]["start"] == 0.0 and shots[-1]["end"] == pytest.approx(9.0)
    assert all(len(s["panels"]) <= 2 for s in shots)
    assert stats["short_shots"] == 1
    # Every cut sits within its budget of the unit it belongs to.
    for s in shots[1:]:
        s0 = s["sentence_starts"][0]
        assert s0 - 0.2 - 1e-6 <= s["start"] <= s0 + 0.3 + 1e-6


def test_hold_groups_never_drifts_a_cut_outside_its_budget():
    # The old donor pass moved boundaries by seconds to reach min_hold; the new
    # budget forbids that. 5 x 1s units: pairs of 2s, one short shot remains.
    units = [{"start": float(i), "end": float(i + 1), "panels": [f"P{i}"], "cues": [], "emphasis": [],
              "sentence": None, "sentence_starts": [float(i)]} for i in range(5)]
    shots, stats = hold_groups(units, MIN_HOLD)
    assert {p for s in shots for p in s["panels"]} == {f"P{i}" for i in range(5)}
    for s in shots[1:]:
        s0 = s["sentence_starts"][0]
        assert s0 - 0.2 - 1e-6 <= s["start"] <= s0 + 0.3 + 1e-6
    assert stats["short_shots"] >= 1          # honestly reported, not hidden by drift
    # Two nudged boundaries, each moving at most lead + max_late (0.5s):
    # with the 3.0s floor the shortfalls are bigger than under the old
    # 2.0s short floor, so the total can reach 1.0s. Every cut still sits
    # inside its own budget (asserted above).
    assert stats["moved_seconds"] <= 1.0 + 1e-6


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


def test_impactful_selection_shows_only_tagged_panels_for_short_sentences():
    # SELECT, don't cover: a <=8s sentence shows ONLY its tagged panel.
    # The unpicked panels are not placed anywhere (no coverage pass).
    panels = [{"id": f"P001-{i:02d}", "text_only": False} for i in range(1, 7)]
    segs = _segments(2, ["P001-01", "P001-05"])
    cov = align_engine.place_impactful(segs, panels, 12.0, 3.0)
    assert cov["uncovered"] == []
    assert cov["placed_by_order"] == []
    for s in segs:
        assert s["sequence"] == [s["selected_panel"]]
        assert s["tagged_coverage"] == 1.0
    shown = {p for s in segs for p in s["sequence"]}
    assert shown == {"P001-01", "P001-05"}
    # panels_shown/total is INFO ONLY: 2/6 shown is not a failure.
    assert cov["panels_shown"] == ["P001-01", "P001-05"]
    assert cov["panels_total"] == 6


def test_impactful_selection_never_places_text_only_panels():
    panels = [{"id": "P001-01", "text_only": False}, {"id": "P001-02", "text_only": True},
              {"id": "P001-03", "text_only": False}]
    segs = _segments(1, ["P001-01"])
    cov = align_engine.place_impactful(segs, panels, 6.0, 3.0)
    assert "P001-02" not in segs[0]["sequence"]
    assert cov["placed_by_order"] == []


def test_impactful_selection_reports_unshown_panels_as_info_only():
    # Unshown panels are not "uncovered" failures anymore: uncovered is
    # always empty, and the QA ratio just reports what was selected.
    panels = [{"id": f"P001-{i:02d}", "text_only": False} for i in range(1, 12)]
    segs = _segments(1, ["P001-01"])
    cov = align_engine.place_impactful(segs, panels, 6.0, 3.0)
    assert cov["uncovered"] == []
    assert cov["panels_shown"] == ["P001-01"]
    assert cov["panels_shown_ratio"] == pytest.approx(1 / 11, abs=1e-4)  # rounded to 4dp


def test_quality_gate_no_longer_fails_on_a_skipped_panel():
    # Rule 9: panels_shown/total is INFO ONLY. A drawn panel no sentence
    # picked is not a failure; a bad card shape still is.
    fmt = format_from("16:9", 30)
    panels = {"panels": [{"id": "A", "width": 300, "height": 300, "text_only": False},
                         {"id": "B", "width": 300, "height": 300, "text_only": False}]}
    plan = {"format": "16:9", "fps": 30, "chapter": "1", "shots": [
        {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single",
         "cards": [{"panel": "A", "crop": [0, 0, 1, 1]}]}]}
    qa = quality_report(plan, panels, None, 3.0)
    assert qa["missing"] == ["B"]
    assert qa["panels_shown"] == 1 and qa["panels_total"] == 2
    assert qa["panels_shown_ratio"] == 0.5
    assert not any("would never appear" in f for f in qa["failures"])
    assert qa["failures"] == []


def test_quality_gate_flags_a_too_short_shot_and_a_wrong_shape():
    panels = {"panels": [{"id": "A", "width": 300, "height": 300, "text_only": False}]}
    plan = {"format": "16:9", "fps": 30, "chapter": "1", "shots": [
        {"id": "S001", "start": 0.0, "end": 1.0, "layout": "single",
         "cards": [{"panel": "A", "crop": [0, 0, 1, 0.5]}]}]}     # 2:1 card: not an allowed shape
    qa = quality_report(plan, panels, None, 3.0)
    assert any("allowed shapes" in f for f in qa["failures"])
    # dense narration: a shot under the floor warns (short_shots/shortest), it
    # does not stop the build; only a shot nobody can see is a failure
    assert not any("too short" in f for f in qa["failures"])
    assert qa["shortest_shot"] == 1.0 and qa["floor"] == 3.0


def test_quality_gate_stops_a_shot_too_short_to_be_seen():
    panels = {"panels": [{"id": "A", "width": 300, "height": 300, "text_only": False}]}
    plan = {"format": "16:9", "fps": 30, "chapter": "1", "shots": [
        {"id": "S001", "start": 0.0, "end": 0.15, "layout": "single",
         "cards": [{"panel": "A", "crop": [0, 0, 1, 1]}]}]}
    qa = quality_report(plan, panels, None, 3.0)
    assert any("too short to be seen" in f for f in qa["failures"])


def test_figure_share_sees_a_character_and_not_a_blank_page():
    assert figure_share(_face(400, 800, top=0.3)) > FIGURE_MIN
    assert figure_share(Image.new("RGB", (400, 800), (255, 255, 255))) < FIGURE_MIN


def _bubble_panel(w=900, h=1600):
    img = _art(w, h)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((int(0.52 * w), int(0.10 * h), int(0.92 * w), int(0.30 * h)), radius=60, fill=(255, 255, 255))
    for y in range(int(0.13 * h), int(0.28 * h), 30):      # dark lettering rows, chunky like real type
        d.rectangle((int(0.56 * w), y, int(0.88 * w), y + 14), fill=(20, 20, 20))
    return img


def test_bubble_with_lettering_is_found_and_kept_out_of_the_card():
    from manhwa_ocr.video.crops import _overlap_share, bubble_boxes

    img = _bubble_panel()
    maps = _panel_maps(img)
    assert maps["bubbles"], "the speech bubble should be detected"
    for target in (0.75, 1.0):
        box, _, _, bubble_ok = shape_crop(img, target, maps)
        assert bubble_ok
        assert max(_overlap_share(box, b) for b in maps["bubbles"]) <= 0.02


def test_clean_bubbles_returns_a_panel_with_bubbles_unchanged():
    # Bubble painting was removed entirely: the panel comes back
    # pixel-identical, bubbles and all. Detection still works -- the
    # crop/drop path (not painting) deals with bubbles now.
    import numpy as np

    from manhwa_ocr.video.crops import _panel_maps, bubble_boxes, clean_bubbles

    img = _bubble_panel()
    maps = _panel_maps(img)
    assert maps["bubbles"], "the speech bubble should still be detected"
    assert bubble_boxes(maps), "bubble_boxes still lists the bubble"
    cleaned = clean_bubbles(img)
    assert cleaned is img
    a, b = np.asarray(img.convert("RGB"), dtype=int), np.asarray(cleaned, dtype=int)
    assert (a == b).all()  # pixel-identical, lettering and all


def test_clean_bubbles_returns_a_panel_without_bubbles_unchanged():
    from manhwa_ocr.video.crops import clean_bubbles

    img = _art(600, 900)
    assert clean_bubbles(img) is img
