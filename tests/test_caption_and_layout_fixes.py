"""Caption timing + caption bar + diversity + P2 wide switch + system-window fixes.

Covers the Ch10 new-code test-build issues:
  1. captions chunked per-shot left 1-5s gaps during narration and showed
     a sentence's tail 3-5s before its words (sentence 41);
  2. captions sat in a bottom band ON the card, covering panel text (241s);
  3. the same panel showed 3x consecutively with an identical crop
     (sentences 55-57 P014-10, 75-77 P020-01);
  4. the P2 auto-switch recorded 1.777 -> 1.0 for a WIDE layout (P014-07);
  5. a system-notification panel rendered as a small square (~34s, P004-07).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.video import plan as plan_mod  # noqa: E402
from manhwa_ocr.video import verify as verify_mod  # noqa: E402
from manhwa_ocr.video.captions import (  # noqa: E402
    CAPTION_BAR_TOP_FRACTION, build_captions, build_captions_from_sentences,
    caption_bar_geometry, caption_text_bbox, validate_caption_timing, write_ass)
from manhwa_ocr.video.crops import is_system_window  # noqa: E402
from manhwa_ocr.video.frames import (  # noqa: E402
    CAPTION_BAR_TOP_FRACTION as FRAMES_BAR_TOP, Card, Format, ShotRenderer,
    card_box, slot_centres)
from manhwa_ocr.video.plan import (  # noqa: E402
    build_plan, ensure_panel_diversity, split_system_window_units)

VCFG = load_config().video

# Ch10 test-build windows: sentence 40 (119.17-121.6), 41 (121.6-128.71),
# 42 (128.71-130.62), 4 (12.36-18.94). Sentence 41's tail ("slashes back
# in a streak of cyan light") was captioned at 122.68-123.38, ~5s early,
# and the filler shot after it left a 3.56s caption gap.
SEG41 = ("The armored arm is cut off, blood spraying from the stump, and Minwoo "
         "slashes back in a streak of cyan light.")
SEGMENTS = [
    {"text": "In exchange, Minwoo will take his head.", "start": 119.17, "end": 121.6},
    {"text": SEG41, "start": 121.6, "end": 128.71},
    {"text": "Mutakta stops and asks why.", "start": 128.71, "end": 130.62},
    {"text": "If Minwoo is going to prevent that regret, he has to save Cedric first, "
             "but his own body will barely move.", "start": 12.36, "end": 18.94},
]
# The old per-shot captions for the same stretch: S032 compressed sentences
# 40+41 into 119.17-123.377, S033 (filler) had no sentence, S034 started
# sentence 42's captions at its shot start (126.933), before the words.
OLD_SHOTS = [
    {"id": "S032", "start": 119.17, "end": 123.377,
     "sentence": "In exchange, Minwoo will take his head. " + SEG41},
    {"id": "S033", "start": 123.377, "end": 126.933, "sentence": ""},
    {"id": "S034", "start": 126.933, "end": 129.665, "sentence": "Mutakta stops and asks why."},
]


def _tail_chunk(caps):
    return next(c for c in caps if "streak of cyan light" in c["text"])


# ------------------------------------------------------------------ 1. caption timing


def test_sentence_captions_have_no_gap_and_tail_is_on_time():
    # SEGMENTS[:3] is the contiguous 119-130s stretch (SEGMENTS[3] is the
    # separate sentence-4 window, validated on its own below).
    caps = build_captions_from_sentences(SEGMENTS[:3])
    assert validate_caption_timing(caps, SEGMENTS[:3]) == []
    tail = _tail_chunk(caps)
    assert tail["start"] >= 126.0  # the tail words are spoken ~127s, not at 122.68
    assert tail["end"] <= 128.71 + 0.5
    for c in caps:
        assert c["sentence"] in (1, 2, 3, 4)


def test_sentence_caption_chunks_stay_inside_their_sentence_window():
    caps = build_captions_from_sentences(SEGMENTS[:3])
    for c in caps:
        seg = SEGMENTS[c["sentence"] - 1]
        assert c["start"] >= seg["start"] - 0.5
        assert c["end"] <= seg["end"] + 0.5


def test_old_per_shot_captions_fail_timing_validation():
    caps = build_captions(OLD_SHOTS)
    tail = _tail_chunk(caps)
    assert tail["start"] == pytest.approx(122.676, abs=0.01)  # the reported early caption
    errs = validate_caption_timing(caps, SEGMENTS[:3])
    assert any("BEFORE" in e for e in errs)  # sentence 42 captioned before its words
    assert any("no caption" in e for e in errs)  # 3.56s gap during narration


def test_sentence_captions_fill_a_filler_shot_window():
    # Sentence 4 spans a filler shot (12.36-18.94); captions must cover it.
    caps = build_captions_from_sentences([SEGMENTS[3]])
    assert caps[0]["start"] == pytest.approx(12.36, abs=0.01)
    assert caps[-1]["end"] == pytest.approx(18.94, abs=0.01)
    assert validate_caption_timing(caps, [SEGMENTS[3]]) == []


# ------------------------------------------------------------------ 2. caption bar below the card


def test_caption_bar_constants_agree():
    assert FRAMES_BAR_TOP == CAPTION_BAR_TOP_FRACTION == pytest.approx(0.92)


def test_caption_text_bbox_sits_inside_the_bar_below_the_cards():
    fmt = Format(1920, 1080, 30)
    bar = caption_bar_geometry(fmt)
    assert bar["top"] == pytest.approx(0.92 * 1080)
    x0, y0, x1, y1 = caption_text_bbox(fmt)
    assert y0 >= bar["top"]  # caption text starts at or below the bar's top
    assert y1 <= 1080
    # Every layout's card ends at or above the bar's top.
    for layout in ("single", "pair", "wide", "inset"):
        bw, bh = card_box(layout, fmt)
        assert bh <= 0.92 * 1080 + 1
        _start, rest = slot_centres(layout, fmt, (int(bw), int(bh)), 0)
        assert rest[1] + bh / 2 <= 0.92 * 1080 + 1  # card bottom above the bar


def test_caption_bar_is_black_below_the_cards_in_render():
    fmt = Format(320, 180, 30)
    panel = Image.new("RGB", (300, 400), (200, 50, 50))
    shot = {"start": 0, "end": 4, "layout": "single", "background": "black",
            "cards": [{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "push_in"}],
            "exit": "cut", "effects": [], "caption_bar": True}
    r = ShotRenderer(shot, {"A": panel}, fmt)
    frame = r.frame_array(1.0)
    bar_top = int(180 * 0.92)
    assert (frame[bar_top + 2:, :] == 0).all()  # the bar is solid black
    assert (frame[:bar_top, :] != 0).any()  # the card area is not blacked out


def test_write_ass_marginv_puts_text_inside_the_bar(tmp_path):
    caps = [{"text": "The streamer walks into", "key": "streamer", "start": 0.0, "end": 2.0, "shot": "sent1"}]
    ass = write_ass(caps, tmp_path / "captions.ass", fmt=Format(1920, 1080, 30))
    style = next(ln for ln in ass.read_text(encoding="utf-8").splitlines() if ln.startswith("Style:"))
    margin_v = int(style.split(",")[-2])  # MarginV is the second-to-last style field
    assert margin_v + 46 * 1.25 <= 0.08 * 1080 + 1  # text fits inside the 8% bar


# ------------------------------------------------------------------ 3. diversity


def _div_shot(sid, start, panel, crop):
    return {"id": sid, "start": start, "end": start + 2.0, "layout": "single",
            "cards": [{"panel": panel, "crop": list(crop), "motion": "push_in"}]}


def test_triple_repeat_gets_a_different_crop():
    shots = [_div_shot("S001", 0.0, "P014-10", [0, 0.092, 1, 0.798]),
             _div_shot("S002", 2.0, "P014-10", [0, 0.092, 1, 0.798]),
             _div_shot("S003", 4.0, "P014-10", [0, 0.092, 1, 0.798])]
    out, changes = ensure_panel_diversity(shots)
    assert len(changes) == 1 and changes[0]["shot"] == "S003"
    assert out[2]["cards"][0]["crop"] != [0, 0.092, 1, 0.798]  # punched in
    # The new crop still differs from the (already punched) middle shot too.
    out2, changes2 = ensure_panel_diversity([
        _div_shot("S001", 0.0, "P014-10", [0, 0.092, 1, 0.798]),
        _div_shot("S002", 2.0, "P014-10", [0.08, 0.148, 0.92, 0.742]),
        _div_shot("S003", 4.0, "P014-10", [0, 0.092, 1, 0.798])])
    assert len(changes2) == 1
    assert out2[2]["cards"][0]["crop"] not in ([0, 0.092, 1, 0.798], [0.08, 0.148, 0.92, 0.742])


def test_diversity_leaves_already_different_crops_alone():
    shots = [_div_shot("S001", 0.0, "P014-10", [0, 0.092, 1, 0.798]),
             _div_shot("S002", 2.0, "P014-10", [0.08, 0.148, 0.92, 0.742]),
             _div_shot("S003", 4.0, "P014-10", [0.16, 0.2, 0.84, 0.69])]
    out, changes = ensure_panel_diversity(shots)
    assert changes == []
    assert out[2]["cards"][0]["crop"] == [0.16, 0.2, 0.84, 0.69]


def test_diversity_ignores_panel_changes():
    shots = [_div_shot("S001", 0.0, "P014-10", [0, 0, 1, 1]),
             _div_shot("S002", 2.0, "P014-10", [0, 0, 1, 1]),
             _div_shot("S003", 4.0, "P014-09", [0, 0, 1, 1])]
    _, changes = ensure_panel_diversity(shots)
    assert changes == []


def test_build_plan_records_diversity_changes():
    plan = build_plan(chapter="1", panels_data={"panels": [
        {"id": "P001-01", "width": 300, "height": 400, "text": ""}]},
        duration=30.0, audio=None, vcfg=VCFG,
        image_for=lambda p: Image.new("RGB", (300, 400), (120, 90, 70)))
    assert "diversity_changes" in plan  # the pass runs on every build


# ------------------------------------------------------------------ 4. P2 switch never to 1.0 in a wide layout


def _fake_pick_wide_vs_pair(monkeypatch):
    def fake_pick(img, shapes, m=None):
        if any(abs(float(s) - 1.777) < 0.01 for s in shapes):
            return {"shape": 1.777, "box": (0, 0, 1, 1), "pad_y": 0.0, "problems": [], "clean": True}
        return {"shape": 1.0, "box": (0, 0, 1, 1), "pad_y": 0.0, "problems": [], "clean": True}
    monkeypatch.setattr(verify_mod, "pick_card", fake_pick)


def test_p2_switch_never_to_square_in_wide_layout(monkeypatch):
    """P014-07-style case: wide 1.777 clean, pair 1.0 clean -> NO switch for wide."""
    _fake_pick_wide_vs_pair(monkeypatch)
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    switch = verify_mod.find_pair_safe_shape(
        img, {}, {"wide": [1.333, 1.777], "pair": [0.5625, 0.75, 1.0]}, layout="wide")
    assert switch is None  # 1.0 is not a valid wide shape


def test_p2_switch_still_applies_for_single_layout(monkeypatch):
    _fake_pick_wide_vs_pair(monkeypatch)
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    # Single allows 1.0, so the pair-safe switch still applies there.
    switch = verify_mod.find_pair_safe_shape(
        img, {}, {"single": [0.5625, 0.75, 1.0, 1.333, 1.777], "pair": [0.5625, 0.75, 1.0]},
        layout="single")
    assert switch is not None and switch["to_shape"] == pytest.approx(1.0)


def test_verify_labels_its_switch_single_layout(monkeypatch):
    """verify.json switches are SINGLE-layout switches: labelled, so a
    1.777 -> 1.0 entry is never misread as a wide-layout switch."""
    from manhwa_ocr.video import verify as _v

    def fake_pick(img, shapes, m=None):
        if any(abs(float(s) - 1.333) < 0.01 for s in shapes):
            return {"shape": 1.333, "box": (0, 0, 1, 1), "pad_y": 0.0, "problems": [], "clean": True}
        return {"shape": 0.75, "box": (0, 0.1, 1, 0.9), "pad_y": 0.0, "problems": [], "clean": True}

    monkeypatch.setattr(_v, "_panel_maps", lambda img, options=None: {
        "_head": {"top": 0.1, "bottom": 0.4, "source": "skin", "confident": True,
                  "figure": True, "keep_top": None},
        "painted": [], "edge_rows": (0.0, 0.0)})
    monkeypatch.setattr(_v, "is_mask_risk", lambda maps: False)
    monkeypatch.setattr(_v, "pick_card", fake_pick)
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    report = _v.verify_chapter([{"id": "P001-01", "width": 300, "height": 400}], lambda p: img,
                               [0.75, 1.333],
                               layout_shapes={"single": [0.5625, 0.75, 1.0, 1.333, 1.777],
                                              "pair": [0.5625, 0.75, 1.0]})
    assert report["layout_switches"]["P001-01"]["layout"] == "single"


def test_planner_drops_a_cached_invalid_wide_switch(monkeypatch):
    """A crop-cache entry recorded before the fix (wide -> 1.0) is not reused."""
    monkeypatch.setattr(plan_mod, "find_pair_safe_shape", lambda *a, **k: None)
    fmt = Format(1920, 1080, 30)
    panels = [{"id": "P014-07", "width": 772, "height": 306, "text": ""}]
    img = Image.new("RGB", (772, 306), (120, 90, 70))
    d = plan_mod._Designer("1", panels, fmt, {"format": "16:9", "fps": 30}, lambda p: img)
    stale = {"layout_switch": {"from_shape": 1.777, "to_shape": 1.0, "box": [0, 0, 1, 1], "pad_y": 0.0}}
    assert d._pair_safe_switch("P014-07", "wide", stale, None) is None


# ------------------------------------------------------------------ 5. system-window panels


def test_is_system_window_detects_the_banner():
    banner = {"id": "P004-07", "width": 771.8, "height": 306.0,
              "text": "INEXPLICABLE MENTAL FORTITUDE! YOUR SOUL IS BEGINNING TO TRANSCEND ITS RANK!",
              "scene": "Narration box, then two blue system notification banners announcing "
                       "'INEXPLICABLE MENTAL FORTITUDE!'"}
    assert is_system_window(banner) is True


def test_is_system_window_ignores_tall_panels_containing_a_window():
    tall = {"id": "P020-03", "width": 900.0, "height": 2397.0,
            "text": "YOU HAVE ACQUIRED THE TITLE 'THE ONE WHO STOPPED ETERNITY'!",
            "scene": "A system window announces the title over a black void."}
    assert is_system_window(tall) is False  # tall art, not a banner


def test_is_system_window_ignores_wide_non_system_panels():
    wide = {"id": "P001-01", "width": 900.0, "height": 300.0,
            "text": "The dragon flies over the valley.",
            "scene": "A wide shot of a dragon over a valley."}
    assert is_system_window(wide) is False


def test_is_system_window_honours_an_explicit_tag():
    assert is_system_window({"id": "P001-01", "width": 300, "height": 400,
                             "system_window": True}) is True


def test_system_window_card_fills_frame_width():
    fmt = Format(1920, 1080, 30)
    panel = Image.new("RGB", (1777, 1000), (40, 80, 160))
    card = Card(panel, {"crop": [0, 0, 1, 1], "wide_fill": True, "at": 0,
                        "enter": "cut", "motion": "push_in"},
                "wide", 0, 0.0, 4.0, fmt, None)
    assert card.cw >= 0.90 * 1920  # >=90% of the frame width, not a small square
    assert card.cw / card.ch >= 1.33
    assert card.ch <= 0.92 * 1080 + 1  # still above the caption bar


def test_system_window_unit_splits_out_of_a_pair():
    banner = {"id": "SYS", "width": 772, "height": 306,
              "text": "SOUL VALUE [3/50]",
              "scene": "A system notification banner."}
    other = {"id": "N1", "width": 300, "height": 400, "text": ""}
    by_id = {"SYS": banner, "N1": other}
    unit = {"start": 0.0, "end": 6.0, "panels": ["SYS", "N1"], "cues": [],
            "emphasis": [], "sentence": "Both.", "sentence_starts": [0.0, 3.0], "second_at": 3.0}
    out = split_system_window_units([unit], by_id)
    assert len(out) == 2
    assert [u["panels"] for u in out] == [["SYS"], ["N1"]]
    assert out[0]["end"] == pytest.approx(3.0) and out[1]["start"] == pytest.approx(3.0)
    # A pair with no system panel passes through untouched.
    plain = {"start": 0.0, "end": 6.0, "panels": ["N1", "N1"], "cues": [],
             "emphasis": [], "sentence": "Both.", "sentence_starts": [0.0, 3.0], "second_at": 3.0}
    assert split_system_window_units([plain], by_id) == [plain]


def test_build_plan_gives_system_panel_a_wide_fill_shot():
    banner = {"id": "SYS", "width": 772, "height": 306,
              "text": "INEXPLICABLE MENTAL FORTITUDE!",
              "scene": "Two blue system notification banners."}
    other = {"id": "N1", "width": 300, "height": 400, "text": ""}
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    plan = build_plan(
        chapter="1", panels_data={"panels": [banner, other], "fingerprint": "x"},
        duration=6.0, audio=None, vcfg=VCFG, image_for=lambda p: img,
        alignment_units=[{"start": 0.0, "end": 6.0, "panels": ["SYS", "N1"], "cues": [],
                          "emphasis": [], "sentence": "Both.", "sentence_starts": [0.0, 3.0],
                          "second_at": 3.0}])
    sys_shots = [s for s in plan["shots"] if s["cards"][0]["panel"] == "SYS"]
    assert len(sys_shots) == 1
    assert sys_shots[0]["layout"] == "wide"
    assert sys_shots[0]["cards"][0].get("wide_fill") is True
    assert all("SYS" not in [c["panel"] for c in s["cards"][1:]] for s in plan["shots"])
