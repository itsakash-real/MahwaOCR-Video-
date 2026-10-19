"""P1 crop/face: grey-oval mask rule, layout-sensitive crop QA, patch
visibility, head safety.

The mask rule is: a DOMINANT bubble overlapping the head range must never
be painted over (that leaves a flat grey oval on the face). Bubble
painting was then removed ENTIRELY -- painting harmed more than it
helped, so no bubble is ever painted now. Crops steer
around it; when no clean crop exists the panel is DROPPED and its time is
re-paired, instead of masking the face in render (Ch2 P018-04 / P020-04
were fixed exactly this way by hand).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.video import verify as verify_mod  # noqa: E402
from manhwa_ocr.video.crops import (  # noqa: E402
    clean_bubbles, is_mask_risk, mask_risk_bubbles, patch_visibility)


# ------------------------------------------------------------------ mask-risk rule


def _maps(head, bubbles):
    return {"_head": {"top": head[0], "bottom": head[1], "source": "skin",
                      "confident": True, "figure": True, "keep_top": None},
            "bubble_regions": [{"box": b, "fill": 0.9} for b in bubbles]}


def test_mask_risk_needs_dominant_bubble_overlapping_head():
    # Dominant (wide) bubble overlapping the head range -> risk.
    maps = _maps((0.10, 0.40), [(0.05, 0.15, 0.60, 0.45)])
    assert is_mask_risk(maps) is True
    assert mask_risk_bubbles(maps) == [(0.05, 0.15, 0.60, 0.45)]
    # Same bubble, head elsewhere -> no risk.
    assert is_mask_risk(_maps((0.70, 0.90), [(0.05, 0.15, 0.60, 0.45)])) is False
    # Small bubble overlapping the head -> not dominant, no risk.
    assert is_mask_risk(_maps((0.10, 0.40), [(0.30, 0.15, 0.45, 0.30)])) is False
    # No head at all -> no risk (nothing to mask).
    maps_no_head = {"_head": {"top": None, "bottom": None, "source": None,
                              "confident": False, "figure": False, "keep_top": None},
                    "bubble_regions": [{"box": (0.05, 0.15, 0.60, 0.45), "fill": 0.9}]}
    assert is_mask_risk(maps_no_head) is False


def _two_tone_panel():
    """200x200: left half dark red, right half pale blue, white bubble box."""
    img = Image.new("RGB", (200, 200), (200, 200, 240))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 99, 199), fill=(120, 40, 40))
    return img


def test_clean_bubbles_never_paints_a_mask_risk_bubble():
    img = _two_tone_panel()
    d = ImageDraw.Draw(img)
    d.rectangle((40, 20, 160, 90), fill=(255, 255, 255))  # the bubble
    maps = _maps((0.10, 0.40), [(0.20, 0.10, 0.80, 0.45)])
    assert is_mask_risk(maps) is True
    out = clean_bubbles(img, maps)
    assert list(getattr(out, "painted_boxes", [])) == []
    assert out.getpixel((100, 55)) == (255, 255, 255)  # bubble left white, face unmasked


def test_clean_bubbles_returns_panel_unchanged_even_with_bubbles():
    # Bubble painting was removed entirely: clean_bubbles() is a no-op.
    # Even a non-risk bubble is left exactly as drawn -- pixel-identical,
    # no painted_boxes -- and the crop/drop path deals with it instead.
    import numpy as np

    img = _two_tone_panel()
    d = ImageDraw.Draw(img)
    d.rectangle((40, 20, 160, 90), fill=(255, 255, 255))  # the bubble
    maps = _maps((0.70, 0.90), [(0.20, 0.10, 0.80, 0.45)])
    assert is_mask_risk(maps) is False
    out = clean_bubbles(img, maps)
    assert out is img
    assert list(getattr(out, "painted_boxes", [])) == []
    assert (np.asarray(out) == np.asarray(img)).all()  # pixel-identical
    assert out.getpixel((100, 55)) == (255, 255, 255)  # bubble left as drawn


# ------------------------------------------------------------------ patch visibility


def test_patch_visibility_flags_a_flat_patch_on_textured_art():
    flat = Image.new("RGB", (100, 100), (128, 128, 128))
    assert patch_visibility(flat, [(0.2, 0.2, 0.8, 0.8)]) == 0.0
    assert patch_visibility(flat, []) == 0.0
    # Striped (textured) art with a flat rectangle in the middle: the flat
    # patch stands out against the textured ring around it.
    striped = Image.new("RGB", (100, 100), (40, 40, 40))
    d = ImageDraw.Draw(striped)
    for x in range(0, 100, 4):
        d.line((x, 0, x, 99), fill=(220, 220, 220))
    d.rectangle((30, 30, 70, 70), fill=(128, 128, 128))
    assert patch_visibility(striped, [(0.3, 0.3, 0.7, 0.7)]) > 0.12
    # Same striped art, box over a region with no flat patch: texture inside
    # matches texture outside, so visibility stays low.
    assert patch_visibility(striped, [(0.05, 0.05, 0.20, 0.20)]) < 0.12


# ------------------------------------------------------------------ pick_card auto-switch


def _cand(box, score, *, bubble_ok=True, texty=0.0, head_ok=True):
    return {"box": box, "score": score, "head_ok": head_ok, "bubble_ok": bubble_ok,
            "blank": 0.0, "soft": 0.5, "painted": 0.0, "texty": texty,
            "pad_y": 0.0, "narrow": False}


def test_pick_card_prefers_fewer_problems_over_a_better_score(monkeypatch):
    """A clean 3:4 beats a 4:3 with lettering even when 4:3 scores better."""
    def fake_candidates(img, aspect, m, fallback=True, allow_pad=False):
        if abs(aspect - 1.333) < 0.01:
            return [_cand((0, 0, 1, 0.5), -10.0, texty=0.9, bubble_ok=False)]
        return [_cand((0, 0.1, 1, 0.9), 5.0)]
    monkeypatch.setattr(verify_mod, "shape_candidates", fake_candidates)
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    maps = {"_head": {"top": 0.1, "bottom": 0.3, "source": "skin", "confident": True,
                      "figure": True, "keep_top": None},
            "bubble_regions": []}
    card = verify_mod.pick_card(img, [1.333, 0.75], maps)
    assert card["clean"] is True
    assert card["shape"] == pytest.approx(0.75)


def test_pick_card_tie_break_prefers_narrower_shape(monkeypatch):
    """Both shapes carry one problem and the same score -> 3:4 wins (pair-safe)."""
    def fake_candidates(img, aspect, m, fallback=True, allow_pad=False):
        return [_cand((0, 0, 1, 0.5), 1.0, texty=0.9, bubble_ok=False)]
    monkeypatch.setattr(verify_mod, "shape_candidates", fake_candidates)
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    maps = {"_head": {"top": 0.1, "bottom": 0.3, "source": "skin", "confident": True,
                      "figure": True, "keep_top": None},
            "bubble_regions": []}
    card = verify_mod.pick_card(img, [1.333, 0.75], maps)
    assert card["shape"] == pytest.approx(0.75)


# ------------------------------------------------------------------ verify decisions


def _head_maps():
    return {"_head": {"top": 0.1, "bottom": 0.4, "source": "skin", "confident": True,
                      "figure": True, "keep_top": None},
            "painted": [], "edge_rows": (0.0, 0.0)}


def _verify_with(monkeypatch, pick_result, *, risk=False, layout=None):
    monkeypatch.setattr(verify_mod, "_panel_maps", lambda img, options=None: _head_maps())
    monkeypatch.setattr(verify_mod, "is_mask_risk", lambda maps: risk)
    monkeypatch.setattr(verify_mod, "pick_card", lambda img, shapes, maps=None: dict(pick_result))
    if layout is not None:
        def fake_layout(img, maps, layout_shapes):
            return dict(layout)
        monkeypatch.setattr(verify_mod, "layout_crop_problems", fake_layout)
    panels = [{"id": "P001-01", "width": 300, "height": 400}]
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    return verify_mod.verify_chapter(panels, lambda p: img, [0.75, 1.333],
                                    layout_shapes={"single": [1.333], "pair": [0.75]})


def test_verify_drops_mask_risk_panel_with_no_clean_crop(monkeypatch):
    report = _verify_with(monkeypatch,
                          {"shape": 0.75, "box": (0, 0, 1, 0.5), "pad_y": 0.0,
                           "problems": ["lettering", "mask_risk"], "clean": False},
                          risk=True)
    assert report["panels"]["P001-01"]["action"] == "dropped"
    assert report["dropped"][0]["panel"] == "P001-01"
    assert "mask" in report["dropped"][0]["reason"]
    assert report["mask_risk"] == ["P001-01"]


def test_verify_keeps_mask_risk_panel_when_a_clean_crop_exists(monkeypatch):
    report = _verify_with(monkeypatch,
                          {"shape": 0.75, "box": (0, 0.3, 1, 0.9), "pad_y": 0.0,
                           "problems": [], "clean": True},
                          risk=True)
    assert report["panels"]["P001-01"]["action"] == "ok"
    assert report["dropped"] == []
    assert report["mask_risk"] == ["P001-01"]  # flagged, but the clean crop avoids it


def test_verify_marks_head_cut_panel_needs_eyes_not_dropped(monkeypatch):
    report = _verify_with(monkeypatch,
                          {"shape": 0.75, "box": (0, 0.2, 1, 0.7), "pad_y": 0.0,
                           "problems": ["head_cut"], "clean": False},
                          risk=False)
    assert report["panels"]["P001-01"]["action"] == "needs eyes"
    assert report["needs_eyes"] == ["P001-01"]
    assert report["dropped"] == []


def test_verify_drops_panel_whose_best_crop_still_shows_a_bubble(monkeypatch):
    # Bubble painting removed: a figure panel with no clean crop (best
    # window still shows a bubble) is DROPPED and re-paired -- the P1
    # mask-risk rule, now applied to every bubble. Head-cut-only panels
    # still go to needs-eyes (see the test above), not to dropped.
    report = _verify_with(monkeypatch,
                          {"shape": 0.75, "box": (0, 0, 1, 0.5), "pad_y": 0.0,
                           "problems": ["lettering"], "clean": False},
                          risk=False)
    assert report["panels"]["P001-01"]["action"] == "dropped"
    assert report["dropped"][0]["panel"] == "P001-01"
    assert "re-pair" in report["dropped"][0]["reason"]


def test_verify_flags_layout_sensitive_crop(monkeypatch):
    report = _verify_with(
        monkeypatch,
        {"shape": 1.333, "box": (0, 0, 1, 0.5), "pad_y": 0.0, "problems": [], "clean": True},
        risk=False, layout={"single": [], "pair": ["lettering"]})
    assert report["layout_sensitive"] == ["P001-01"]
    assert report["layout_problems"]["P001-01"] == {"single": [], "pair": ["lettering"]}


def test_layout_crop_problems_checks_each_layout(monkeypatch):
    def fake_pick(img, shapes, maps=None):
        if shapes == [0.75]:
            return {"shape": 0.75, "box": (0, 0, 1, 0.5), "pad_y": 0.0,
                    "problems": [], "clean": True}
        return {"shape": 1.333, "box": (0, 0, 1, 0.5), "pad_y": 0.0,
                "problems": ["lettering"], "clean": False}
    monkeypatch.setattr(verify_mod, "pick_card", fake_pick)
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    out = verify_mod.layout_crop_problems(img, {}, {"single": [1.333], "pair": [0.75]})
    assert out == {"single": ["lettering"], "pair": []}
    assert verify_mod.layout_crop_problems(img, {}, None) == {}
