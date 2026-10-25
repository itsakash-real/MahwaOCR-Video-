"""White-space auto-crop v3 — the art-body (border-box) pass
(manhwa_ocr/video/bodytrim.py, wired into panels.detect via the
`body_trim` setting; docs/WHITESPACE_AUTOCROP.md).

The failure being fixed: a panel's SFX lettering and speech bubbles
overflow onto the page's white canvas OUTSIDE its drawn border box, and
every row-based trimmer stops at that ink — so the white between the box
and the ink rode inside the rendered frame (qcframes/S001.png) while
panels.json recorded trim2 = 0.0 everywhere. The body pass crops the
panel bounds to the drawn box; the guards never destroy art.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.video import panels as panels_mod
from manhwa_ocr.video.bodytrim import compute_body
from manhwa_ocr.video.frameqc import (_axis_guaranteed, _panelfit_rects,
                                      _rect_regions, _shot_failures)

W = 400


def _canvas(h, w=W):
    arr = np.full((h, w, 3), 255, dtype=np.uint8)
    return arr


def _art(arr, y0, y1, x0, x1, seed=5):
    """Textured mid-tone art filling the border box."""
    rng = np.random.default_rng(seed)
    arr[y0:y1, x0:x1] = rng.integers(60, 200, size=(y1 - y0, x1 - x0, 3), dtype=np.uint8)


def _box(arr, y0, y1, x0, x1, t=3):
    """A drawn border box: dark 3px frame, art inside."""
    dark = (20, 20, 20)
    arr[y0:y0 + t, x0:x1] = dark
    arr[y1 - t:y1, x0:x1] = dark
    arr[y0:y1, x0:x0 + t] = dark
    arr[y0:y1, x1 - t:x1] = dark
    _art(arr, y0 + t, y1 - t, x0 + t, x1 - t)


def _pil(arr):
    return Image.fromarray(arr, "RGB")


# --------------------------------------------------------------- the S001 class

def test_s001_replica_crops_to_the_border_box():
    # White canvas; SFX strokes rising above the box; a speech bubble
    # hanging below it. Every row-trimmer stops at that ink; the body
    # pass crops exactly at the drawn border lines.
    arr = _canvas(600)
    for x in range(60, 340, 25):          # sparse SFX strokes above the box
        arr[40:95, x:x + 3] = (10, 10, 10)
    _box(arr, 100, 500, 40, 360)
    for y in range(520, 586):             # a speech bubble below the box:
        a = (y - 552) / 33.0              # a 3px dark ellipse outline + text
        half = int(round(80 * max(0.0, np.sqrt(max(0.0, 1.0 - a * a)))))
        if half < 2:
            continue
        arr[y, 220 - half:220 - half + 3] = (10, 10, 10)
        arr[y, 220 + half - 3:220 + half] = (10, 10, 10)
    arr[540:556, 190:250] = (10, 10, 10)  # the bubble's text block
    res = compute_body(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (100, 100)
    assert (res.cut_left, res.cut_right) == (40, 40)
    assert res.flags == []


def test_snow_panel_without_a_border_is_untouched():
    # White art (a snow sky with pale speckles): no border lines exist,
    # nothing qualifies, the panel is exactly as it was.
    arr = _canvas(400)
    rng = np.random.default_rng(2)
    speck = rng.integers(0, 6, size=(400, W, 3), dtype=np.uint8)
    arr[speck.sum(axis=2) < 10] = (225, 228, 235)
    res = compute_body(_pil(arr))
    assert (res.cut_top, res.cut_bottom, res.cut_left, res.cut_right) == (0, 0, 0, 0)
    assert "no_border" in res.flags and "no_border_x" in res.flags


def test_glued_two_panel_strip_keeps_both_boxes():
    # Two bordered boxes stacked with a white gutter: the outer canvas is
    # trimmed; the gutter and the second box stay (never amputated).
    arr = _canvas(300)
    _box(arr, 30, 120, 40, 360)
    _box(arr, 150, 240, 40, 360)
    res = compute_body(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (30, 60)
    assert (res.cut_left, res.cut_right) == (40, 40)
    assert res.flags == []


def test_solid_art_above_the_outermost_line_blocks_the_crop():
    # [dense art][canvas][thin line][canvas][thin line]: the topmost thin
    # band would amputate the art block above it — the dense-band guard
    # refuses the crop on that axis.
    arr = _canvas(300)
    _art(arr, 0, 60, 0, W, seed=9)
    arr[100:103, 30:370] = (20, 20, 20)
    arr[180:183, 30:370] = (20, 20, 20)
    res = compute_body(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (0, 0)
    assert "multi_art" in res.flags


def test_body_smaller_than_min_keep_is_refused():
    # Two thin lines close together near the top: the "box" between them
    # keeps only a third of the axis — min_keep refuses.
    arr = _canvas(300)
    arr[40:43, 30:370] = (20, 20, 20)
    arr[70:73, 30:370] = (20, 20, 20)
    res = compute_body(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (0, 0)
    assert "min_keep" in res.flags


def test_side_cap_clamps_an_enormous_sfx_band():
    # A box whose top canvas region is ~42% of the panel: the shave is
    # clamped to the 40% cap and flagged, like every other trim stage.
    arr = _canvas(600)
    for x in range(60, 340, 30):
        arr[30:245, x:x + 3] = (10, 10, 10)   # a huge sparse SFX zone
    _box(arr, 250, 590, 40, 360)
    res = compute_body(_pil(arr))
    assert res.cut_top == int(0.40 * 600)
    assert "capped_top" in res.flags
    assert res.cut_bottom == 10


def test_unbordered_webtoon_block_crops_to_the_dense_art_span():
    # The common webtoon shape (regressor P002-06): no drawn border — a
    # block of art with bubbles floating on white canvas above and below.
    # The dense-span fallback crops to the art; the bubble zones are all
    # bubble-sparse rows, so the cut is allowed.
    arr = _canvas(600)
    for x in range(150, 260, 12):          # bubble 1: outline-ish arcs + text
        arr[45:115, x:x + 3] = (10, 10, 10)
    arr[70:86, 170:240] = (10, 10, 10)
    arr[140:470, :] = np.random.default_rng(6).integers(
        60, 200, size=(330, 400, 3), dtype=np.uint8)
    for x in range(160, 250, 12):          # bubble 2 below
        arr[500:565, x:x + 3] = (10, 10, 10)
    arr[520:536, 180:250] = (10, 10, 10)
    res = compute_body(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (140, 130)
    assert (res.cut_left, res.cut_right) == (0, 0)
    assert "dense_span" in res.flags


def test_dense_span_refuses_a_textured_discarded_zone():
    # Snow-art rows below the art block: textured (40% content — neither
    # dense art nor bubble-sparse canvas). The zone guard refuses the
    # bottom cut; the clean top zone still crops.
    arr = _canvas(600)
    rng = np.random.default_rng(11)
    arr[100:400, :] = rng.integers(60, 200, size=(300, 400, 3), dtype=np.uint8)
    snow = rng.random((200, 400, 1)) < 0.4
    arr[400:600, :] = np.where(snow, (205, 205, 210), (250, 250, 250)).astype(np.uint8)
    res = compute_body(_pil(arr))
    assert res.cut_top == 100
    assert res.cut_bottom == 0
    assert "zone_not_canvas" in res.flags and "dense_span" in res.flags


def test_x_axis_fallback_flags_are_tagged():
    # Full-width horizontal borders, but no side borders and no dense
    # column span (only sparse vertical strokes): y crops, x refuses
    # with the _x-tagged flag.
    arr = _canvas(400)
    arr[50:53, :] = (20, 20, 20)
    arr[347:350, :] = (20, 20, 20)
    for x in range(80, 330, 25):
        arr[100:250, x:x + 3] = (15, 15, 15)   # sparse vertical SFX strokes
    res = compute_body(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (50, 50)
    assert (res.cut_left, res.cut_right) == (0, 0)
    assert "no_border_x" in res.flags and "no_border" not in res.flags


# --------------------------------------------------------------- detect wiring

def test_detect_body_wiring(tmp_path, monkeypatch):
    # With body_trim on (the default) each panel carries a body record and
    # bordered pages are cropped at the box; with it off, geometry is the
    # pre-v3 result and no record exists. The SFX strokes above the first
    # box are content to every row-trimmer — only the body pass removes
    # their canvas.
    from manhwa_ocr.video import bodytrim as bodytrim_mod

    page = tmp_path / "page_001.png"
    img = Image.new("RGB", (400, 900), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for x in range(80, 340, 30):
        d.rectangle((x, 25, x + 2, 55), fill=(10, 10, 10))   # sparse SFX above the box
    d.rectangle((40, 60, 360, 380), outline=(20, 20, 20), width=3)
    d.rectangle((43, 63, 357, 377), fill=(170, 140, 140))
    d.rectangle((40, 520, 360, 840), outline=(20, 20, 20), width=3)
    d.rectangle((43, 523, 357, 837), fill=(140, 170, 140))
    img.save(page)

    on, _ = panels_mod.detect([page], {})
    assert len(on) == 2
    assert all(p.body for p in on)
    assert on[0].body["top"] > 0
    assert on[0].y0 == pytest.approx(60, abs=2)
    assert on[0].y1 == pytest.approx(380, abs=2)

    monkeypatch.setattr(bodytrim_mod, "compute_body",
                        lambda img: bodytrim_mod.BodyResult())
    off, _ = panels_mod.detect([page], {"body_trim": False})
    assert all(p.body == {} for p in off)
    assert off[0].y0 < on[0].y0  # un-cropped: the SFX canvas stays inside


def test_panels_version_bumped_for_the_body_pass():
    # The version is part of the fingerprint: every existing session
    # re-detects once when it upgrades, picking up the body crops.
    from manhwa_ocr.video.panels import PANELS_VERSION
    assert PANELS_VERSION >= 5


# --------------------------------------------------------------- frameqc v3

PLAN_SHOT = {"id": "S001", "panelfit": True,
             "cards": [{"panel": "P001-01",
                        "panelfit": {"kind": "zoom", "base_w": 600, "base_h": 1080,
                                     "zoom_start": 1.0, "zoom_end": 1.1,
                                     "zoom_rate": 0.05}}]}


def _frame_with_white_band(band_h=40):
    """A 1920x1080 frame: art everywhere except a white band across the top
    of a centred 600px pillar (the S001 geometry)."""
    rng = np.random.default_rng(7)
    f = rng.integers(40, 220, size=(1080, 1920, 3), dtype=np.uint8)
    f[0:band_h, 660:1260] = (250, 250, 250)
    return f


def test_panelfit_white_band_at_a_touched_wall_fails_when_body_guaranteed():
    bodies = {"P001-01": {"top": 30.0, "bottom": 20.0, "left": 0.0, "right": 0.0,
                          "flags": []}}
    row = _shot_failures(PLAN_SHOT, {"start": _frame_with_white_band()}, 1.0, bodies)
    assert any("record/render divergence" in f for f in row["failures"])


def test_panelfit_white_band_is_only_a_note_without_a_body_record():
    row = _shot_failures(PLAN_SHOT, {"start": _frame_with_white_band()}, 1.0, {})
    assert row["failures"] == []
    assert any("near-white" in n for n in row["notes"])


def test_panelfit_white_band_carrying_ink_is_content():
    f = _frame_with_white_band()
    f[0:40, 660:1260:9] = (30, 30, 30)   # SFX strokes inside the white band
    row = _shot_failures(PLAN_SHOT, {"start": f}, 1.0,
                         {"P001-01": {"top": 30.0, "bottom": 20.0, "left": 0.0,
                                      "right": 0.0, "flags": []}})
    assert row["failures"] == []
    assert any("carries art" in n for n in row["notes"])


def test_drift_shot_checks_sides_not_the_oversized_walls():
    spec = {"kind": "drift", "base_w": 600, "base_h": 1296,
            "zoom_end": 1.0, "zoom_rate": 0.0}
    rects = _panelfit_rects(spec)
    assert len(rects) == 2
    r = rects[0]
    assert r["y"] < 0 and r["y"] + r["h"] > 1080     # vertically oversized
    f = np.random.default_rng(8).integers(40, 220, size=(1080, 1920, 3), dtype=np.uint8)
    f[:, 660:696] = (250, 250, 250)                  # white slab on the left edge
    row = _shot_failures({"id": "S2", "panelfit": True,
                          "cards": [{"panel": "P", "panelfit": spec}]},
                         {"start": f, "end": f}, 1.0,
                         {"P": {"top": 10.0, "bottom": 10.0, "left": 20.0,
                                "right": 20.0, "flags": []}})
    assert any("record/render divergence" in s for s in row["failures"])


def test_axis_guarantee_needs_both_sides_and_no_blockers():
    ok = {"top": 30.0, "bottom": 20.0, "left": 0.0, "right": 0.0, "flags": []}
    assert _axis_guaranteed(ok, ("top",)) and _axis_guaranteed(ok, ("bottom",))
    assert not _axis_guaranteed(ok, ("left",))
    capped = {**ok, "top": 432.0, "flags": ["capped_top"]}
    assert not _axis_guaranteed(capped, ("top",))
    assert not _axis_guaranteed(capped, ("bottom",))   # a cap blocks the whole axis
    xok = {"top": 0.0, "bottom": 0.0, "left": 12.0, "right": 10.0,
           "flags": ["no_border"]}
    assert _axis_guaranteed(xok, ("left",))


def test_panelfit_rects_are_centre_anchored_and_zoom_to_end():
    rects = _panelfit_rects(PLAN_SHOT["cards"][0]["panelfit"])
    assert [r["at"] for r in rects] == ["start", "end"]
    assert (rects[0]["x"], rects[0]["w"]) == (660.0, 600.0)
    assert (rects[0]["y"], rects[0]["h"]) == (0.0, 1080.0)
    assert rects[1]["w"] == pytest.approx(660.0)  # 600 * 1.1


def test_rect_regions_skips_bled_edges_and_clamps_the_rest():
    f = np.zeros((1080, 1920, 3), dtype=np.uint8)
    big = {"x": -100.0, "y": -50.0, "w": 2120.0, "h": 1180.0}
    assert _rect_regions(f, big, 1.0) is None       # every edge bled: nothing measurable
    pillar = {"x": 660.0, "y": 0.0, "w": 600.0, "h": 1080.0}
    regions = _rect_regions(f, pillar, 1.0)
    assert set(regions) == {"top", "bottom", "left", "right"}
    assert regions["top"].shape[0] == max(1, int(round(0.06 * 600)))
