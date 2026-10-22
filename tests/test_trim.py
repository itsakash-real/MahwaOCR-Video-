"""Build 1: the white-space trimmer v1 (manhwa_ocr/video/trim.py) and its
opt-in wiring in panel detection."""

import numpy as np
import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.video import panels as panels_mod
from manhwa_ocr.video.trim import apply_trim, compute_trim, detect_gutter_colors

W = 1000  # wide enough that share arithmetic is precise


def _art_rows(arr, y0, y1, seed=7):
    """Textured dark art: almost no pixel matches a light gutter."""
    rng = np.random.default_rng(seed)
    arr[y0:y1] = rng.integers(0, 130, size=(y1 - y0, arr.shape[1], 3), dtype=np.uint8)


def _speck_rows(arr, y0, y1, dark_share):
    """Rows whose gutter-share is (1 - dark_share): dark specks on white."""
    for y in range(y0, y1):
        n = int(round(dark_share * arr.shape[1]))
        if n:
            xs = np.linspace(0, arr.shape[1] - 1, n).astype(int)
            arr[y, xs] = (20, 20, 20)


def _img(h, fill=(255, 255, 255)):
    arr = np.zeros((h, W, 3), dtype=np.uint8)
    arr[:] = fill
    return arr


def _pil(arr):
    return Image.fromarray(arr, "RGB")


# --------------------------------------------------------------- unit: cuts

def test_clean_bands_trim_exactly():
    arr = _img(600)
    _art_rows(arr, 100, 540)
    res = compute_trim(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (100, 60)
    assert res.flags == []
    assert res.purity == 1.0


def test_no_band_no_trim():
    arr = _img(400)
    _art_rows(arr, 0, 400)
    res = compute_trim(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (0, 0)
    assert res.flags == []


def test_cream_gutter_trims():
    arr = _img(500, fill=(250, 244, 230))
    _art_rows(arr, 80, 460)
    res = compute_trim(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (80, 40)
    assert res.flags == []


def test_one_sided_band_still_trims():
    # dark art touches the top edge; only the bottom is banded
    arr = _img(500)
    _art_rows(arr, 0, 420)
    res = compute_trim(_pil(arr))
    assert res.cut_top == 0
    assert res.cut_bottom == 80


def test_black_bands_never_trim():
    arr = _img(500, fill=(0, 0, 0))
    _art_rows(arr, 100, 400, seed=3)
    arr[100:400] = np.clip(arr[100:400].astype(int) + 60, 0, 200)  # visible art
    res = compute_trim(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (0, 0)


def test_bubble_in_band_stops_trim_and_flags():
    arr = _img(400)
    _speck_rows(arr, 50, 91, 0.04)   # the bubble: outline rows in the band
    _art_rows(arr, 131, 400)
    res = compute_trim(_pil(arr))
    assert res.cut_top == 50                 # stopped at the bubble, not through it
    assert "sparse_below_top" in res.flags   # white below the bubble remains: review
    assert res.cut_bottom == 0


def test_gradient_flagged_not_cut():
    arr = _img(300)
    for i in range(150):                     # specks thicken 0% -> ~50%
        _speck_rows(arr, i, i + 1, i / 300.0)
    _art_rows(arr, 150, 300)
    res = compute_trim(_pil(arr))
    assert res.cut_top == 0
    assert "gradient_top" in res.flags


def test_all_white_panel_untouched():
    res = compute_trim(_pil(_img(300)))
    assert (res.cut_top, res.cut_bottom) == (0, 0)
    assert res.flags == ["all_blank"]


def test_cap_limits_and_flags():
    arr = _img(1000)
    _art_rows(arr, 500, 1000)
    res = compute_trim(_pil(arr))
    assert res.cut_top == 400                # 40% of 1000, not the full 500
    assert "capped_top" in res.flags


def test_min_height_guard():
    arr = _img(100)
    _art_rows(arr, 45, 55)
    res = compute_trim(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (0, 0)
    assert "min_height" in res.flags


def test_uniform_pale_panel_untouched():
    arr = _img(300, fill=(205, 205, 205))
    res = compute_trim(_pil(arr))
    assert (res.cut_top, res.cut_bottom) == (0, 0)


def test_apply_trim_is_exact_crop():
    arr = _img(600)
    _art_rows(arr, 100, 540)
    img = _pil(arr)
    res = compute_trim(img)
    out = apply_trim(img, res)
    assert out.height == 600 - 160
    assert np.array_equal(np.asarray(out), arr[100:540])
    assert img.height == 600                 # original untouched


def test_gutter_detection_per_edge():
    arr = _img(200)
    _art_rows(arr, 0, 120)
    top, bottom = detect_gutter_colors(arr)
    assert top is None                       # dark art at the top edge
    assert bottom is not None                # white band at the bottom


# --------------------------------------------------------------- wiring

def test_fingerprint_stable_when_trim_off(tmp_path):
    # The bake-off safety property: with trim_v1 off (or at its defaults) the
    # panels fingerprint must be IDENTICAL to before the setting existed, so
    # shipped chapters are never silently re-detected.
    page = tmp_path / "page_001.png"
    Image.new("RGB", (100, 100), (255, 255, 255)).save(page)
    fp_default = panels_mod._fingerprint([page], panels_mod._settings(None))
    fp_explicit = panels_mod._fingerprint(
        [page], panels_mod._settings({"trim_v1": False, "trim_cap": 0.40}))
    fp_on = panels_mod._fingerprint(
        [page], panels_mod._settings({"trim_v1": True}))
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


def test_detect_wiring_with_stubbed_trim(tmp_path, monkeypatch):
    # compute_trim itself is unit-tested above; here a stub proves the
    # detect() wiring: bounds shift by the cuts, segments follow, the record
    # lands on the panel — and with the setting off, nothing changes at all.
    from manhwa_ocr.video import trim as trim_mod

    page = _two_panel_page(tmp_path / "page_001.png")
    off, _ = panels_mod.detect([page], None)
    assert len(off) == 2
    assert all(p.trim == {} for p in off)

    stub = lambda img, cap_share=0.40: trim_mod.TrimResult(10, 5, ["stub"])
    monkeypatch.setattr(trim_mod, "compute_trim", stub)
    on, _ = panels_mod.detect([page], {"trim_v1": True})
    assert len(on) == 2
    for before, after in zip(off, on):
        assert after.trim == {"top": 10.0, "bottom": 5.0, "flags": ["stub"]}
        assert after.y0 == before.y0 + 10
        assert after.y1 == before.y1 - 5
        assert after.segments[0]["y0"] == int(after.y0)


def test_build_panels_json_trim_key(tmp_path, monkeypatch):
    from manhwa_ocr.video import trim as trim_mod
    from manhwa_ocr.video.media import pages_dir
    from manhwa_ocr.video.panels import build_panels

    stub = lambda img, cap_share=0.40: trim_mod.TrimResult(10, 5, ["stub"])
    monkeypatch.setattr(trim_mod, "compute_trim", stub)

    root = tmp_path / "s"
    folder = pages_dir(root, "1")
    folder.mkdir(parents=True)
    _two_panel_page(folder / "page_001.png")
    data = build_panels(root, "1", {"trim_v1": True}, log=lambda *_: None)
    assert all(p["trim"]["top"] == 10.0 for p in data["panels"])

    root2 = tmp_path / "s2"
    folder2 = pages_dir(root2, "1")
    folder2.mkdir(parents=True)
    _two_panel_page(folder2 / "page_001.png")
    data2 = build_panels(root2, "1", None, log=lambda *_: None)
    assert all("trim" not in p for p in data2["panels"])   # off: key absent entirely
