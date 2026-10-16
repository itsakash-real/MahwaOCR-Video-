"""Regression tests for the tool-fix session (Lumberjack bugs).

One section per fix, so a failure points straight at the behavior that regressed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.video import sync  # noqa: E402
from manhwa_ocr.video.validate import validate  # noqa: E402

# ====================================================================== FIX 1: sync


def _w(text: str, start: float, per: float = 0.35):
    out, t = [], start
    for w in text.split():
        out.append({"w": w, "start": round(t, 3), "end": round(t + per * 0.8, 3)})
        t += per
    return out


def _timeline(sentences, words, duration, **kw):
    """Run sync.align the way align_engine does, then validate() the result."""
    res = sync.align(sentences, words, duration, **kw)
    timed = res["sentences"]
    segs = []
    for i, s in enumerate(timed):
        end = float(timed[i + 1]["start"]) if i + 1 < len(timed) else duration
        segs.append({"sentence": i + 1, "start": s["start"], "end": end,
                     "selected_panel": f"P{i + 1:03d}-01", "confidence": "tag_confirmed_in_window"})
    order = {s["selected_panel"]: i for i, s in enumerate(segs)}
    return res, segs, validate(segs, order, duration)


def _assert_clean(res, segs, report, min_seg):
    assert report["status"] == "PASS", report["problems"]
    starts = [s["start"] for s in segs]
    assert starts == sorted(starts)
    for s in segs:
        assert s["end"] - s["start"] >= min_seg - 1e-3, s
    assert segs[-1]["end"] <= 40.0 + 1e-6


def test_tokens_spell_every_integer_and_split_hyphens():
    assert sync.tokens("Number 99.") == ["number", "ninety", "nine"]
    assert sync.tokens("ninety-nine") == ["ninety", "nine"]
    assert sync.tokens("100") == ["one", "hundred"]
    assert sync.tokens("16") == ["sixteen"]
    assert sync.tokens("1,000") == ["one", "thousand"]
    assert sync.tokens("2026") == ["two", "thousand", "twenty", "six"]
    assert sync.tokens("1999") == ["nineteen", "ninety", "nine"]
    assert sync.tokens("21st") == ["twenty", "first"]


def test_digit_in_script_matches_word_whisper_heard():
    sents = [{"text": "The count begins."}, {"text": "Number 99."}, {"text": "Then it ends."}]
    heard = _w("The count begins.", 1.0) + _w("Number ninety-nine.", 4.0) + _w("Then it ends.", 7.0)
    res, segs, report = _timeline(sents, heard, 10.0)
    assert res["matched"] == 1.0                      # every sentence anchored, none interpolated
    assert segs[1]["start"] == pytest.approx(4.0, abs=0.05)
    assert report["status"] == "PASS", report["problems"]


def test_digit_100_matches_one_hundred_spoken():
    sents = [{"text": "Level 100 reached."}]
    res = sync.align(sents, _w("Level one hundred reached.", 0.5), 6.0)
    assert res["matched"] == 1.0


def test_same_anchor_cluster_is_spread_not_collapsed():
    """Whisper gave a burst of words one identical timestamp, so several short
    sentences shared one anchor: this used to give 'end is not after start'."""
    sents = [{"text": "He walked in slowly and sat down."},
             {"text": "Number 99."}, {"text": "Rem."}, {"text": "Ram."}, {"text": "Emilia."},
             {"text": "Then everything went quiet for a long while after that."}]
    words = _w("He walked in slowly and sat down.", 1.0)
    burst = [{"w": w, "start": 5.0, "end": 5.0} for w in "Number ninety nine Rem Ram Emilia".split()]
    words += burst + _w("Then everything went quiet for a long while after that.", 9.0)
    res, segs, report = _timeline(sents, words, 20.0)
    assert res["spread"] >= 2
    starts = [s["start"] for s in segs]
    assert len(set(starts)) == len(starts)
    _assert_clean(res, segs, report, 0.25)
    # The window is shared in proportion to word counts: "Number 99." (3 words) gets more than "Rem." (1).
    assert (segs[2]["start"] - segs[1]["start"]) > (segs[3]["start"] - segs[2]["start"]) - 1e-6


def test_run_of_one_word_sentences_gets_minimum_duration():
    names = ["Rem", "Ram", "Emilia", "Beatrice", "Roswaal", "Subaru", "Felt", "Reinhard"]
    sents = [{"text": "They all gathered in the hall."}] + [{"text": f"{n}."} for n in names] + \
            [{"text": "Nobody spoke after that."}]
    words = _w("They all gathered in the hall.", 0.5) + _w(" ".join(names), 3.0, per=0.12) + \
        _w("Nobody spoke after that.", 6.0)
    res, segs, report = _timeline(sents, words, 12.0)
    assert report["status"] == "PASS", report["problems"]
    for s in segs:
        assert s["end"] - s["start"] >= 0.25 - 1e-3


def test_unmatched_tail_cluster_never_zero_length():
    """Unheard sentences interpolated near the end of the audio."""
    sents = [{"text": "A long first sentence that is heard."}] + [{"text": f"Word{i}."} for i in range(6)]
    words = _w("A long first sentence that is heard.", 0.5)
    res, segs, report = _timeline(sents, words, 6.0)
    assert report["status"] == "PASS", report["problems"]
    assert segs[-1]["end"] - segs[-1]["start"] >= 0.25 - 1e-3


def test_min_segment_is_configurable_and_capped_by_audio_length():
    sents = [{"text": "One."}, {"text": "Two."}, {"text": "Three."}]
    res = sync.align(sents, [], 0.6, min_segment=1.0)    # impossible: 3 x 1.0s in 0.6s
    starts = [s["start"] for s in res["sentences"]]
    assert starts == sorted(starts) and len(set(starts)) == 3
    assert starts[-1] < 0.6
    res = sync.align(sents, [], 30.0, min_segment=2.0)
    s = [x["start"] for x in res["sentences"]]
    assert all(b - a >= 2.0 - 1e-6 for a, b in zip(s, s[1:]))


def test_well_spaced_sentences_are_untouched():
    sents = [{"text": "The hero wakes up."}, {"text": "Then the ground explodes."}]
    res = sync.align(sents, _w("The hero wakes up. Then the ground explodes.", 0.5), 10.0)
    assert res["spread"] == 0
    assert res["sentences"][1]["start"] == pytest.approx(0.5 + 4 * 0.35, abs=0.01)


# ====================================================================== FIX 2: framing

import math  # noqa: E402

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from manhwa_ocr.video import panels as panels_mod  # noqa: E402
from manhwa_ocr.video.crops import _panel_maps, shape_candidates  # noqa: E402
from manhwa_ocr.video.frames import format_from, padded_region, shapes_for  # noqa: E402
from manhwa_ocr.video.verify import pick_card, pick_crop  # noqa: E402

SINGLE = shapes_for("single", format_from("16:9", 30))


def _art(w, h, base=(80, 100, 130), seed=7):
    rng = np.random.default_rng(seed)
    blocks = rng.normal(0, 22, (h // 8 + 1, w // 8 + 1, 1)).repeat(8, axis=0).repeat(8, axis=1)[:h, :w]
    arr = np.clip(np.array(base, dtype=np.float32) + blocks, 0, 255).astype("uint8")
    return Image.fromarray(arr, "RGB")


def _edge_margin_rows(img: Image.Image):
    return panels_mod.trim_edge_rows(img)


def _crop(img: Image.Image, box):
    w, h = img.size
    return img.crop((round(box[0] * w), round(max(0.0, box[1]) * h), round(box[2] * w), round(min(1.0, box[3]) * h)))


def _aspect(box, w, h):
    return ((box[2] - box[0]) * w) / ((box[3] - box[1]) * h)


def _card_aspect(card, w, h):
    """Aspect of the drawn card: the crop plus blurred padding above and below."""
    box = card["box"]
    return ((box[2] - box[0]) * w) / (((box[3] - box[1]) + 2 * card["pad_y"]) * h)


def _is_allowed(aspect):
    return min(abs(math.log(aspect / a)) for a in SINGLE) <= 0.02


def _page_with_gutters(path: Path, panel_heights=(900, 1500), width=900, white_strip=14):
    """A strip page: white gutters between panels, and a thin white strip (shorter than
    the gutter finder's minimum) clinging to each panel's top and bottom edge."""
    total = 120 + sum(panel_heights) + 120 * len(panel_heights)
    page = Image.new("RGB", (width, total), (255, 255, 255))
    y = 120
    for i, h in enumerate(panel_heights):
        art = _art(width, h, seed=3 + i)
        page.paste(art, (0, y + white_strip))
        y += h + white_strip * 2 + 120
        page = page if y <= total else page
    page = page.crop((0, 0, width, y))
    page.save(path)
    return page


def _detect_panels(tmp_path, **cfg):
    page_path = tmp_path / "page_01.png"
    _page_with_gutters(page_path)
    panels, norm_w = panels_mod.detect([page_path], cfg or None)
    files = {page_path.name: page_path}
    imgs = []
    for p in panels:
        strip = panels_mod._stitch(p, files)
        scale = strip.width / norm_w
        imgs.append(strip.crop((int(p.x0 * scale), 0, int(round(p.x1 * scale)), strip.height)))
    return panels, imgs


def test_trim_edge_rows_cuts_white_and_black_edges_only():
    img = _art(600, 800)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 600, 39), fill=(255, 255, 255))            # white gutter on top
    d.rectangle((0, 775, 600, 800), fill=(4, 4, 4))               # near-black gutter at the bottom
    d.rectangle((0, 400, 600, 440), fill=(255, 255, 255))         # a white band INSIDE the art stays
    assert panels_mod.trim_edge_rows(img) == (40, 25)
    assert panels_mod.trim_edge_rows(Image.new("RGB", (50, 50), (255, 255, 255))) == (0, 0)


def test_white_threshold_is_configurable():
    img = _art(300, 400)
    ImageDraw.Draw(img).rectangle((0, 0, 300, 29), fill=(238, 238, 238))   # off-white, below 242
    assert panels_mod.trim_edge_rows(img)[0] == 0
    assert panels_mod.trim_edge_rows(img, white=235)[0] == 30


def test_detected_panels_are_flush_to_the_artwork(tmp_path):
    panels, imgs = _detect_panels(tmp_path)
    assert len(panels) == 2
    for img in imgs:
        assert _edge_margin_rows(img) == (0, 0), "no white gutter rows may remain on a panel's edge"
        assert img.height in (900, 1500) or abs(img.height - 900) < 3 or abs(img.height - 1500) < 3


def test_tightening_can_be_switched_off(tmp_path):
    _, imgs = _detect_panels(tmp_path, tighten_edges=False)
    assert any(_edge_margin_rows(i) != (0, 0) for i in imgs)


def test_settings_added_by_this_fix_do_not_change_old_fingerprints(tmp_path):
    p = tmp_path / "a.png"
    Image.new("RGB", (10, 10)).save(p)
    base = panels_mod._settings(None)
    old_style = {k: v for k, v in base.items() if k not in ("tighten_edges", "white_threshold", "black_threshold")}
    import hashlib
    import json
    want = hashlib.sha256(json.dumps({"v": panels_mod.PANELS_VERSION, **old_style}, sort_keys=True).encode())
    st = p.stat()
    with p.open("rb") as fh:
        head = fh.read(65536)
    want.update(f"{p.name}:{st.st_size}:".encode() + hashlib.sha256(head).digest())
    assert panels_mod._fingerprint([p], base) == want.hexdigest()[:16]
    assert panels_mod._fingerprint([p], {**base, "tighten_edges": False}) != want.hexdigest()[:16]


@pytest.mark.parametrize("make", [lambda: _art(900, 2800), lambda: _art(900, 1200), lambda: _art(1000, 1000)])
def test_tall_and_square_panels_keep_full_width_with_a_vertical_window(make):
    img = make()
    shape, box, probs, _ = pick_crop(img, SINGLE, _panel_maps(img))
    assert box[0] == 0.0 and box[2] == 1.0, "full panel width"
    assert 0.0 <= box[1] < box[3] <= 1.0 + 1e-9
    assert shape in SINGLE and _is_allowed(_aspect(box, img.width, img.height))


def test_wide_panel_is_padded_not_side_cropped():
    img = _art(2400, 800)                          # 3:1, wider than the widest shape (16:9)
    card = pick_card(img, SINGLE, _panel_maps(img))
    box = card["box"]
    assert box == (0.0, 0.0, 1.0, 1.0), "the whole panel: no side crop, nothing cut"
    assert card["pad_y"] > 0, "the rest of the card is blurred padding above and below"
    assert card["shape"] == pytest.approx(1.777, abs=0.01)   # nearest allowed shape
    assert _is_allowed(_card_aspect(card, img.width, img.height))
    cand = shape_candidates(img, 1.777, _panel_maps(img), allow_pad=True)[0]
    assert cand["pad_y"] > 0 and not cand["narrow"]
    # Without allow_pad (an explicit single-shape request) the box stays an exact shape inside the panel.
    plain = shape_candidates(img, 1.777, _panel_maps(img))[0]
    assert plain["pad_y"] == 0 and _aspect(plain["box"], 2400, 800) == pytest.approx(1.777, rel=0.01)


def test_padded_region_has_the_boxes_aspect_and_keeps_the_panel_whole():
    img = _art(1800, 600, base=(200, 40, 40))
    region = padded_region(img, [0.0, 0.0, 1.0, 1.0], 0.2)
    assert region.width / region.height == pytest.approx(1800 / (1.4 * 600), rel=0.01)
    pad = round(0.2 * 600)
    assert np.asarray(region.crop((0, pad, 1800, pad + 600))).tolist() == np.asarray(img).tolist()


def test_crop_with_gutters_has_zero_white_rows(tmp_path):
    panels, imgs = _detect_panels(tmp_path)
    for img in imgs:
        for layout in ("single", "pair", "wide"):
            shapes = shapes_for(layout, format_from("16:9", 30))
            card = pick_card(img, shapes, _panel_maps(img))
            assert _edge_margin_rows(_crop(img, card["box"])) == (0, 0)
            assert card["box"][0] == 0.0 and card["box"][2] == 1.0
            assert _is_allowed(_card_aspect(card, img.width, img.height)) or layout != "single"


def _bubble_panel(w=900, h=1600):
    img = _art(w, h)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((int(0.52 * w), int(0.10 * h), int(0.92 * w), int(0.30 * h)), radius=60, fill=(255, 255, 255))
    for y in range(int(0.13 * h), int(0.28 * h), 30):
        d.rectangle((int(0.56 * w), y, int(0.88 * w), y + 14), fill=(20, 20, 20))
    return img


def test_bubble_is_avoided_by_the_vertical_choice_first():
    img = _bubble_panel()
    maps = _panel_maps(img)
    assert maps["bubbles"]
    shape, box, probs, clean = pick_crop(img, SINGLE, maps)
    assert clean and "lettering" not in probs
    assert box[0] == 0.0 and box[2] == 1.0, "bubble avoided without touching the sides"
    assert box[1] >= 0.29                          # the window starts below the bubble


def test_horizontal_shift_is_a_last_resort_and_never_below_min_width_kept():
    # A small bubble cut by the panel's right edge, mid-height: every full-width window
    # shows too much of it, but a window 90% as wide shows almost none.
    img = _art(900, 900)
    d = ImageDraw.Draw(img)
    d.rectangle((716, 358, 900, 488), fill=(20, 20, 20))
    d.rectangle((720, 362, 900, 484), fill=(255, 255, 255))
    for y in (374, 404, 434):
        d.rectangle((748, y, 880, y + 14), fill=(20, 20, 20))
    maps = _panel_maps(img)
    assert maps["bubbles"], "the bubble should be detected"
    full = pick_crop(img, [1.0, 1.333, 1.777], _panel_maps(img, {"min_width_kept": 1.0}))
    assert not full[3], "with no narrowing allowed, no full-width window is clean"
    shape, box, probs, clean = pick_crop(img, [1.0, 1.333, 1.777], maps)
    assert clean and "lettering" not in probs
    assert 0.9 - 1e-6 <= box[2] - box[0] < 1.0
    # Every candidate the planner considers, whatever the shape, respects the minimum width.
    for a in SINGLE:
        for c in shape_candidates(img, a, maps, fallback=False, allow_pad=True):
            assert c["box"][2] - c["box"][0] >= 0.9 - 1e-3
    strict = _panel_maps(img, {"min_width_kept": 1.0})
    for c in shape_candidates(img, 1.0, strict, fallback=False, allow_pad=True):
        assert c["box"][2] - c["box"][0] >= 1.0 - 1e-6


def test_full_width_can_be_switched_off_for_the_old_behavior():
    img = _art(2400, 800)
    maps = _panel_maps(img, {"full_width": False})
    box = shape_candidates(img, 1.0, maps)[0]["box"]
    assert box[2] - box[0] < 1.0 and box[1] >= 0.0
    assert _aspect(box, 2400, 800) == pytest.approx(1.0, rel=0.01)


def test_qa_shape_check_still_applies_to_padded_cards_and_is_not_loosened():
    from manhwa_ocr.video.build import quality_report
    from manhwa_ocr.video.plan import validate

    panels = {"panels": [{"id": "A", "width": 2400, "height": 800, "text_only": False}]}

    def plan_with(card):
        return {"format": "16:9", "fps": 30, "chapter": "1", "shots": [
            {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single", "background": "blur",
             "cards": [{"panel": "A", "enter": "cut", "motion": "push_in", **card}]}]}

    def bad(plan):
        return any("allowed shapes" in f for f in quality_report(plan, panels, None, 3.0)["failures"])

    assert bad(plan_with({"crop": [0, 0, 1, 1]}))                               # 3:1 unpadded: refused, as before
    assert bad(plan_with({"crop": [0, 0, 1, 1], "pad_y": 0.1}))                 # 2400 / 960 = 2.5: still not a shape
    exact = plan_with({"crop": [0, 0, 1, 1], "pad_y": 0.34375})                 # 2400 / (1.6875 * 800) = 16:9
    assert not bad(exact)
    exact["version"], exact["duration"] = 2, 4.0
    assert validate(exact, {"A"}) == []
    exact["shots"][0]["cards"][0]["pad_y"] = 9.0                               # absurd padding is refused
    assert validate(exact, {"A"}) != []
    exact["shots"][0]["cards"][0].update(pad_y=0.1, crop=[0, -0.2, 1, 1.2])     # crop stays inside the panel
    assert validate(exact, {"A"}) != []


# ====================================================================== FIX 3: pale heads

from manhwa_ocr.video.crops import face_top, figure_share, head_of  # noqa: E402
from manhwa_ocr.video.verify import verify_chapter  # noqa: E402


def _dark_bg(w, h, base=(25, 25, 45), seed=11):
    return _art(w, h, base=base, seed=seed)


def _white_haired(w=900, h=1600):
    """A white-haired, pale-skinned figure on a dark background: no skin colour anywhere."""
    img = _dark_bg(w, h)
    d = ImageDraw.Draw(img)
    d.ellipse((int(0.28 * w), int(0.20 * h), int(0.72 * w), int(0.47 * h)), fill=(246, 246, 250))   # hair
    d.ellipse((int(0.38 * w), int(0.28 * h), int(0.62 * w), int(0.45 * h)), fill=(236, 232, 236))   # face
    d.rectangle((int(0.25 * w), int(0.47 * h), int(0.75 * w), h), fill=(240, 240, 244))             # dress
    return img


def _gray_skinned(w=900, h=1600):
    img = _art(w, h, base=(30, 70, 120), seed=5)
    d = ImageDraw.Draw(img)
    d.ellipse((int(0.30 * w), int(0.25 * h), int(0.70 * w), int(0.33 * h)), fill=(15, 15, 25))      # dark hair
    d.ellipse((int(0.34 * w), int(0.30 * h), int(0.66 * w), int(0.50 * h)), fill=(140, 142, 146))   # gray face
    d.rectangle((int(0.25 * w), int(0.50 * h), int(0.75 * w), h), fill=(40, 40, 60))                # clothes
    return img


def _skin_figure_low(w=900, h=2800):
    """A character clearly present (skin colour) but nowhere a head the detector can find."""
    img = _art(w, h, base=(60, 60, 90), seed=9)
    ImageDraw.Draw(img).rectangle((int(0.3 * w), int(0.72 * h), int(0.7 * w), int(0.92 * h)), fill=(225, 175, 145))
    return img


def test_the_skin_colour_test_misses_these_figures_so_the_old_detector_found_nothing():
    for img in (_white_haired(), _gray_skinned()):
        assert face_top(_panel_maps(img)) is None
        assert figure_share(img) < 0.004


def test_white_haired_figure_head_is_found_and_kept_in_the_card():
    img = _white_haired()
    maps = _panel_maps(img)
    head = head_of(maps)
    assert head["source"] == "pale" and head["confident"] is False and head["figure"] is True
    assert 0.15 <= head["top"] <= 0.21            # the top of the hair (0.20)
    assert head["bottom"] <= 0.55                  # not the whole body
    for layout in ("single", "pair"):              # (a 16:9 'wide' card is too short for a whole head here)
        card = pick_card(img, shapes_for(layout, format_from("16:9", 30)), maps)
        assert card["box"][1] <= head["top"] - 0.04 + 1e-6, f"{layout}: the top of the head is cut"
        assert card["box"][3] >= head["bottom"] + 0.04 - 1e-6 or card["pad_y"] > 0


def test_gray_skinned_figure_face_is_kept_in_the_card():
    img = _gray_skinned()
    maps = _panel_maps(img)
    head = head_of(maps)
    assert head["source"] == "pale"
    card = pick_card(img, SINGLE, maps)
    assert card["box"][1] <= 0.30 and card["box"][3] >= 0.50, "the whole gray face stays in the card"


def test_pale_detection_can_be_switched_off():
    img = _white_haired()
    head = head_of(_panel_maps(img, {"pale_heads": False, "keep_top": 0}))
    assert head["source"] is None and head["keep_top"] is None


def test_white_bands_bubbles_and_plain_art_are_not_mistaken_for_heads():
    band = _art(900, 2000)
    ImageDraw.Draw(band).rectangle((0, 0, 900, 600), fill=(255, 255, 255))
    assert head_of(_panel_maps(band))["source"] is None
    assert head_of(_panel_maps(_bubble_panel()))["source"] is None
    assert head_of(_panel_maps(_art(900, 1600)))["source"] is None


def test_figure_without_a_located_head_never_loses_the_top_quarter():
    img = _skin_figure_low()
    maps = _panel_maps(img)
    head = head_of(maps)
    assert head["source"] is None and head["keep_top"] == pytest.approx(0.25)
    for layout in ("single", "pair"):
        card = pick_card(img, shapes_for(layout, format_from("16:9", 30)), maps)
        assert card["box"][1] < 0.25, f"{layout}: the top 25% of the panel was cropped away"
    assert head_of(_panel_maps(img, {"keep_top": 0}))["keep_top"] is None       # video.heads.keep_top: 0 turns it off
    assert head_of(_panel_maps(img, {"keep_top": 0.4}))["keep_top"] == pytest.approx(0.4)


def test_verify_lists_low_confidence_heads_and_never_drops_a_pale_character(tmp_path):
    panels = [{"id": "P001-01", "text_only": False}, {"id": "P001-02", "text_only": False},
              {"id": "P001-03", "text_only": False}]
    imgs = {"P001-01": _white_haired(), "P001-02": _skin_figure_low(), "P001-03": _art(900, 1600)}
    report = verify_chapter(panels, lambda p: imgs[p["id"]], SINGLE, tmp_path, "1", log=lambda *_: None)
    listed = {e["panel"]: e["reason"] for e in report["low_confidence_heads"]}
    assert set(listed) == {"P001-01", "P001-02"}
    assert "skin colour" in listed["P001-01"] and "no head was located" in listed["P001-02"]
    assert report["panels"]["P001-01"]["figure"] is True and report["panels"]["P001-01"]["action"] != "dropped"
    assert report["panels"]["P001-01"]["head"] == "pale" and report["panels"]["P001-01"]["head_confident"] is False
    import json
    saved = json.loads((tmp_path / "video" / "chapter_1.verify.json").read_text(encoding="utf-8"))
    assert [e["panel"] for e in saved["low_confidence_heads"]] == ["P001-01", "P001-02"]


# ====================================================================== FIX 4: recap finalize guards

import json  # noqa: E402

from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.script import RecapBuilder  # noqa: E402
from manhwa_ocr.util import UserError  # noqa: E402
from manhwa_ocr.video import cues as cues_mod  # noqa: E402


def _rows(spec):
    """panels.json rows: 'D' drawn, 'T' text-only, 'C' credits."""
    out = []
    for i, kind in enumerate(spec, start=1):
        out.append({"id": f"P001-{i:02d}", "index": i, "y0": 0.0, "y1": 100.0, "x0": 0.0, "x1": 100.0,
                    "width": 100.0, "height": 100.0, "tall": False, "scene": "", "text": "",
                    "segments": [{"page": 1, "file": "a.png", "y0": 0, "y1": 100, "page_w": 100}],
                    "text_only": kind in "TC", "credits": kind == "C"})
    return out


def _sentence(i, words=14):
    return " ".join(f"word{i}x{k}" for k in range(words)) + "."


def _tagged(tags, words=14):
    return " ".join(f"{{{t}}} {_sentence(i, words)}" if t else _sentence(i, words) for i, t in enumerate(tags, 1))


def test_tag_on_a_text_only_panel_is_an_error_naming_sentence_panel_and_nearest_drawn():
    panels = _rows("DTDC")
    narration = _tagged(["P001-01", "P001-02", "P001-03", "P001-03"])
    report = cues_mod.check(narration, panels)
    assert len(report["errors"]) == 1
    msg = report["errors"][0]
    assert "sentence 2" in msg and "P001-02" in msg and "text-only" in msg
    assert "{P001-03}" in msg, "the nearest drawn panel (the later one on a tie) is suggested"
    assert "word2x0" in msg                                   # the sentence is quoted


def test_tag_on_a_credits_panel_is_an_error():
    panels = _rows("DDDC")
    report = cues_mod.check(_tagged(["P001-01", "P001-02", "P001-03", "P001-04"]), panels)
    assert len(report["errors"]) == 1 and "credits" in report["errors"][0]
    assert "sentence 4" in report["errors"][0] and "{P001-03}" in report["errors"][0]


def test_a_clean_recap_has_no_errors_and_no_warnings():
    panels = _rows("DDDD")
    report = cues_mod.check(_tagged(["P001-01", "P001-02", "P001-03", "P001-04"], words=14), panels)
    assert report == {"errors": [], "warnings": []}


def test_warns_when_tagged_share_is_under_90_percent():
    panels = _rows("DDDD")
    tags = ["P001-01", "P001-02", "P001-03", "P001-04"] + [None] * 4
    report = cues_mod.check(_tagged(tags, words=14), panels)
    assert not report["errors"]
    assert any("50% of sentences name a panel" in w for w in report["warnings"])
    ok = cues_mod.check(_tagged(["P001-01", "P001-02", "P001-03", "P001-04"] * 3, words=14), panels)
    assert not any("name a panel" in w for w in ok["warnings"])


def test_warns_on_a_run_of_three_tiny_sentences_but_not_two():
    panels = _rows("DDDD")
    long = _sentence(1, 14)
    three = f"{{P001-01}} {long} {{P001-02}} Rem. {{P001-03}} Ram. {{P001-04}} Emilia. {{P001-04}} {long}"
    two = f"{{P001-01}} {long} {{P001-02}} Rem. {{P001-03}} Ram. {{P001-04}} {long} {{P001-04}} {long}"
    w3 = cues_mod.check(three, panels)["warnings"]
    assert any("sentences 2-4 are 3 in a row under 6 words" in w for w in w3)
    assert not any("in a row" in w for w in cues_mod.check(two, panels)["warnings"])
    tail = f"{{P001-01}} {long} {{P001-02}} {long} {{P001-03}} A. {{P001-04}} B. {{P001-04}} C."
    assert any("3 in a row" in w for w in cues_mod.check(tail, panels)["warnings"])      # a run at the very end


def test_warns_about_digits_and_says_what_will_happen():
    panels = _rows("DDDD")
    narration = _tagged(["P001-01", "P001-02", "P001-03", "P001-04"], words=14).replace("word1x0", "99")
    on = cues_mod.check(narration, panels, spell=True)["warnings"]
    off = cues_mod.check(narration, panels, spell=False)["warnings"]
    assert any("contains digits (99)" in w and "spelled out" in w for w in on)
    assert any("contains digits (99)" in w and "spell them out" in w for w in off)


def test_warns_when_there_are_too_few_words_for_the_drawn_panels():
    panels = _rows("DDDDDDDDDD") + _rows("T")[:0]
    short = _tagged([f"P001-{i:02d}" for i in range(1, 11)], words=6)       # 60 words for 10 panels = 6.0 per panel
    warn = [w for w in cues_mod.check(short, panels)["warnings"] if "per panel" in w]
    assert len(warn) == 1 and "6.0 per panel" in warn[0] and "7.5" in warn[0]
    enough = _tagged([f"P001-{i:02d}" for i in range(1, 11)], words=8)      # 80 words >= 75
    assert not [w for w in cues_mod.check(enough, panels)["warnings"] if "per panel" in w]
    # text-only and credits panels do not count as drawn panels
    mixed = _rows("DDDDDDDDDD") + _rows("TTTTTTTTCC")
    mixed = [{**p, "id": f"P002-{i:02d}"} if i >= 10 else p for i, p in enumerate(mixed)]
    assert not [w for w in cues_mod.check(enough, mixed)["warnings"] if "per panel" in w]


def test_min_hold_is_configurable_in_the_words_per_panel_rule():
    panels = _rows("DDDD")
    narration = _tagged(["P001-01", "P001-02", "P001-03", "P001-04"], words=5)         # 20 words, 4 panels
    assert any("per panel" in w for w in cues_mod.check(narration, panels, min_hold=3.0)["warnings"])
    assert not any("per panel" in w for w in cues_mod.check(narration, panels, min_hold=1.5)["warnings"])


# ---- finalize

def _series(tmp_path, spec="DTDD", labels=("1",), spell=None):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    for c in labels:
        (ws / f"chapter_{c}.md").write_text(f"# Ch {c}\n\nSomething happens in chapter {c}.\n", encoding="utf-8")
        media = ws / "media" / f"ch{c}"
        media.mkdir(parents=True, exist_ok=True)
        rows = [{**r, "id": r["id"].replace("P001", f"P{int(c):03d}")} for r in _rows(spec)]
        (media / "panels.json").write_text(json.dumps({"version": 4, "chapter": c, "fingerprint": "x",
                                                       "norm_width": 100, "pages": ["a.png"], "panels": rows}),
                                           encoding="utf-8")
    overrides = {"series__workdir": str(ws), "series__name": "T"}
    cfg = load_config(**overrides)
    if spell is not None:
        cfg.video["tts"] = {"spell_numbers": spell}
    logs = []
    return ws, RecapBuilder(cfg, log=logs.append), logs


def _parts(builder, narrations):
    (builder.task_dir / "parts.json").write_text(
        json.dumps({c: {"narration": n, "hook": "h"} for c, n in narrations.items()}), encoding="utf-8")


def test_finalize_rejects_a_tag_on_a_text_only_panel_and_keeps_the_task_open(tmp_path):
    ws, b, _ = _series(tmp_path, "DTDD", labels=("1", "2"))
    b.prepare(["1", "2"], 6)
    bad = _tagged(["P001-01", "P001-02", "P001-03", "P001-04"] * 4, words=14)
    good = _tagged(["P002-01", "P002-03", "P002-04"] * 4, words=14)
    _parts(b, {"1": bad, "2": good})
    with pytest.raises(UserError) as exc:
        b.finalize()
    text = str(exc.value)
    assert "Chapter 1 was NOT saved" in text and "P001-02" in text and "sentence 2" in text
    assert "{P001-03}" in text
    assert not (ws / "recaps" / "chapter_1.md").exists()                    # nothing saved for the bad chapter
    assert (ws / "recaps" / "chapter_2.md").exists()                        # the good one was
    assert b.pending_task() == ["1"]


def test_finalize_warns_but_still_saves(tmp_path):
    ws, b, logs = _series(tmp_path, "DDDD")
    b.prepare(["1"], 6)
    narration = " ".join([f"{{P001-01}} {_sentence(1, 14)}", "Rem.", "Ram.", "Emilia.", f"{{P001-02}} {_sentence(2, 14)}"] * 3)
    _parts(b, {"1": narration})
    assert [p.name for p in b.finalize()] == ["chapter_1.md"]
    joined = "\n".join(logs)
    assert "in a row under 6 words" in joined and "name a panel" in joined


def test_new_tts_spells_numbers_but_the_reading_copy_keeps_digits(tmp_path):
    ws, b, _ = _series(tmp_path, "DDDD")
    b.prepare(["1"], 6)
    s = "{P001-01} Number 99 stood among the first 100 survivors of the 16 who made it out of the old tower alive."
    _parts(b, {"1": " ".join([s, s.replace("{P001-01}", "{P001-02}")] * 3)})
    b.finalize()
    tts = (ws / "recaps" / "chapter_1.tts.txt").read_text(encoding="utf-8")
    assert "ninety nine" in tts and "one hundred" in tts and "sixteen" in tts
    assert not any(ch.isdigit() for ch in tts)
    assert "Number 99" in (ws / "recaps" / "chapter_1.md").read_text(encoding="utf-8")
    cues = json.loads((ws / "recaps" / "chapter_1.cues.json").read_text(encoding="utf-8"))
    assert "ninety nine" in cues["sentences"][0]["text"]          # cues.json text == the recorded text


def test_spell_numbers_can_be_switched_off(tmp_path):
    ws, b, _ = _series(tmp_path, "DDDD", spell=False)
    b.prepare(["1"], 6)
    s = "{P001-01} Number 99 stood among the first 100 survivors of the 16 who made it out of the old tower alive."
    _parts(b, {"1": " ".join([s, s.replace("{P001-01}", "{P001-02}")] * 3)})
    b.finalize()
    assert "99" in (ws / "recaps" / "chapter_1.tts.txt").read_text(encoding="utf-8")


def test_a_chapter_with_audio_never_has_its_recap_or_tts_rewritten(tmp_path):
    ws, b, logs = _series(tmp_path, "DDDD")
    b.prepare(["1"], 6)
    _parts(b, {"1": _tagged(["P001-01", "P001-02", "P001-03", "P001-04"] * 3, words=14)})
    b.finalize()
    recap, tts = ws / "recaps" / "chapter_1.md", ws / "recaps" / "chapter_1.tts.txt"
    before = (recap.read_bytes(), tts.read_bytes())
    (ws / "audio").mkdir()
    (ws / "audio" / "chapter_1.mp3").write_bytes(b"ID3 not really audio")
    b.prepare(["1"], 6, force=True)
    _parts(b, {"1": _tagged(["P001-04", "P001-03", "P001-02", "P001-01"] * 3, words=15)})
    assert b.finalize() == []
    assert (recap.read_bytes(), tts.read_bytes()) == before
    assert any("already has audio" in m and "NOT rewritten" in m for m in logs)
    assert b.pending_task() is None                     # handled, not left owing a rewrite


def test_without_audio_a_forced_rewrite_still_works(tmp_path):
    ws, b, _ = _series(tmp_path, "DDDD")
    b.prepare(["1"], 6)
    _parts(b, {"1": _tagged(["P001-01", "P001-02", "P001-03", "P001-04"] * 3, words=14)})
    b.finalize()
    b.prepare(["1"], 6, force=True)
    _parts(b, {"1": _tagged(["P001-04", "P001-03", "P001-02", "P001-01"] * 3, words=15)})
    assert len(b.finalize()) == 1


# ====================================================================== FIX 5: grouped tags

from manhwa_ocr.video import pairing  # noqa: E402
from manhwa_ocr.video.align_engine import align_sentences  # noqa: E402
from manhwa_ocr.video.validate import redistribute, validate as validate_timeline  # noqa: E402


def test_redistribute_spreads_extra_tags_over_the_free_sentences_that_follow():
    tags = [["A", "B", "C", "D"], [], [], [], ["E"]]
    assert redistribute(tags, 1) == {1: ["A"], 2: ["B"], 3: ["C"], 4: ["D"]}
    # not enough free sentences: the rest stay together on the last one used
    assert redistribute([["A", "B", "C", "D"], [], ["X"]], 1) == {1: ["A"], 2: ["B", "C", "D"]}
    # nothing free right after it, or only one tag: nothing to move
    assert redistribute([["A", "B"], ["X"]], 1) == {}
    assert redistribute([["A"], []], 1) == {}
    # a sentence that holds the previous picture can't carry a tag
    assert redistribute([["A", "B"], [], []], 1, holds=[False, True, False]) == {1: ["A"], 3: ["B"]}


def _jump_segments():
    panels = [{"id": f"P001-{i:02d}", "text": "", "text_only": False} for i in range(1, 21)]
    order = {p["id"]: i for i, p in enumerate(panels)}
    tags = [["P001-01"], ["P001-02"], ["P001-03"], ["P001-05", "P001-06", "P001-08", "P001-14"],
            [], [], [], ["P001-15"]]
    sentences = [{"text": f"Sentence number {i} walks on through the cold hallway slowly.", "panels": t}
                 for i, t in enumerate(tags, 1)]
    segs = align_sentences(sentences, panels)
    t = 0.0
    for s in segs:
        s["start"], s["end"] = t, t + 3.0
        t += 3.0
    return segs, order, t


def test_align_error_for_a_jump_says_which_tag_moves_to_which_sentence():
    segs, order, duration = _jump_segments()
    report = validate_timeline(segs, order, duration)
    assert report["checks"]["no_unexplained_jumps"] == "FAIL"
    problem = next(p for p in report["problems"] if p["check"] == "no_unexplained_jumps")
    d = problem["detail"]
    assert problem["sentence"] == 5
    assert "sentence 4 carries 4 tags (P001-05, P001-06, P001-08, P001-14)" in d
    assert '"4": ["P001-05"], "5": ["P001-06"], "6": ["P001-08"], "7": ["P001-14"]' in d
    assert "Fix in pairs.json" in d


def test_a_jump_with_no_grouped_cause_still_names_the_panels_skipped():
    panels = {f"P001-{i:02d}": i for i in range(1, 30)}
    segs = [{"sentence": 1, "start": 0, "end": 3, "selected_panel": "P001-01", "confidence": "tag_confirmed_in_window",
             "tagged_panels": ["P001-01"], "candidate_window": ["P001-01"]},
            {"sentence": 2, "start": 3, "end": 6, "selected_panel": "P001-20", "confidence": "no_evidence_default_sequence",
             "tagged_panels": [], "candidate_window": ["P001-20"]}]
    d = next(p["detail"] for p in validate_timeline(segs, panels, 6.0)["problems"] if p["check"] == "no_unexplained_jumps")
    assert "P001-01" in d and "P001-20" in d and "tag this sentence with P001-02" in d


def _pair_workspace(tmp_path, tags):
    root = tmp_path / "ws"
    pages = root / "media" / "ch1" / "pages"
    pages.mkdir(parents=True)
    Image.new("RGB", (100, 2000), (90, 110, 140)).save(pages / "page_01.png")
    rows = []
    for i in range(20):
        rows.append({"id": f"P001-{i + 1:02d}", "index": i + 1, "y0": i * 100.0, "y1": i * 100.0 + 100, "x0": 0.0,
                     "x1": 100.0, "width": 100.0, "height": 100.0, "tall": False, "scene": "", "text": "",
                     "segments": [{"page": 1, "file": "page_01.png", "y0": i * 100, "y1": i * 100 + 100, "page_w": 100}],
                     "text_only": False, "credits": False})
    (root / "media" / "ch1" / "panels.json").write_text(json.dumps(
        {"version": 4, "chapter": "1", "fingerprint": "x", "norm_width": 100, "pages": ["page_01.png"], "panels": rows}),
        encoding="utf-8")
    narration = " ".join((("{" + ",".join(t) + "} ") if t else "") + f"Sentence number {i} walks on through the cold hallway slowly."
                         for i, t in enumerate(tags, 1))
    cues_mod.write_outputs(root, "1", narration, {r["id"] for r in rows})
    return root


GROUPED = [["P001-01"], ["P001-02"], ["P001-03"], ["P001-05", "P001-06", "P001-08", "P001-14"], [], [], [], ["P001-15"]]


def test_pair_detects_grouped_tags_and_offers_a_suggestion_without_applying_it(tmp_path):
    root = _pair_workspace(tmp_path, GROUPED)
    found = pairing.grouped_tags(root, "1")
    assert [g["sentence"] for g in found] == [4]
    assert found[0]["move"] == {4: ["P001-05"], 5: ["P001-06"], 6: ["P001-08"], 7: ["P001-14"]}
    task = pairing.write_task(root, "1", None, log=lambda *_: None)
    md = task.read_text(encoding="utf-8")
    assert "## Grouped tags" in md and "sentence 4 carries 4 tags" in md and "no_unexplained_jumps" in md
    pairs = json.loads(pairing.pairs_path(root, "1").read_text(encoding="utf-8"))
    assert pairs["4"] == ["P001-05", "P001-06", "P001-08", "P001-14"] and pairs["5"] == []   # NOT applied
    assert pairs["_suggestions"]["4"]["move"]["5"] == ["P001-06"]
    assert any("sentence 4 carries 4 tags" in p for p in pairing.check(root, "1"))
    # apply ignores the suggestion notes and changes nothing
    assert pairing.apply(root, "1", log=lambda *_: None) == 0
    # copying the suggestion into the numbered entries applies it
    pairs.update({k: v for k, v in ((k, v) for k, v in pairs["_suggestions"]["4"]["move"].items())})
    pairing.pairs_path(root, "1").write_text(json.dumps(pairs), encoding="utf-8")
    assert pairing.apply(root, "1", log=lambda *_: None) == 4
    new = cues_mod.load_cues(root, "1")["sentences"]
    assert [s["panels"] for s in new[3:7]] == [["P001-05"], ["P001-06"], ["P001-08"], ["P001-14"]]
    assert pairing.grouped_tags(root, "1") == []


def test_pair_stays_quiet_when_tags_are_spread_or_the_next_sentence_is_tagged(tmp_path):
    spread = [["P001-01"], ["P001-02", "P001-03"], ["P001-04"], ["P001-05"]]
    assert pairing.grouped_tags(_pair_workspace(tmp_path / "a", spread), "1") == []
    tagged_next = [["P001-01", "P001-02", "P001-03", "P001-04"], ["P001-05"], ["P001-06"]]
    assert pairing.grouped_tags(_pair_workspace(tmp_path / "b", tagged_next), "1") == []
    plain = [["P001-01"], [], ["P001-03"]]
    assert pairing.grouped_tags(_pair_workspace(tmp_path / "c", plain), "1") == []
