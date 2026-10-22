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
    report = {
        "chapter": chapter,
        "cap_share": cap_share,
        "panels": len(rows),
        "trimmed": sum(1 for r in rows if r["cut_top_px"] or r["cut_bottom_px"]),
        "flagged": sum(1 for r in rows if r["flags"]),
        "rows": rows,
    }
    out = work_root(root) / "video" / f"ch{chapter}" / "trim_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return report
