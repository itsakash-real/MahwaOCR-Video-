"""Build 1 — white-space trimmer v1 (blank rows only).

Manhwa panels arrive as tall strips with the source's blank gutter baked in
above and below the artwork. Any plan window that includes those rows shows
a white slab inside the video frame; the standing rule is that panel art
must touch the frame's top and bottom walls. The detector's older edge
tightening (panels.trim_edge_rows) only recognises rows that are almost
entirely brighter than a fixed gray; this module is the stricter,
review-aware version:

- The gutter colour is detected per panel edge (cream/off-white gutters
  count), never assumed to be pure white, and only LIGHT gutters are ever
  trimmed — never dark bands.
- A row is blank only when (almost) every pixel matches the gutter colour.
  Any content — a bubble outline, a glyph, a border — stops the trim. A
  bubble floating in the band therefore stops it too, and the panel is
  FLAGGED for human review (the white below the bubble is interior and
  stays; removing in-band bubbles is v1.1, after the detector exists).
- Safeguards: a per-side cap (default 40% of panel height, excess flagged),
  gradient fades flagged instead of cut, a minimum-height guard, all-white
  panels left untouched and flagged. Edges only, never mid-panel.

The trim runs inside panel detection when the `trim_v1` panel setting is on
(opt-in per series config; panels.json then records each panel's trim
amounts and flags). `video trim --chapter N` writes a read-only report of
what the trimmer WOULD do to a chapter's panels, for bake-offs and review.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

BLANK_SHARE = 0.995    # a row is blank when >= this share of pixels match the gutter
DENSE_SHARE = 0.5      # a row is solid art when < this share matches the gutter
SOFT_SHARE = 0.90      # first content row still >= this share -> possibly a fade
GUTTER_TOL = 12        # per-channel tolerance for "matches the gutter colour"
MIN_GUTTER_LIGHT = 190  # the detected gutter's darkest channel must be at least this
DEFAULT_CAP = 0.40     # max share of panel height cut from one side
MIN_KEEP_SHARE = 0.10  # never leave less than this share of the panel
MIN_KEEP_PX = 24

Gutter = Tuple[float, float, float]


@dataclass
class TrimResult:
    cut_top: int = 0        # rows, in image pixels
    cut_bottom: int = 0
    flags: List[str] = field(default_factory=list)
    purity: float = 1.0     # lowest gutter-share among removed rows (proof)


def _edge_gutter(sample: np.ndarray) -> Optional[Gutter]:
    """The gutter colour of one edge's sampled rows, or None."""
    med = np.median(sample, axis=0)
    if float(med.min()) < MIN_GUTTER_LIGHT:
        return None
    # The edge must actually be dominated by that colour.
    if (np.abs(sample - med).max(axis=1) <= GUTTER_TOL).mean() < 0.5:
        return None
    return tuple(float(c) for c in med)


def detect_gutter_colors(arr: np.ndarray) -> Tuple[Optional[Gutter], Optional[Gutter]]:
    """(top gutter, bottom gutter), each detected from its own edge rows —
    a panel banded on one side only must still trim on that side."""
    h = arr.shape[0]
    if h == 0:
        return None, None
    k = min(8, h)
    top = arr[:k].reshape(-1, 3).astype(np.float32)
    bottom = arr[-k:].reshape(-1, 3).astype(np.float32)
    return _edge_gutter(top), _edge_gutter(bottom)


def gutter_shares(arr: np.ndarray, gutter: Gutter, tol: int = GUTTER_TOL) -> np.ndarray:
    """Per-row share of pixels matching the gutter colour."""
    diff = np.abs(arr.astype(np.int16) - np.asarray(gutter, dtype=np.int16)).max(axis=2)
    return (diff <= tol).mean(axis=1)


def _side(shares: np.ndarray, from_top: bool, cap_px: int,
          sparse_gap: int) -> tuple[int, List[str]]:
    """(rows to cut, flags) for one edge of the panel."""
    n = len(shares)
    s = shares if from_top else shares[::-1]
    blank = s >= BLANK_SHARE
    if blank.all():
        return 0, []  # handled by the caller's all-blank guard
    stop = int(np.argmax(~blank))  # first content row, from this edge
    if stop == 0:
        return 0, []
    flags: List[str] = []
    dense_rel = int(np.argmax(s < DENSE_SHARE)) if (s < DENSE_SHARE).any() else n
    zone = s[stop:dense_rel]
    if (s[stop] >= SOFT_SHARE and zone.size
            and (zone < BLANK_SHARE).all()
            and float(zone[-1]) < float(zone[0]) - 0.02):
        # The edge fades gradually toward the art and never returns to blank
        # on the way: a gradient, which v1 flags rather than cuts. (Sparse
        # content in a blank band — a bubble outline — does return to blank
        # below the object, so it takes the sparse path instead.)
        return 0, ["gradient_top" if from_top else "gradient_bottom"]
    if dense_rel - stop > sparse_gap:
        # Sparse content (a bubble outline, stray glyphs) stopped the trim
        # well before the solid art begins: white remains inside the band.
        flags.append("sparse_below_top" if from_top else "sparse_above_bottom")
    cut = stop
    if cut > cap_px:
        cut = cap_px
        flags.append("capped_top" if from_top else "capped_bottom")
    return cut, flags


def compute_trim(img: Image.Image, *, cap_share: float = DEFAULT_CAP,
                 tol: int = GUTTER_TOL) -> TrimResult:
    """What v1 would trim from this panel image. Pure: changes nothing."""
    arr = np.asarray(img.convert("RGB"))
    h = arr.shape[0]
    if h == 0 or arr.shape[1] == 0:
        return TrimResult()
    g_top, g_bottom = detect_gutter_colors(arr)
    if g_top is None and g_bottom is None:
        return TrimResult()
    sh_top = gutter_shares(arr, g_top, tol) if g_top is not None else None
    sh_bottom = gutter_shares(arr, g_bottom, tol) if g_bottom is not None else None
    probe = sh_top if sh_top is not None else sh_bottom
    if (probe >= BLANK_SHARE).all():
        return TrimResult(0, 0, ["all_blank"])
    cap_px = int(cap_share * h)
    sparse_gap = max(6, int(0.01 * h))
    cut_top, flags_top = (_side(sh_top, True, cap_px, sparse_gap)
                          if sh_top is not None else (0, []))
    cut_bottom, flags_bottom = (_side(sh_bottom, False, cap_px, sparse_gap)
                                if sh_bottom is not None else (0, []))
    flags = flags_top + flags_bottom
    kept = h - cut_top - cut_bottom
    if kept < max(MIN_KEEP_PX, int(MIN_KEEP_SHARE * h)):
        return TrimResult(0, 0, sorted(set(flags + ["min_height"])))
    purity = 1.0
    if cut_top and sh_top is not None:
        purity = min(purity, float(sh_top[:cut_top].min()))
    if cut_bottom and sh_bottom is not None:
        purity = min(purity, float(sh_bottom[h - cut_bottom:].min()))
    return TrimResult(cut_top, cut_bottom, sorted(set(flags)), purity)


def apply_trim(img: Image.Image, res: TrimResult) -> Image.Image:
    """The trimmed copy. The original is never modified."""
    if not res.cut_top and not res.cut_bottom:
        return img
    return img.crop((0, res.cut_top, img.width, img.height - res.cut_bottom))


# ------------------------------------------------------------------ report


def trim_report(root: Path, chapter: str, *, cap_share: float = DEFAULT_CAP) -> Dict[str, Any]:
    """Read-only: what the trimmer would do to every panel of a chapter.

    Writes .work/video/chN/trim_report.json and returns it. Panel images and
    panels.json are never modified."""
    from ..util import UserError, require_chapter
    from ..workspace import work_root
    from .panels import load_panels, panel_image

    chapter = require_chapter(chapter)
    data = load_panels(root, chapter)
    if not data:
        raise UserError(f"No panels.json for chapter {chapter}; run video panels first.")
    rows = []
    for p in data["panels"]:
        img = panel_image(root, chapter, p, data["norm_width"])
        res = compute_trim(img, cap_share=cap_share)
        rows.append({
            "panel": p["id"],
            "height_px": img.height,
            "cut_top_px": res.cut_top,
            "cut_bottom_px": res.cut_bottom,
            "cut_top_frac": round(res.cut_top / max(1, img.height), 4),
            "cut_bottom_frac": round(res.cut_bottom / max(1, img.height), 4),
            "flags": res.flags,
            "removed_min_share": round(res.purity, 4),
        })
    # The art-body pass (v3, docs/WHITESPACE_AUTOCROP.md) is applied at panel
    # build and recorded per panel. Report BOTH: what the panel already
    # carries (its `body` record) and what a fresh computation would still
    # shave — so the report previews the pass on sessions built before it
    # and reads as a no-op on sessions it already cropped.
    from .bodytrim import compute_body

    body_rows = []
    for p in data["panels"]:
        b = p.get("body") or {}
        img = panel_image(root, chapter, p, data["norm_width"])
        fresh = compute_body(img)
        body_rows.append({
            "panel": p["id"],
            "recorded": bool(b.get("top") or b.get("bottom") or b.get("left") or b.get("right")),
            "recorded_cuts": {k: b.get(k, 0.0) for k in ("top", "bottom", "left", "right")},
            "recorded_flags": list(b.get("flags") or []),
            "fresh_cut_top": fresh.cut_top, "fresh_cut_bottom": fresh.cut_bottom,
            "fresh_cut_left": fresh.cut_left, "fresh_cut_right": fresh.cut_right,
            "fresh_flags": list(fresh.flags),
        })
    report = {
        "chapter": chapter,
        "cap_share": cap_share,
        "panels": len(rows),
        "trimmed": sum(1 for r in rows if r["cut_top_px"] or r["cut_bottom_px"]),
        "flagged": sum(1 for r in rows if r["flags"]),
        "rows": rows,
        "body": {
            "note": "art-body (border-box) pass (docs/WHITESPACE_AUTOCROP.md): 'recorded' = "
                    "applied at panel build (cuts in normalized units at norm_width); "
                    "'fresh' = what the pass would still shave from the current panel "
                    "image, in px",
            "panels": len(body_rows),
            "recorded_cropped": sum(1 for r in body_rows if r["recorded"]),
            "would_crop_now": sum(1 for r in body_rows
                                  if r["fresh_cut_top"] or r["fresh_cut_bottom"]
                                  or r["fresh_cut_left"] or r["fresh_cut_right"]),
            "flagged": sum(1 for r in body_rows if r["recorded_flags"] or r["fresh_flags"]),
            "rows": body_rows,
        },
    }
    out = work_root(root) / "video" / f"ch{chapter}" / "trim_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return report


# ------------------------------------------------------------------ v2: Rule 6
#
# Rule 6 of the user's rendering spec (2026-10-23): pre-processing white
# space eradication. v1 (above) trims blank ROWS against a detected LIGHT
# gutter colour, with per-side caps and review flags. v2 is the stricter,
# literal rule the panel-fit mode is built on, applied at panel build time
# (video-build prep: after the recap, before pairing/plan — raw page assets
# are never touched):
#
# - an asset is trimmed exactly to its ink bounding box: every outer row
#   or column that is (near-)pure white OR (near-)pure black is margin;
# - a strip holding several panels across a WHITE or BLACK gap is
#   sliced at the gap into separate panel assets (the gap is discarded).
#   The gap floor is TRIM2_GAP_MIN_PX (15px since B1e — the user's rule:
#   stacked panels are ALWAYS separate assets, sequenced with the
#   narration, never shown glued together). Black gaps slice too since
#   B1f — the user's rule is "white or black": a black separator over
#   the floor is an editorial panel cut, not artwork. (One UNDER the
#   floor — Solo ch1's ~4px detonator/explosion separator — stays a
#   landmark for a hand-written slice hint; see slicehints.py.)
#
# A row/column is blank only when (almost) every pixel is margin — any
# ink at all, a bubble outline included, stops the trim on that side,
# exactly the v1 caution. All-blank assets are left untouched and
# flagged, never emptied. Thresholds are named constants here (the
# hardcode rule); the white/black levels default to the same values the
# panel settings use (242 / 13) and panel build passes its settings in.

TRIM2_BLANK_SHARE = BLANK_SHARE  # share of a row/column that must be margin to count as blank
TRIM2_WHITE_MIN = 242            # a pixel is white margin when its DARKEST channel >= this
TRIM2_BLACK_MAX = 13             # ... or black margin when its BRIGHTEST channel <= this
TRIM2_GAP_MIN_PX = 15            # a white OR black gap at least this tall inside a strip slices it
                           # (B1e: the user's locked rule — any contiguous
                           # ~pure row run over 15px is a panel gutter. The
                           # old 200px floor left stacked panels glued
                           # together on screen whenever their gutter was
                           # thinner than 200px, which is most of them.)
TRIM2_MIN_KEEP_PX = MIN_KEEP_PX  # never trim an asset below this many px on an axis


@dataclass
class Trim2Result:
    cut_top: int = 0        # rows/cols, in image pixels
    cut_bottom: int = 0
    cut_left: int = 0
    cut_right: int = 0
    flags: List[str] = field(default_factory=list)
    purity: float = 1.0     # lowest margin-share among removed rows/cols (proof)


def _margin_shares(arr: np.ndarray, *, white_min: int, black_max: int) -> Tuple[np.ndarray, np.ndarray]:
    """(per-row, per-column) share of margin pixels in an RGB array."""
    margin = (arr.min(axis=2) >= white_min) | (arr.max(axis=2) <= black_max)
    return margin.mean(axis=1), margin.mean(axis=0)


def compute_trim_v2(img: Image.Image, *, white_min: int = TRIM2_WHITE_MIN,
                    black_max: int = TRIM2_BLACK_MAX) -> Trim2Result:
    """The ink bounding box of one asset, as cuts from each side. Pure."""
    arr = np.asarray(img.convert("RGB"))
    h, w = arr.shape[0], arr.shape[1]
    if h == 0 or w == 0:
        return Trim2Result()
    rows, cols = _margin_shares(arr, white_min=white_min, black_max=black_max)
    rb = rows >= TRIM2_BLANK_SHARE
    cb = cols >= TRIM2_BLANK_SHARE
    if rb.all() or cb.all():
        return Trim2Result(0, 0, 0, 0, ["all_blank"])
    cut_top = int(np.argmax(~rb))
    cut_bottom = int(np.argmax(~rb[::-1]))
    cut_left = int(np.argmax(~cb))
    cut_right = int(np.argmax(~cb[::-1]))
    kept_h = h - cut_top - cut_bottom
    kept_w = w - cut_left - cut_right
    if kept_h < max(TRIM2_MIN_KEEP_PX, int(MIN_KEEP_SHARE * h)) or kept_w < TRIM2_MIN_KEEP_PX:
        return Trim2Result(0, 0, 0, 0, ["min_keep"])
    purity = 1.0
    if cut_top:
        purity = min(purity, float(rows[:cut_top].min()))
    if cut_bottom:
        purity = min(purity, float(rows[h - cut_bottom:].min()))
    if cut_left:
        purity = min(purity, float(cols[:cut_left].min()))
    if cut_right:
        purity = min(purity, float(cols[w - cut_right:].min()))
    return Trim2Result(cut_top, cut_bottom, cut_left, cut_right, [], purity)


def apply_trim_v2(img: Image.Image, res: Trim2Result) -> Image.Image:
    """The bbox-trimmed copy. The original is never modified."""
    if not (res.cut_top or res.cut_bottom or res.cut_left or res.cut_right):
        return img
    return img.crop((res.cut_left, res.cut_top,
                     img.width - res.cut_right, img.height - res.cut_bottom))


def _true_runs(mask: np.ndarray) -> List[Tuple[int, int]]:
    """[start, end) runs of True in a 1-D mask."""
    out: List[Tuple[int, int]] = []
    j, n = 0, len(mask)
    while j < n:
        if mask[j]:
            k = j
            while k < n and mask[k]:
                k += 1
            out.append((j, k))
            j = k
        else:
            j += 1
    return out


def slice_gaps(img: Image.Image, *, gap_min_px: int = TRIM2_GAP_MIN_PX,
               white_min: int = TRIM2_WHITE_MIN,
               black_max: int = TRIM2_BLACK_MAX) -> List[Tuple[int, int]]:
    """(y0, y1) pieces of a strip after slicing at large WHITE or BLACK gaps.

    A gap is a run of >= gap_min_px rows that are (near-)pure white OR
    (near-)pure black across the whole strip — the user's rule is
    "white or black" (B1f supersedes the white-only reading of Rule 6
    point 3: a black separator over the floor is an editorial panel
    cut, not artwork). A row that mixes white
    and black pixels is NOT a gap row (neither share is near-pure), so a
    checkerboard/noise band never slices. Gaps touching an edge are
    margins, not slice points — the bbox trim owns those. Returns
    [(0, h)] when there is nothing to slice.
    """
    arr = np.asarray(img.convert("RGB"))
    h = arr.shape[0]
    if h == 0 or arr.shape[1] == 0:
        return []
    white = (arr.min(axis=2) >= white_min).mean(axis=1) >= TRIM2_BLANK_SHARE
    black = (arr.max(axis=2) <= black_max).mean(axis=1) >= TRIM2_BLANK_SHARE
    cuts = [(a, b) for a, b in _true_runs(white | black)
            if b - a >= gap_min_px and a > 0 and b < h]
    if not cuts:
        return [(0, h)]
    pieces: List[Tuple[int, int]] = []
    start = 0
    for a, b in cuts:
        pieces.append((start, a))
        start = b
    pieces.append((start, h))
    return [(a, b) for a, b in pieces if b - a > 0]
