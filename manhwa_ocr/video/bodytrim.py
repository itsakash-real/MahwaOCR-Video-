"""White-space auto-crop v3 — the art-body (border-box) pass.

The standing trimmers (panels.tighten_edges, trim v1/v2) shave rows and
columns that are (almost) pure white or pure black. They stop at the
first ink row — which is exactly the failure this module exists for: a
manhwa panel is a DRAWN BORDER BOX, and its SFX lettering and speech
bubbles overflow onto the page's white canvas OUTSIDE that box (measured
evidence: .work/qcframes/S001.png — white bands above and below the box
inside the rendered frame while panels.json recorded trim2 = 0.0 on all
123 panels). No row-based trimmer can remove that white: the overflow
ink stops it, and cutting the ink is never allowed.

The body pass finds the panel's drawn rectangular border and crops the
panel bounds to it. Overflow decorations that leave the box are cropped
away like any element crossing a frame edge; whatever the box contains
(a bubble's upper half where it overlaps the border — normal comic
clipping) is kept whole. panels.json geometry is adjusted once, at
detect time, so pairing, panelfit fit geometry, the blurred backdrop and
the renderer all follow from the single source of truth.

Detection (per axis, independent):

- BORDER path: a candidate border line is a band of consecutive rows
  whose dark-pixel share over the middle 90% of the width is >=
  LINE_SHARE, and whose thickness is at most LINE_THICK_FRAC of the
  panel (abs floor LINE_THICK_MIN). Dense art rows are too thick;
  strokes, bubble outlines and lettering are too sparse — neither
  qualifies. Top border = the topmost qualifying band, bottom = the
  bottommost; both must exist (a lone line is not a box). Columns
  likewise, sampled inside the y box when one was found (a side border
  only spans its box's rows).
- DENSE-SPAN fallback (unbordered webtoon panels — the common case):
  art rows carry continuous MIDDLE-TONES (>= DENSE_ROW_SHARE share of
  pixels that are neither canvas-white nor canvas-black); bubbles and
  SFX on canvas are bimodal — pure white around pure black glyphs — and
  solid black bands are pure black, so all of them classify as canvas
  no matter how much ink they hold. The body = first dense run's start
  to last dense run's end (middle dips never split the crop). A
  discarded outer zone is accepted only when EVERY row in it stays
  under ZONE_MIDTONE_MAX midtone: anything with real tonal structure
  (snow art, grass, pale scenes) refuses the cut on that side
  (`zone_not_canvas`) instead of risking amputation.

Guards — art is never destroyed (the user's rule 10):

- min_keep: the body must keep >= MIN_KEEP_SHARE of each axis (and at
  least MIN_KEEP_PX), else that axis is not cropped. The first box of a
  2/3-panel glued strip fails this, so strips are never amputated.
- multi_art (border path): a discarded outer region holding a run of
  >= DENSE_RUN_ROWS consecutive rows that are >= DENSE_ROW_SHARE content
  is solid artwork — another panel's art, not canvas — and blocks the
  crop on that axis. Bubbles, SFX and text never reach that row share.
- side cap: at most SIDE_CAP of the axis is shaved per side (clamped,
  flagged), mirroring the trim_v1/v2 caps.
- no_border: nothing found at all -> untouched. Borderless sparse art,
  snow panels and logo canvas are exactly as they were.

The result is pure (changes nothing); panels.detect applies the cuts and
records them per panel as `body`, same shape as `trim2`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import numpy as np
from PIL import Image

DARK_MAX = 140          # a pixel darker than this (grayscale) is border ink
LINE_SHARE = 0.75       # share of a row/column that must be border ink to qualify;
                        # well above textured art's dark share (~57% at the 60-200
                        # palette, and side borders diluted by stack gutters land
                        # ~84%), so art rows/columns never masquerade as borders
LINE_THICK_FRAC = 0.012 # a border band is at most this share of the axis long
LINE_THICK_MIN = 4      # ... and never thicker than this many px
WHITE_MIN = 242         # a pixel is canvas-white when its darkest channel >= this
BLACK_MAX = 13          # ... or canvas-black when its brightest channel <= this
                        # (mirrors panels' white/black thresholds). Canvas is
                        # BOTH: a solid black band below a bubble is gutter,
                        # exactly like the user's Rule 8 says.
DENSE_ROW_SHARE = 0.45  # a row/column this full of MIDDLE-TONE pixels is art;
                        # bubbles/SFX on canvas are pure white + pure black with
                        # nothing between, snow/grass are continuous midtones
ZONE_MIDTONE_MAX = 0.15 # a discarded dense-span zone must have every row under
                        # this midtone share: real tonal structure (art) refuses
                        # the cut on that side; pure white/black + sparse black
                        # glyph rows pass — bubble bands and black canvas crop
DENSE_RUN_ROWS = 8      # floor for a dense run's length (scaled with the axis)
MIN_KEEP_SHARE = 0.55   # the body must keep at least this share of each axis
MIN_KEEP_PX = 24        # ... and at least this many px (mirrors trim.py)
SIDE_CAP = 0.40         # max share of an axis shaved per side (mirrors trim caps)


@dataclass
class BodyResult:
    cut_top: int = 0        # px to shave, per side, at strip resolution
    cut_bottom: int = 0
    cut_left: int = 0
    cut_right: int = 0
    flags: List[str] = field(default_factory=list)


def _bands(dark_share: np.ndarray, axis_len: int) -> List[tuple[int, int]]:
    """Consecutive [start, end) runs of rows/columns at or above LINE_SHARE."""
    mask = dark_share >= LINE_SHARE
    if not mask.any():
        return []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2])]


def _outer_bands(bands: List[tuple[int, int]], axis_len: int) -> tuple[int, int] | None:
    """(top band start, bottom band end) of the qualifying thin border bands,
    or None when fewer than two exist. A band qualifies when its thickness
    is at most the border-line limit — dense art runs are too thick."""
    thick_max = max(LINE_THICK_MIN, int(round(LINE_THICK_FRAC * axis_len)))
    thin = [(a, b) for a, b in bands if b - a <= thick_max]
    if len(thin) < 2:
        return None
    return thin[0][0], thin[-1][1]


def _dense_art_run(content_share: np.ndarray, lo: int, hi: int) -> bool:
    """True when rows [lo, hi) contain a run of >= DENSE_RUN_ROWS rows whose
    content share >= DENSE_ROW_SHARE — solid artwork in a would-be discarded
    region (another panel's art, never canvas)."""
    if hi - lo < DENSE_RUN_ROWS:
        return False
    dense = content_share[lo:hi] >= DENSE_ROW_SHARE
    run = 0
    for flag in dense:
        run = run + 1 if flag else 0
        if run >= DENSE_RUN_ROWS:
            return True
    return False


def _long_runs(mask: np.ndarray, min_len: int) -> List[tuple[int, int]]:
    """[start, end) runs of True at least `min_len` long."""
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2])
            if b - a >= min_len]


def _midtone(arr: np.ndarray) -> np.ndarray:
    """Boolean mask of MIDDLE-TONE pixels: neither canvas-white nor
    canvas-black. Art carries continuous midtones; bubbles, SFX strokes
    and solid canvas bands are bimodal (pure white around pure black)."""
    rgb = np.asarray(arr.convert("RGB"), dtype=np.uint8)
    return (rgb.min(axis=2) < WHITE_MIN) & (rgb.max(axis=2) > BLACK_MAX)


def _side_cuts(arr: np.ndarray, *, vertical: bool) -> tuple[int, int, List[str]]:
    """(cut_open, cut_close, flags) for one axis. vertical=True works on rows
    (top/bottom), False on columns (left/right). Border path first; the
    dense-art-span fallback covers unbordered panels. All row/column
    classification is on the MIDTONE share — the bimodal canvas rows
    (white fill + black glyph, solid black band) classify as canvas even
    though they are full of ink."""
    g = np.asarray(arr.convert("L"), dtype=np.uint8)
    tone = _midtone(arr)
    if not vertical:
        g, tone = g.T, tone.T
    h, w = g.shape
    if h < 2 * MIN_KEEP_PX or w < 8:
        return 0, 0, []
    mid = g[:, w // 20: w - w // 20] if w >= 20 else g
    share = (mid < DARK_MAX).mean(axis=1)
    tone_share = tone.mean(axis=1)
    flags: List[str] = []
    cut_open = cut_close = 0
    outer = _outer_bands(_bands(share, h), h)
    if outer is not None:
        top, bottom = outer
        cut_open, cut_close = top, h - bottom
        if _dense_art_run(tone_share, 0, cut_open) or \
                _dense_art_run(tone_share, h - cut_close, h):
            flags.append("multi_art")
            cut_open = cut_close = 0
    else:
        flags.append("no_border")
    if not cut_open and not cut_close:
        # Dense-art-span fallback (unbordered panels): the body is the span
        # from the first to the last dense art run; each discarded zone must
        # be canvas on EVERY row (midtone under the zone floor — bubble text
        # and solid black bands pass, snow/grass/scene art refuses), else
        # that side alone is refused.
        runs = _long_runs(tone_share >= DENSE_ROW_SHARE,
                          max(DENSE_RUN_ROWS, int(round(0.004 * h))))
        if runs:
            cut_open, cut_close = runs[0][0], h - runs[-1][1]
            if (tone_share[:cut_open] > ZONE_MIDTONE_MAX).any():
                flags.append("zone_not_canvas")
                cut_open = 0
            if (tone_share[h - cut_close:] > ZONE_MIDTONE_MAX).any():
                flags.append("zone_not_canvas")
                cut_close = 0
            if cut_open or cut_close:
                flags.append("dense_span")
    keep = h - cut_open - cut_close
    if keep < max(MIN_KEEP_PX, int(MIN_KEEP_SHARE * h)):
        return 0, 0, flags + ["min_keep"]
    cap = int(SIDE_CAP * h)
    if cut_open > cap:
        cut_open = cap
        flags.append("capped_top" if vertical else "capped_left")
    if cut_close > cap:
        cut_close = cap
        flags.append("capped_bottom" if vertical else "capped_right")
    return cut_open, cut_close, flags


def compute_body(img: Image.Image) -> BodyResult:
    """The art-body crop of one panel image, as cuts from each side. Pure.

    Flags are axis-tagged where the bare name would be ambiguous: the
    y-axis keeps trim.py's vocabulary (`no_border`, `min_keep`,
    `multi_art`, `capped_top/bottom`); the x-axis suffixes `_x`
    (`no_border_x`, ...) so a record reader can tell which axis a
    fallback belongs to. The x pass samples inside the y box when one
    was found — a side border only spans its box's rows, so measuring
    it over the whole panel dilutes it with canvas."""
    if img.width == 0 or img.height == 0:
        return BodyResult()
    cut_top, cut_bottom, flags_y = _side_cuts(img, vertical=True)
    sub = img
    if cut_top or cut_bottom:
        sub = img.crop((0, cut_top, img.width, img.height - cut_bottom))
    cut_left, cut_right, flags_x = _side_cuts(sub, vertical=False)
    renamed = [{"no_border": "no_border_x", "min_keep": "min_keep_x",
                "multi_art": "multi_art_x", "dense_span": "dense_span_x",
                "zone_not_canvas": "zone_not_canvas_x"}.get(f, f) for f in flags_x]
    return BodyResult(cut_top, cut_bottom, cut_left, cut_right,
                      sorted(set(flags_y + renamed)))
