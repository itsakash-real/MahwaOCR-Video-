"""Rule 6 (user's rendering spec): pre-processing white-space eradication
— manhwa_ocr/video/trim.py v2: full ink-bounding-box trim (pure-white OR
pure-black rows/columns are margins) plus slicing strips at large white
gaps. Extends the v1 trimmer (tests/test_trim.py); applied at panel build
via the opt-in `trim_v2` panel setting, after the recap exists and before
pairing/plan — raw page assets are never modified.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.video import panels as panels_mod
from manhwa_ocr.video import trim as trim_mod
from manhwa_ocr.video.trim import (TRIM2_BLACK_MAX, TRIM2_GAP_MIN_PX,
                                   TRIM2_GUTTER_MIN_PX, TRIM2_WHITE_MIN,
                                   apply_trim_v2, compute_trim_v2, gutter_segments,
                                   slice_gaps)

W = 200


def _art(arr, y0, y1, x0=0, x1=None, seed=3):
    """Textured mid-tone art: no pixel is white or black margin."""
    rng = np.random.default_rng(seed)
    x1 = arr.shape[1] if x1 is None else x1
    arr[y0:y1, x0:x1] = rng.integers(60, 200, size=(y1 - y0, x1 - x0, 3), dtype=np.uint8)


def _pil(arr):
    return Image.fromarray(arr, "RGB")


def _blank(h, w=W, fill=(255, 255, 255)):
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:] = fill
    return arr


# --------------------------------------------------------------- bbox trim

def test_white_margins_and_black_gutter_trim_to_ink_bbox():
    # White margins top/bottom/right, a pure-BLACK gutter on the left:
    # both are margins under Rule 6 and all four sides cut exactly.
    arr = _blank(200)
    arr[:, :15] = (0, 0, 0)
    _art(arr, 30, 180, 15, 190)
    res = compute_trim_v2(_pil(arr))
    assert (res.cut_top, res.cut_bottom, res.cut_left, res.cut_right) == (30, 20, 15, 10)
    assert res.flags == []
    assert res.purity == 1.0
    out = apply_trim_v2(_pil(arr), res)
    assert out.size == (200 - 25, 200 - 50)


def test_black_margins_trimmed_unlike_v1():
    # v1 never trims dark bands (light gutters only); Rule 6 names pure
    # black rows/columns as margins too.
    arr = _blank(160, fill=(0, 0, 0))
    _art(arr, 25, 140, 0, 200)
    res = compute_trim_v2(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (25, 20)
    assert (res.cut_left, res.cut_right) == (0, 0)


def test_art_touching_all_edges_is_unchanged():
    arr = _blank(180)
    _art(arr, 0, 180)
    res = compute_trim_v2(_pil(arr))
    assert (res.cut_top, res.cut_bottom, res.cut_left, res.cut_right) == (0, 0, 0, 0)
    assert res.flags == []
    assert slice_gaps(_pil(arr)) == [(0, 180)]


def test_content_row_inside_the_margin_stops_the_trim():
    # The bubble caution, v2 form: a row inside the white band carrying
    # real content (a glyph row: >0.5% non-margin pixels) is ink, not
    # margin — the trim stops above it and everything below stays.
    arr = _blank(200)
    _art(arr, 100, 200)
    arr[40, 60:120] = (30, 30, 30)   # a dark glyph row floating in the band
    res = compute_trim_v2(_pil(arr))
    assert res.cut_top == 40
    assert res.cut_bottom == 0


def test_all_blank_asset_left_untouched_and_flagged():
    res = compute_trim_v2(_pil(_blank(120)))
    assert (res.cut_top, res.cut_bottom, res.cut_left, res.cut_right) == (0, 0, 0, 0)
    assert res.flags == ["all_blank"]


def test_thresholds_are_the_named_constants():
    assert TRIM2_WHITE_MIN == 242 == panels_mod.WHITE_THRESHOLD
    assert TRIM2_BLACK_MAX == 13 == panels_mod.BLACK_THRESHOLD
    assert TRIM2_GAP_MIN_PX == 200


# --------------------------------------------------------------- gap slicing

def _strip(gap_lo, gap_hi, h=600, gap_fill=(255, 255, 255)):
    arr = _blank(h, w=60)
    _art(arr, 0, gap_lo, 0, 60)
    arr[gap_lo:gap_hi] = gap_fill
    _art(arr, gap_hi, h, 0, 60)
    return _pil(arr)


def test_two_panel_strip_slices_at_large_white_gap():
    img = _strip(180, 420)          # a 240px white gap >= the 200px constant
    assert slice_gaps(img) == [(0, 180), (420, 600)]


def test_small_white_gap_does_not_slice():
    img = _strip(200, 320)          # 120px: a line break, not a panel gap
    assert slice_gaps(img) == [(0, 600)]


def test_black_gap_never_slices():
    # Rule 6 point 3 names WHITE gaps; a long black run can be a
    # deliberate night panel, so it is margin for the trim, never a cut.
    img = _strip(180, 420, gap_fill=(0, 0, 0))
    assert slice_gaps(img) == [(0, 600)]


def test_edge_gap_is_margin_not_a_slice_point():
    arr = _blank(500, w=60)
    _art(arr, 260, 500, 0, 60)      # white only at the very top edge
    assert slice_gaps(_pil(arr)) == [(0, 500)]


# ------------------------------------------------- gutter segments (fix 1)

def test_gutter_threshold_is_the_users_15px():
    # The user's analysis: a white/black strip over 15px high between
    # two images is a slice point, not part of either picture.
    assert TRIM2_GUTTER_MIN_PX == 15


def test_gutter_segments_slice_a_white_gap_over_15px():
    img = _strip(200, 300)          # 100px white gap: invisible to
    assert slice_gaps(img) == [(0, 600)]        # slice_gaps (>= 200px)…
    assert gutter_segments(img) == [(0, 200), (300, 600)]   # …seen here


def test_gutter_segments_slice_a_black_gap_too():
    # Fix 1 names pure black as well as pure white (unlike slice_gaps,
    # whose white-only rule guards whole-strip slicing decisions).
    img = _strip(200, 300, gap_fill=(0, 0, 0))
    assert gutter_segments(img) == [(0, 200), (300, 600)]


def test_gutter_gap_of_exactly_15px_does_not_slice():
    img = _strip(200, 215)          # 15px: "over 15px" is the rule
    assert gutter_segments(img) == [(0, 600)]
    img = _strip(200, 216)          # 16px slices
    assert gutter_segments(img) == [(0, 200), (216, 600)]


def test_gutter_edge_runs_are_margins_not_segments():
    arr = _blank(500, w=60)
    _art(arr, 260, 500, 0, 60)      # white only at the very top edge
    assert gutter_segments(_pil(arr)) == [(0, 500)]


# --------------------------------------------------------------- wiring

def test_fingerprint_stable_when_trim_v2_off(tmp_path):
    page = tmp_path / "page_001.png"
    Image.new("RGB", (100, 100), (255, 255, 255)).save(page)
    fp_default = panels_mod._fingerprint([page], panels_mod._settings(None))
    fp_explicit = panels_mod._fingerprint(
        [page], panels_mod._settings({"trim_v2": False}))
    fp_on = panels_mod._fingerprint(
        [page], panels_mod._settings({"trim_v2": True}))
    assert fp_default == fp_explicit
    assert fp_on != fp_default


def _two_panel_page(path):
    """Two textured panels on white, detect()'s standard fare."""
    w, h = 300, 1000
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for y0, y1, colour in [(40, 400, (200, 60, 60)), (460, 820, (60, 160, 60))]:
        d.rectangle((10, y0, w - 10, y1 - 1), fill=colour)
        for x in range(10, w - 10, 7):
            d.line((x, y0, x, y1 - 1), fill=(colour[0] // 2, colour[1] // 2, colour[2] // 2))
    img.save(path)
    return path


def test_detect_slicing_wiring_with_stubs(tmp_path, monkeypatch):
    # compute_trim_v2 / slice_gaps are unit-tested above; stubs prove the
    # detect() wiring: a sliced strip becomes separate panels with
    # canonical renumbered ids and a trim2 record on each piece — and
    # with the setting off, nothing changes at all.
    page = _two_panel_page(tmp_path / "page_001.png")
    off, _ = panels_mod.detect([page], None)
    assert len(off) == 2
    assert all(p.trim2 == {} for p in off)

    monkeypatch.setattr(trim_mod, "compute_trim_v2",
                        lambda img, white_min=242, black_max=13: trim_mod.Trim2Result())
    monkeypatch.setattr(trim_mod, "slice_gaps",
                        lambda img, gap_min_px=200, white_min=242: [(0, 100), (150, 360)])
    on, _ = panels_mod.detect([page], {"trim_v2": True})
    assert len(on) == 4
    assert [p.id for p in on] == ["P001-01", "P001-02", "P001-03", "P001-04"]
    assert [p.trim2["piece"] for p in on] == [0, 1, 0, 1]
    assert all(p.trim2["pieces"] == 2 for p in on)
    assert (on[0].y0, on[0].y1) == (off[0].y0, off[0].y0 + 100)
    assert (on[1].y0, on[1].y1) == (off[0].y0 + 150, off[0].y1)
    assert (on[2].y0, on[3].y1) == (off[1].y0, off[1].y1)


def test_detect_records_gutter_segments_as_metadata_only(tmp_path, monkeypatch):
    # Analysis fix 1 wiring: gutter segments land on the panel as
    # METADATA (fractions of the piece height) — panel ids and geometry
    # are untouched, so recap tags and sync can never break on them;
    # the planner turns the metadata into segment sub-shots.
    page = _two_panel_page(tmp_path / "page_001.png")
    monkeypatch.setattr(trim_mod, "compute_trim_v2",
                        lambda img, white_min=242, black_max=13: trim_mod.Trim2Result())
    monkeypatch.setattr(trim_mod, "slice_gaps",
                        lambda img, gap_min_px=200, white_min=242: [(0, img.height)])
    monkeypatch.setattr(trim_mod, "gutter_segments",
                        lambda img, min_px=15, white_min=242, black_max=13:
                        [(0, 100), (150, img.height)])
    on, _ = panels_mod.detect([page], {"trim_v2": True})
    assert [p.id for p in on] == ["P001-01", "P001-02"]     # no renumbering
    for p in on:
        assert len(p.gutter_segments) == 2
        (a0, a1), (b0, b1) = p.gutter_segments
        assert a0 == 0.0 and b1 == 1.0 and 0.0 < a1 < b0 < 1.0


def test_detect_bbox_wiring_with_stub(tmp_path, monkeypatch):
    page = _two_panel_page(tmp_path / "page_001.png")
    off, _ = panels_mod.detect([page], None)
    monkeypatch.setattr(trim_mod, "compute_trim_v2",
                        lambda img, white_min=242, black_max=13:
                        trim_mod.Trim2Result(10, 5, 0, 0, [], 1.0))
    monkeypatch.setattr(trim_mod, "slice_gaps",
                        lambda img, gap_min_px=200, white_min=242: [(0, img.height)])
    on, _ = panels_mod.detect([page], {"trim_v2": True})
    assert len(on) == 2
    for before, after in zip(off, on):
        assert after.trim2["top"] == 10.0 and after.trim2["bottom"] == 5.0
        assert after.y0 == before.y0 + 10
        assert after.y1 == before.y1 - 5
