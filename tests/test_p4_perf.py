"""P4 — PERFORMANCE: frame-path caches + PR1-PR4 cache regression locks.

P3 made every frame a full Ken Burns resample (54% of frame time in profile),
so P4 does NOT try to cache the moving viewport. It caches the rebuilds
around it (backdrop, card resize, font) and locks the older caches in place.
Pixel output must be identical with caches warm or cold.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.video import frames as frames_mod  # noqa: E402
from manhwa_ocr.video.cropcache import crop_key, load_crop, store_crop  # noqa: E402
from manhwa_ocr.video.frames import (  # noqa: E402
    Format,
    ShotRenderer,
    _BG_CACHE,
    _SRC_CACHE,
    _overlay_font,
    _shadow_mask,
    _vignette_grid,
    format_from,
)
from manhwa_ocr.video.panels import _load_page  # noqa: E402
from manhwa_ocr.video.render import _jobs  # noqa: E402


def _panel_img(color, w=300, h=400):
    img = Image.new("RGB", (w, h), color)
    ImageDraw.Draw(img).rectangle((50, 50, 150, 200), fill=(20, 20, 220))
    return img


def _shot(**kw):
    s = {"start": 0, "end": 4, "layout": "single", "background": "blur", "bg_panel": "A",
         "cards": [{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "push_in"}],
         "exit": "cut", "effects": []}
    s.update(kw)
    return s


def _renderer(panel, fmt=None, **kw):
    fmt = fmt or Format(1920, 1080, 30)
    return ShotRenderer(_shot(**kw), {"A": panel}, fmt, {})


# ------------------------------------------------------------------ P4 caches


def test_bg_cache_reuses_same_panel():
    _BG_CACHE.clear()
    panel = _panel_img((200, 50, 50))
    r1 = _renderer(panel)
    r2 = _renderer(panel)
    assert r2.bg is r1.bg
    assert len(_BG_CACHE) == 1


def test_bg_cache_no_stale_hit_for_different_image_same_id():
    _BG_CACHE.clear()
    r1 = _renderer(_panel_img((200, 30, 30)))
    r2 = _renderer(_panel_img((30, 30, 200)))
    assert r2.bg is not r1.bg
    assert not np.array_equal(r2.bg, r1.bg)


def test_src_cache_reuses_resize_and_changes_with_crop():
    _SRC_CACHE.clear()
    panel = _panel_img((200, 50, 50))
    r1 = _renderer(panel)
    r2 = _renderer(panel)
    assert r2.cards[0].src is r1.cards[0].src
    r3 = _renderer(panel, cards=[{"panel": "A", "crop": [0, 0, 0.5, 1], "enter": "cut",
                                  "at": 0, "motion": "push_in"}])
    assert r3.cards[0].src is not r1.cards[0].src


def test_font_cached():
    assert _overlay_font(24) is _overlay_font(24)
    assert _overlay_font(24) is not _overlay_font(32)


def test_frame_pixel_identical_with_and_without_cache():
    panel = _panel_img((200, 50, 50))
    _BG_CACHE.clear()
    _SRC_CACHE.clear()
    warm = _renderer(panel).frame_array(1.0)
    _BG_CACHE.clear()
    _SRC_CACHE.clear()
    cold = _renderer(panel).frame_array(1.0)
    assert np.array_equal(warm, cold)


# ------------------------------------------------------------------ older caches still live


def test_crop_cache_roundtrip_and_key_stability(tmp_path):
    panel = {"id": "P001-01", "x0": 0, "x1": 900, "y0": 0, "y1": 400,
             "segments": [{"file": "page_001.png", "y0": 0, "y1": 400}]}
    k1 = crop_key("fp-1", panel, [0.75, 1.0], {"full_width": True}, None)
    k2 = crop_key("fp-1", panel, [0.75, 1.0], {"full_width": True}, None)
    assert k1 == k2
    assert crop_key("fp-2", panel, [0.75, 1.0], {"full_width": True}, None) != k1
    entry = {"pick": {"box": [0, 0, 1, 1]}, "head": {}, "edge_rows": [0.0, 0.0]}
    store_crop(tmp_path, "1", k1, entry)
    assert load_crop(tmp_path, "1", k1) == entry
    assert load_crop(tmp_path, "1", "missing-key") is None


def test_vignette_and_shadow_caches_hit():
    # Vignette: call background() directly — a second ShotRenderer with the
    # same panel would hit the P4 bg cache and never reach background().
    _vignette_grid.cache_clear()
    _shadow_mask.cache_clear()
    fmt = Format(1920, 1080, 30)
    panel = _panel_img((200, 50, 50))
    frames_mod.background("blur", panel, fmt)
    frames_mod.background("blur", panel, fmt)
    assert _vignette_grid.cache_info().hits >= 1
    # Shadow: two Cards with identical geometry share the cached mask.
    _renderer(panel, fmt=fmt)
    _renderer(panel, fmt=fmt)
    assert _shadow_mask.cache_info().hits >= 1


def test_load_page_lru_returns_same_object(tmp_path):
    p = tmp_path / "page_001.png"
    _panel_img((10, 20, 30)).save(p)
    mtime = p.stat().st_mtime
    assert _load_page(str(p), mtime) is _load_page(str(p), mtime)


def test_jobs_key_stable_across_identical_plans(tmp_path):
    from manhwa_ocr.video.media import pages_dir
    pages = pages_dir(tmp_path, "1")
    pages.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (900, 400), "white").save(pages / "page_001.png")
    plan = {"duration": 6.0, "shots": [
        {"id": "S001", "start": 0.0, "end": 6.0, "layout": "single",
         "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1]}]}]}
    panels = {"panels": [{"id": "P001-01", "page": 1,
                          "segments": [{"file": "page_001.png", "y0": 0, "y1": 400}],
                          "x0": 0, "x1": 900, "y0": 0, "y1": 400}],
              "norm_width": 900, "fingerprint": "fp-aaa"}
    fmt = format_from("16:9", 30, False)
    a = _jobs(tmp_path, "1", plan, panels, fmt, {}, False)
    b = _jobs(tmp_path, "1", plan, panels, fmt, {}, False)
    assert [j["out"] for j in a] == [j["out"] for j in b]
