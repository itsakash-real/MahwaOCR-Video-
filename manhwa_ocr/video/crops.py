"""Focal crops: show the part of a panel that matters, not the whole thing.

Recap channels rarely show a full tall webtoon panel. They show the face, the
hand on the sword, the explosion. This finds those regions with a cheap,
deterministic heuristic:

  + visual detail (edges): line art, faces and action are busy
  + skin-like colour: faces and hands are usually the focal point
  - near-white area: speech bubbles and empty background

and slides a window of the wanted aspect ratio over the panel to find the
best-scoring placements. Several non-overlapping windows are returned, so a
panel shown twice can show two different parts of itself.

The result is just a default: crops are stored in the plan, where the agent
(or you) can adjust them after looking at the storyboard.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
from PIL import Image

Box = Tuple[float, float, float, float]  # x0, y0, x1, y1 as fractions of the panel
ANALYSIS_W = 160


def _gradients(gray: np.ndarray) -> np.ndarray:
    """Central-difference gradient magnitude (same size as the input)."""
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = np.abs(gray[:, 2:] - gray[:, :-2])
    gy[1:-1, :] = np.abs(gray[2:, :] - gray[:-2, :])
    return gx + gy


def _art_maps(rgb: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(soft texture, hard strokes, flat white) masks.

    Drawn art is mostly soft texture: shading, hair, skin, backgrounds. Lettering
    is hard black-on-white (or white-on-colour) strokes, and bubbles and empty
    pages are flat white. Counting only soft texture as "detail" stops crops from
    centring on captions and stops big lettering from passing as art.
    """
    gray = rgb.mean(axis=2)
    grad = _gradients(gray)
    soft = ((grad > 6) & (grad < 60)).astype(np.float32)
    hard = (grad >= 60).astype(np.float32)
    flat_white = ((gray > 225) & (grad < 4)).astype(np.float32)
    return soft, hard, flat_white


def _small(img: Image.Image, width: int, resample=Image.BICUBIC) -> np.ndarray:
    h = max(8, round(img.height * width / max(1, img.width)))
    return np.asarray(img.convert("RGB").resize((width, h), resample), dtype=np.float32)


def _maps(img: Image.Image) -> Tuple[np.ndarray, int, int]:
    rgb = _small(img, ANALYSIS_W, Image.BILINEAR)
    sh, sw = rgb.shape[:2]
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    soft, hard, _ = _art_maps(rgb)
    white = (rgb.min(axis=2) > 228).astype(np.float32)
    skin = ((r > 95) & (g > 40) & (b > 20) & (r > g) & (r > b) & (r - b > 15) & (r - g > 6)
            & (r < 252)).astype(np.float32)
    score = soft * (1 - white) - 0.9 * hard + 1.2 * skin * (1 - white) - 1.0 * white
    return score, sw, sh


def _window_sums(score: np.ndarray, ww: int, wh: int, step: int) -> List[Tuple[float, int, int]]:
    ii = np.pad(score.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    h, w = score.shape
    out = []
    for y in range(0, h - wh + 1, step):
        for x in range(0, w - ww + 1, step):
            s = ii[y + wh, x + ww] - ii[y, x + ww] - ii[y + wh, x] + ii[y, x]
            out.append((float(s), x, y))
    return out


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / area if area > 0 else 0.0


def text_bands(img: Image.Image, limit: float = 0.45) -> Tuple[float, float]:
    """(top, bottom) of the panel once caption boxes at its top or bottom edge
    are cut off, as fractions of its height. (0, 1) when there are none.

    A caption row is mostly flat white box or hard lettering and has little
    soft texture. Bands are only taken from the edges, where narration boxes
    sit; a white sky in the middle of the art is never touched.
    """
    rgb = _small(img, 120)
    soft, hard, flat_white = _art_maps(rgb)
    texty = np.clip(flat_white + hard, 0, 1).mean(axis=1)
    art = soft.mean(axis=1)
    row = (texty > 0.45) & (texty > 2.0 * art)
    row = np.convolve(row.astype(np.float32), np.ones(3) / 3, mode="same") > 0.5   # ignore 1-row blips
    n = len(row)
    margin, cap = max(1, int(0.06 * n)), int(limit * n)

    def run(seq) -> int:
        start = next((i for i in range(min(margin, len(seq))) if seq[i]), None)
        if start is None:
            return 0
        end = start
        while end < min(cap, len(seq)) and seq[end]:
            end += 1
        return end if end - start >= max(3, int(0.06 * n)) else 0

    top, bottom = run(row), run(row[::-1])
    if (n - top - bottom) < 0.35 * n:        # little art left: keep the panel as it is
        return 0.0, 1.0
    return top / n, 1 - bottom / n


def focal_crops(img: Image.Image, aspect: float, k: int = 3, zoom: float = 1.0,
                trim_text: bool = True) -> List[Box]:
    """Up to k crop boxes of width/height `aspect`, best first.

    zoom < 1 asks for a tighter crop (e.g. 0.75 = a window 75% the size of the
    largest one that fits), useful for picking out a detail. Caption boxes on
    the panel's top or bottom edge are left out of every crop.
    """
    if trim_text:
        top, bottom = text_bands(img)
        if top > 0.02 or bottom < 0.98:
            W, H = img.size
            inner = img.crop((0, int(top * H), W, max(int(top * H) + 2, int(bottom * H))))
            span = bottom - top
            return [(round(x0, 4), round(top + y0 * span, 4), round(x1, 4), round(top + y1 * span, 4))
                    for x0, y0, x1, y1 in focal_crops(inner, aspect, k, zoom, trim_text=False)]
    pw, ph = img.size
    panel_aspect = pw / ph
    # Never cut the sides: characters stand across the full width. A panel at
    # least about as wide as the card is shown whole (the card takes its shape);
    # a taller one is cropped at the top and bottom only, to its key part.
    if panel_aspect >= aspect * 0.85:
        return [(0.0, 0.0, 1.0, 1.0)]

    score, sw, sh = _maps(img)
    ww, wh = sw, sw / aspect           # full width, slide vertically
    ww, wh = max(4, int(ww * zoom)), max(4, int(wh * zoom))
    ww, wh = min(ww, sw), min(wh, sh)
    step = max(1, min(ww, wh) // 24)

    ranked = sorted(_window_sums(score, ww, wh, step), reverse=True)
    picked: List[Box] = []
    for s, x, y in ranked:
        box = (x / sw, y / sh, (x + ww) / sw, (y + wh) / sh)
        if all(_iou(box, p) < 0.35 for p in picked):
            picked.append(tuple(round(v, 4) for v in box))
        if len(picked) >= k:
            break
    return picked or [(0.0, 0.0, 1.0, 1.0)]


def _skin(rgb: np.ndarray) -> np.ndarray:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return ((r > 95) & (g > 40) & (b > 20) & (r > g) & (r > b) & (r - b > 15) & (r - g > 6)
            & (r < 252)).astype(np.float32)


def _box_maps(img: Image.Image, box: Box, with_skin: bool = False):
    rgb = _small(img, 120)
    h, w = rgb.shape[:2]
    soft, hard, flat_white = _art_maps(rgb)
    x0, y0, x1, y1 = box
    r = (slice(int(y0 * h), max(int(y0 * h) + 1, int(y1 * h))), slice(int(x0 * w), max(int(x0 * w) + 1, int(x1 * w))))
    out = (soft[r], np.clip(hard[r] + flat_white[r], 0, 1), h)
    return out + (float(_skin(rgb)[r].sum() / max(1.0, _skin(rgb).sum())),) if with_skin else out


def text_share(img: Image.Image, box: Box) -> float:
    """How much of `box` is lettering or flat white (bubbles, caption boxes), 0..1."""
    return float(_box_maps(img, box)[1].mean())


def _shave(img: Image.Image, box: Box, most: float = 0.2) -> Box:
    """Trim thin slivers of caption box or bubble off the crop's top and bottom."""
    _, texty, h = _box_maps(img, box)
    rows = texty.mean(axis=1)
    n = len(rows)
    limit = max(1, int(most * n))
    top = 0
    while top < limit and rows[top] > 0.33:
        top += 1
    bottom = 0
    while bottom < limit and rows[n - 1 - bottom] > 0.33:
        bottom += 1
    if not top and not bottom:
        return box
    x0, y0, x1, y1 = box
    span = y1 - y0
    # one extra row past the sliver, so no hairline of it survives
    t = (top + (1 if top else 0)) / n * span
    b = (bottom + (1 if bottom else 0)) / n * span
    return (x0, round(y0 + t, 4), x1, round(y1 - b, 4))


def clean_crop(img: Image.Image, aspects: List[float]) -> Box:
    """The crop, over a few allowed card shapes, that shows the most art and the
    least lettering.

    A taller window shows more of the panel but is likelier to take in a
    speech bubble or caption; a shorter one can sit between them. Empty dark or
    flat areas score low too, so the crop never lands on nothing.
    """
    best = None
    for aspect in aspects:
        for box in focal_crops(img, aspect, k=3):
            soft, texty, _, faces = _box_maps(img, box, with_skin=True)
            area = (box[2] - box[0]) * (box[3] - box[1])
            # faces = share of the panel's skin (faces, hands) inside the crop:
            # characters matter more than avoiding a small sound effect
            score = 2.0 * float(texty.mean()) - 0.6 * float(soft.mean()) - 0.5 * faces - 0.04 * area
            if best is None or score < best[0]:
                best = (score, box)
    return _shave(img, best[1]) if best else (0.0, 0.0, 1.0, 1.0)


TEXT_ONLY_BELOW = 0.25


def art_score(img: Image.Image) -> float:
    """How much of the whole panel is drawn art, 0..1 (mid-tones with texture)."""
    a = _small(img, 120)
    g = a.mean(axis=2)
    sat = a.max(axis=2) - a.min(axis=2)
    mid = ((g > 45) & (g < 215)) | ((sat > 40) & (g < 235))
    grad = np.zeros_like(g)
    grad[1:-1, 1:-1] = np.abs(g[2:, 1:-1] - g[:-2, 1:-1]) + np.abs(g[1:-1, 2:] - g[1:-1, :-2])
    return round(float((mid & (grad > 6)).mean()), 3)


def crop_art(img: Image.Image, aspect: float = 0.75, area: float = 0.6) -> float:
    """Soft-texture share of the panel's most art-like window, 0..1.

    Judging the best window instead of the whole panel keeps an art panel with
    a big speech bubble beside it (the art is still worth showing) and rejects
    big lettering on a coloured background (no window of it is art).
    """
    rgb = _small(img, 80)
    h, w = rgb.shape[:2]
    soft, hard, flat_white = _art_maps(rgb)
    score = soft - 0.6 * hard - 0.4 * flat_white
    if w / h > aspect:
        wh, ww = h, h * aspect
    else:
        ww, wh = w, w / aspect
    k = area ** 0.5
    ww, wh = max(4, min(w, int(ww * k))), max(4, min(h, int(wh * k)))
    _, x, y = max(_window_sums(score, ww, wh, 2))
    # Measure the chosen window again at a finer scale: lettering blurs into
    # "soft texture" when shrunk too far.
    W, H = img.size
    window = img.crop((int(x / w * W), int(y / h * H), max(int(x / w * W) + 2, int((x + ww) / w * W)),
                       max(int(y / h * H) + 2, int((y + wh) / h * H))))
    fine_soft, _, _ = _art_maps(_small(window, 120))
    return round(float(fine_soft.mean()), 3)


CROP_ART_BELOW = 0.13        # no art anywhere in the panel
CROP_ART_WEAK = 0.2          # a little art, but the panel is mostly lettering


def is_text_only(art: float, crop: float) -> bool:
    """Speech bubbles, captions, sound effects, big lettering, blank pages.

    Measured on real chapters: lettering/bubble panels have crop_art <= 0.17
    (and a whole-panel art_score <= 0.25 when above 0.13); art panels with a
    bubble beside them score 0.14+ with more art overall.
    """
    return crop < CROP_ART_BELOW or (crop < CROP_ART_WEAK and art <= TEXT_ONLY_BELOW)


# Scanlation credit and promo pages. They sit on a chapter's first or last
# pages; checked only there, because OCR text is shared by neighbouring panels
# and a site watermark inside real art must not count.
CREDIT_WORDS = ("read at", "fastest release", "discord", "patreon", "ko-fi", "kofi", "translator",
                "translation:", "proofread", "typeset", "redraw", "cleaner", "raw provider",
                "join our", "support us", "scanlat", "quality checker", "editor:")


def looks_like_credits(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in CREDIT_WORDS)


def tone(img: Image.Image) -> str:
    """'white', 'dark' or 'mid', from the panel's outer border.

    Picks a background that suits the panel: white-background panels sit well
    on white, dark ones on black.
    """
    small = np.asarray(img.convert("L").resize((64, max(8, round(64 * img.height / img.width)))), dtype=np.float32)
    border = np.concatenate([small[0], small[-1], small[:, 0], small[:, -1]])
    m = float(border.mean())
    if m > 225:
        return "white"
    if m < 40:
        return "dark"
    return "mid"


# ------------------------------------------------------------------ shape-locked crops
#
# Every card is cut to one exact shape (w/h in pixels), chosen from a short
# list per layout. The window is slid over the panel and scored. Three things
# are hard constraints:
#   - no speech bubble or caption box inside the card (bubble_boxes)
#   - a detected head is kept whole: its top and chin stay inside the card
#   - the card is the exact shape (checked by plan.quality_report)
# A window of the target shape always exists, so nothing is dropped for lack of
# a crop; when even the best window breaks a constraint, that is reported.

HEAD_MARGIN = 0.04       # fraction of the panel's height kept above and below a head
FIGURE_MIN = 0.004       # skin share above this means a character is drawn here
BUBBLE_OK = 0.02         # share of a card a speech bubble may take before it counts as shown


def edge_rows(gray: np.ndarray, white: int = 242, black: int = 13, share: float = 0.985) -> Tuple[float, float]:
    """(top, bottom) fractions of a grayscale panel that are all-white or all-black rows at
    its edges: gutter the card should not show. (0, 0) for a clean panel."""
    if gray.size == 0:
        return 0.0, 0.0
    margin = ((gray > white).mean(axis=1) >= share) | ((gray < black).mean(axis=1) >= share)
    n = len(margin)
    top = 0
    while top < n and margin[top]:
        top += 1
    if top >= n:
        return 0.0, 0.0
    bottom = 0
    while bottom < n - top and margin[n - 1 - bottom]:
        bottom += 1
    return top / n, bottom / n


def _panel_maps(img: Image.Image, options: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Analysis maps for one panel, computed once and sliced per candidate.

    options: {"full_width": bool, "min_width_kept": float, "white_threshold": int,
    "black_threshold": int}, normally video.crop and video.panels from the config."""
    opts = options or {}
    rgb = _small(img, 120)
    soft, hard, flat_white = _art_maps(rgb)
    gray = rgb.mean(axis=2)
    m: Dict[str, Any] = {"soft": soft, "texty": np.clip(hard + flat_white, 0, 1), "skin": _skin(rgb),
                         "blank": flat_white, "gray": gray, "gray_hi": _small(img, 240).mean(axis=2)}
    m["sat"] = (rgb.max(axis=2) - rgb.min(axis=2)) / 255.0
    m["pale_heads"] = bool(opts.get("pale_heads", True))
    m["keep_top"] = float(opts.get("keep_top", KEEP_TOP))
    m["full_width"] = bool(opts.get("full_width", True))
    m["min_width_kept"] = float(opts.get("min_width_kept", MIN_WIDTH_KEPT))
    m["edge_rows"] = edge_rows(m["gray_hi"], int(opts.get("white_threshold", 242)), int(opts.get("black_threshold", 13)))
    m["bubble_regions"] = bubble_regions(m)
    m["bubbles"] = [r["box"] for r in m["bubble_regions"]]
    # Bubble painting was removed entirely (clean_bubbles() is a no-op),
    # so this is always empty. Kept so the scoring shape stays stable.
    m["painted"] = list(getattr(img, "painted_boxes", []))
    return m


def _label(fg: np.ndarray) -> Tuple[np.ndarray, int]:
    """Label 4-connected regions of a boolean grid: (labels, count), 0 = background."""
    try:
        import cv2                      # compiled labelling, if OpenCV happens to be installed

        n, lab = cv2.connectedComponents(fg.astype(np.uint8), connectivity=4)
        return lab.astype(np.int32), int(n) - 1
    except ImportError:
        pass
    # Pure numpy fallback: every cell takes the smallest label among its neighbours
    # until nothing changes. Correct everywhere, but slow on large white areas.
    h, w = fg.shape
    big = np.iinfo(np.int64).max
    lab = np.where(fg, np.arange(h * w, dtype=np.int64).reshape(h, w) + 1, big)
    while True:
        m = lab.copy()
        np.minimum(m[1:, :], lab[:-1, :], out=m[1:, :])
        np.minimum(m[:-1, :], lab[1:, :], out=m[:-1, :])
        np.minimum(m[:, 1:], lab[:, :-1], out=m[:, 1:])
        np.minimum(m[:, :-1], lab[:, 1:], out=m[:, :-1])
        m = np.where(fg, m, big)
        if np.array_equal(m, lab):
            break
        lab = m
    values = np.unique(lab[fg])
    remap = np.zeros(h * w + 2, dtype=np.int32)
    remap[values % (h * w + 2)] = np.arange(1, len(values) + 1, dtype=np.int32)
    out = np.zeros((h, w), dtype=np.int32)
    out[fg] = remap[lab[fg] % (h * w + 2)]
    return out, len(values)


def _components(mask: np.ndarray) -> List[Tuple[int, int, int, int, int, np.ndarray]]:
    """4-connected regions of a boolean grid: (y0, y1, x0, x1, area, pixels), ends
    exclusive; `pixels` is the region's own mask inside its box."""
    fg = np.asarray(mask, dtype=bool)
    if not fg.any():
        return []
    lab, count = _label(fg)
    ys, xs = np.nonzero(lab)
    ids = lab[ys, xs]
    y_min = np.full(count + 1, 1 << 30, np.int64); y_max = np.full(count + 1, -1, np.int64)
    x_min = np.full(count + 1, 1 << 30, np.int64); x_max = np.full(count + 1, -1, np.int64)
    np.minimum.at(y_min, ids, ys); np.maximum.at(y_max, ids, ys)
    np.minimum.at(x_min, ids, xs); np.maximum.at(x_max, ids, xs)
    area = np.bincount(ids, minlength=count + 1)
    out = []
    for k in range(1, count + 1):
        if area[k] == 0:
            continue
        y0, y1, x0, x1 = int(y_min[k]), int(y_max[k]) + 1, int(x_min[k]), int(x_max[k]) + 1
        out.append((y0, y1, x0, x1, int(area[k]), lab[y0:y1, x0:x1] == k))
    return out


def bubble_boxes(m: Dict[str, Any]) -> List[Box]:
    """Speech bubbles and caption boxes, as panel fractions (see bubble_regions)."""
    return [r["box"] for r in bubble_regions(m)]


def bubble_regions(m: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Speech bubbles and caption boxes: {"box": panel fractions, "fill": how much
    of its box the white region fills}.

    A bubble is a compact, pure-white region with three marks that pale art
    (sky, walls, speed lines) lacks:
      - dark lettering in at least two separate lines of text,
      - a dark outline around most of its edge,
      - a bounded size: at least 20% of the panel wide and 5% tall, at most 35% of it,
      - lettering in its middle, not just at its edges (art with outlines fails this).
    Measured on a finer grid than the crop search, so thin outlines survive.
    """
    gray = m["gray_hi"]
    h, w = gray.shape
    white = gray > 235
    dark = gray < 90
    out: List[Dict[str, Any]] = []
    for y0, y1, x0, x1, area, pix in _components(white):
        bh, bw = y1 - y0, x1 - x0
        if bw < 0.2 * w or bh < 0.05 * h or area > 0.35 * h * w or area / float(bh * bw) < 0.38:
            continue
        block = dark[y0:y1, x0:x1] & ~pix            # lettering: dark pixels inside the region's box
        if not 0.005 <= float(block.mean()) <= 0.35:
            continue
        rows = block.mean(axis=1) > 0.02
        bands = int(np.sum(rows[1:] & ~rows[:-1]) + (1 if rows[0] else 0))
        if bands < 2:
            continue
        # outline: the ring just outside the region must be mostly dark
        pad = np.pad(pix, 4)
        ring = np.zeros_like(pad)
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                ring |= np.roll(np.roll(pad, dy, axis=0), dx, axis=1)
        ring &= ~pad
        gy0, gx0 = max(0, y0 - 4), max(0, x0 - 4)
        sub = gray[gy0:min(h, y1 + 4), gx0:min(w, x1 + 4)]
        ry0, rx0 = gy0 - (y0 - 4), gx0 - (x0 - 4)
        ring = ring[ry0:ry0 + sub.shape[0], rx0:rx0 + sub.shape[1]]
        outline = float((sub[ring] < 170).mean()) if ring.any() else 0.0
        # A bubble cut by the panel's edge has no outline there, so a region with
        # a lot of text (four or more lines) needs less of it.
        if outline < (0.25 if bands >= 4 else 0.4):
            continue
        # Lettering sits in the middle of a bubble. Robe folds, door frames and
        # speed lines put their dark pixels at the edges instead: reject those.
        yy, xx = np.mgrid[0:bh, 0:bw]
        radius = ((yy - (bh - 1) / 2) / (bh / 2)) ** 2 + ((xx - (bw - 1) / 2) / (bw / 2)) ** 2
        dk = dark[y0:y1, x0:x1]
        centre = float(dk[radius <= 0.36].mean()) if (radius <= 0.36).any() else 0.0
        edge_ring = (radius <= 1.0) & (radius > 0.36)
        edge = float(dk[edge_ring].mean()) if edge_ring.any() else 0.0
        if centre < 0.035 or centre < 0.6 * edge:
            continue
        out.append({"box": (max(0, x0 - 2) / w, max(0, y0 - 2) / h, min(w, x1 + 2) / w, min(h, y1 + 2) / h),
                    "fill": area / float(bh * bw)})
    return out


def _overlap_share(box: Box, other: Box) -> float:
    """Share of `box`'s area that lies inside `other`."""
    ix = max(0.0, min(box[2], other[2]) - max(box[0], other[0]))
    iy = max(0.0, min(box[3], other[3]) - max(box[1], other[1]))
    area = (box[2] - box[0]) * (box[3] - box[1])
    return (ix * iy) / area if area > 0 else 0.0


def face_top(maps: Dict[str, Any]) -> float | None:
    """Fraction of the panel's height where the topmost character's skin starts,
    searched in the upper 55% (where heads are). None if no figure is found.

    Heuristic: three rows in a row that are at least 3% skin. Pale or heavily
    stylised art can miss; the QA report counts those panels as head_risk."""
    rows = maps["skin"].mean(axis=1)
    h = len(rows)
    run = 0
    for y in range(int(0.55 * h)):
        run = run + 1 if rows[y] >= 0.03 else 0
        if run >= 3:
            return max(0.0, (y - 2) / h)
    return None


FIGURE_CLEAR = 0.01      # skin share above this: a character is clearly in the panel
KEEP_TOP = 0.25          # no head found but a figure is: a window never starts this far down the panel
PALE_LEVELS = (170, 140, 110)   # brightness floors tried for a pale/gray head blob
PALE_CONTRAST = 35
MOSTLY_WHITE = 0.5     # a panel more than this white has no pale head
LETTER_DARK = 80       # gray level of lettering strokes
LETTER_SHARE = 0.05    # share of a pale blob's middle that is lettering before it is called a bubble       # a head blob must differ from its surroundings by at least this much gray


def _ring(pix: np.ndarray, r: int = 2) -> np.ndarray:
    """The pixels within r of a region but not in it (padded by r, so the ring fits)."""
    pad = np.pad(pix, r)
    out = np.zeros_like(pad)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= np.roll(np.roll(pad, dy, axis=0), dx, axis=1)
    return out & ~pad


def pale_head(maps: Dict[str, Any]) -> Dict[str, float] | None:
    """A head for characters the skin-colour test cannot see: white or silver hair, gray or
    very pale skin. Deterministic and deliberately modest (the result is flagged
    low-confidence):

      - find compact blobs that are bright or mid-gray *and* nearly colourless,
        at a few brightness floors;
      - keep blobs that stand clearly apart from their surroundings (a gray-level step
        around the whole blob), are not a wall or sky (not spanning the panel, not too
        big) and are not a speech bubble;
      - the head is the topmost such blob, in the upper 65% of the panel (position
        prior); its extent is estimated from the blob's width near its top.

    Returns {"top", "bottom"} as panel fractions, or None."""
    gray, sat = maps["gray"], maps["sat"]
    h, w = gray.shape
    if h < 12 or w < 12:
        return None
    if float((gray > 225).mean()) > MOSTLY_WHITE:
        return None                                    # a mostly white panel is lettering or a blank page, not a figure
    bubbles = list(maps.get("bubbles") or [])
    best = None
    for level in PALE_LEVELS:
        mask = (sat < 0.14) & (gray > level)
        for y0, y1, x0, x1, area, pix in _components(mask):
            bh, bw = y1 - y0, x1 - x0
            frac = area / float(h * w)
            if not 0.012 <= frac <= 0.45 or bh < 0.05 * h or bw < 0.05 * w:
                continue
            if bw > 0.85 * w or (x0 <= 0 and x1 >= w) or y0 > 0.65 * h:
                continue                               # a wall, a sky, or too low to be a head
            if area / float(bh * bw) < 0.25:
                continue                               # thin lines, not a body of colour
            box = (x0 / w, y0 / h, x1 / w, y1 / h)
            if any(_overlap_share(box, b) >= 0.5 for b in bubbles):
                continue                               # lettering's white, not hair
            my, mx = max(1, int(0.15 * bh)), max(1, int(0.15 * bw))
            inner = gray[y0 + my:y1 - my, x0 + mx:x1 - mx]
            if inner.size and float((inner < LETTER_DARK).mean()) > LETTER_SHARE and float(np.median(gray[y0:y1, x0:x1])) > 200:
                continue                               # a white shape with writing inside: a speech bubble, not hair
            ring = _ring(pix, 2)
            # Surroundings only where the picture really is (the ring may fall off an edge).
            gy0, gx0 = max(0, y0 - 2), max(0, x0 - 2)
            frame = gray[gy0:min(h, y1 + 2), gx0:min(w, x1 + 2)]
            ry0, rx0 = gy0 - (y0 - 2), gx0 - (x0 - 2)
            ring = ring[ry0:ry0 + frame.shape[0], rx0:rx0 + frame.shape[1]]
            if ring.sum() < 8:
                continue
            inside = float(np.median(gray[y0:y1, x0:x1][pix]))
            around = float(np.median(frame[ring]))
            if abs(inside - around) < PALE_CONTRAST:
                continue
            top_rows = pix[:max(2, int(0.3 * bh))]
            head_w = int(top_rows.sum(axis=1).max())
            head_h = min(bh, 1.15 * max(head_w, 1))      # hair and face are about as tall as wide
            cand = (y0, -level, max(0.0, y0 / h - 0.01), min(1.0, (y0 + head_h) / h + 0.01))
            if best is None or cand[:2] < best[:2]:
                best = cand
    if best is None:
        return None
    return {"top": best[2], "bottom": best[3]}


def head_of(maps: Dict[str, Any]) -> Dict[str, Any]:
    """Where the head is, and how sure we are. {"top", "bottom", "source", "confident",
    "figure", "keep_top"}.

    source "fixes" (a reviewer's face entry) and "skin" (the skin-colour test) are confident.
    source "pale" (white/silver/gray heads, see pale_head) is a guess: used to keep the head
    in the card but listed for a reviewer. With no head found and a clear figure
    (skin share >= FIGURE_CLEAR), keep_top is set: a window must not start further down
    than maps["keep_top"] of the panel. Switch off with video.heads (pale: false, keep_top: 0).
    """
    if maps.get("face_override"):
        fe, fb = maps["face_override"]
        return {"top": fe, "bottom": fb, "source": "fixes", "confident": True, "figure": True, "keep_top": None}
    if "_head" in maps:
        return maps["_head"]
    skin_share = float(maps["skin"].mean())
    fe = face_top(maps)
    if fe is not None:
        info = {"top": fe, "bottom": face_bottom(maps, fe), "source": "skin", "confident": True,
                "figure": True, "keep_top": None}
    else:
        pale = pale_head(maps) if maps.get("pale_heads", True) else None
        if pale:
            info = {"top": pale["top"], "bottom": pale["bottom"], "source": "pale", "confident": False,
                    "figure": True, "keep_top": None}
        else:
            keep = float(maps.get("keep_top", KEEP_TOP))
            clear = skin_share >= FIGURE_CLEAR and keep > 0
            info = {"top": None, "bottom": None, "source": None, "confident": False,
                    "figure": skin_share >= FIGURE_MIN, "keep_top": keep if clear else None}
    maps["_head"] = info
    return info


def face_bottom(maps: Dict[str, Any], top: float) -> float:
    """Fraction of the panel's height where the skin from `top` ends (the chin),
    allowing gaps of up to two rows."""
    rows = maps["skin"].mean(axis=1)
    h = len(rows)
    last, gap = int(top * h) + 2, 0
    for k in range(last, int(0.9 * h)):
        if rows[k] >= 0.02:
            last, gap = k, 0
        else:
            gap += 1
            if gap > 2:
                break
    return min(1.0, (last + 1) / h)


def figure_share(img: Image.Image) -> float:
    """Share of the panel that is skin-coloured: a rough test for a character."""
    return round(float(_panel_maps(img)["skin"].mean()), 4)


NARROW_COST = 6.0        # a window narrower than the panel: only when nothing full-width is clean
ART_PAD_COST = 1.5       # per unit of padding when the card shows only the art of a panel with white edge rows
PAD_COST = 3.0           # per unit of blurred padding a card would need above/below the panel
CUT_COST = 0.8           # per unit of the panel's height a full-width window leaves out
PAD_MAX = 2.5            # never pad a panel to more than this multiple of its own height
MIN_WIDTH_KEPT = 0.9


def _full_width_boxes(aspect: float, pa: float, fe, fb, minw: float, allow_pad: bool = False,
                      edge=(0.0, 0.0)):
    """Candidate boxes that keep the panel's whole width: (x0, y0, x1, y1, extra_cost, pad_y).

    - A shape at least as wide as the panel (aspect >= panel aspect): a full-width window
      slid up and down. Only the vertical position is a choice.
    - A shape narrower than the panel (a wide panel), when allow_pad: the whole panel, with
      the card padded above and below by pad_y panel-heights each (the card draws the
      padding as a blurred copy of the panel). The box itself stays inside the panel.
    - Last resort, narrower windows no thinner than `minw`, with a heavy cost so they
      only win when every full-width window shows a bubble or cuts a head.
    """
    r = aspect / pa                                   # window width / height as fractions
    out = []

    def vertical(bw: float, bh: float, xs, extra: float) -> None:
        ys = [0.0] if bh >= 1.0 - 1e-9 else list(np.linspace(0.0, 1.0 - bh, 21))
        if fe is not None and fb is not None and bh < 1.0 - 1e-9:
            for y in (fe - HEAD_MARGIN, fb + HEAD_MARGIN - bh):      # exact positions that keep the head
                ys.append(min(max(0.0, y), 1.0 - bh))
        for x0 in xs:
            for y0 in ys:
                out.append((x0, y0, x0 + bw, y0 + bh, extra + CUT_COST * max(0.0, 1.0 - bh), 0.0))

    et, eb = edge
    if r >= 1.0 - 1e-9:
        vertical(1.0, 1.0 / r, [0.0], 0.0)
        ah, bh = 1.0 - et - eb, 1.0 / r
        if allow_pad and et + eb >= 0.02 and ah >= 0.2 and ah < bh <= PAD_MAX:
            # White rows (a gutter, or a bubble the crop must exclude) sit on the panel's edge: show only the
            # art, full width, and pad the rest of the shape with the blurred panel instead of white.
            out.append((0.0, et, 1.0, 1.0 - eb, ART_PAD_COST * (bh - ah), (bh - ah) / 2))
        for bw in sorted({0.95, minw}, reverse=True):
            if minw <= bw < 1.0 - 1e-9 and bw / r <= 1.0 + 1e-9:
                vertical(bw, bw / r, [0.0, (1.0 - bw) / 2, 1.0 - bw], NARROW_COST)
    else:
        bh = 1.0 / r
        if allow_pad and bh <= PAD_MAX:
            out.append((0.0, 0.0, 1.0, 1.0, PAD_COST * (bh - 1.0), (bh - 1.0) / 2))
        if r >= minw - 1e-9:                          # a slightly narrower card, no padding
            vertical(r, 1.0, [0.0, (1.0 - r) / 2, 1.0 - r], NARROW_COST)
    return out


def _legacy_boxes(aspect: float, panel_aspect: float, fe, fb):
    """The original generator: zoomed windows that may cut the sides."""
    r = aspect / panel_aspect
    full = (r, 1.0) if r <= 1.0 else (1.0, 1.0 / r)
    out = []
    for s in (1.0, 0.85, 0.7, 0.55):               # no deeper zoom: the card would go soft
        bw, bh = full[0] * s, full[1] * s
        if s < 1.0 and (bw < 0.1 or bh < 0.1):
            continue                                # too small to show anything useful
        xs = [0.0] if bw >= 1.0 - 1e-9 else list(np.linspace(0.0, 1.0 - bw, 17))
        ys = [0.0] if bh >= 1.0 - 1e-9 else list(np.linspace(0.0, 1.0 - bh, 21))
        if fe is not None and fb is not None and bh < 1.0 - 1e-9:
            for y in (fe - HEAD_MARGIN, fb + HEAD_MARGIN - bh):
                ys.append(min(max(0.0, y), 1.0 - bh))
        for x0 in xs:
            for y0 in ys:
                out.append((x0, y0, x0 + bw, y0 + bh, (0.1 * (1.0 - s)) + (0.3 if (r > 1.0 and s < 1.0) else 0.0), 0.0))
    return out


def shape_candidates(img: Image.Image, aspect: float, maps: Dict[str, Any] | None = None,
                     fallback: bool = True, allow_pad: bool = False) -> List[Dict[str, Any]]:
    """Every window of exactly `aspect` (w/h in pixels) worth showing, best first.

    Each: {"box", "score", "head_ok", "bubble_ok", "blank", "soft", "painted", "texty", "pad_y",
    "narrow"}. Lower score is better. By default (maps["full_width"], video.crop.full_width)
    every window keeps the panel's full width and only slides vertically. With allow_pad, a
    panel too wide for the shape is shown whole with `pad_y` panel-heights of blurred padding
    above and below instead of losing its sides (see _full_width_boxes); the box itself
    always lies inside 0..1. With full_width off the original zoomed, side-cropping windows
    are used. A face position from fixes.json (maps["face_override"]) replaces the skin guess.
    When a shape cannot be reached at full width (and `fallback` is true) the old side-cropping
    windows are returned for it; choosers that compare several shapes pass fallback=False so a
    shape that would cost the panel its sides is simply not a candidate.
    All windows are scored together with summed-area tables, so this is fast.
    """
    m = maps or _panel_maps(img)
    gh, gw = m["soft"].shape
    panel_aspect = img.width / max(1, img.height) if img is not None else gw / gh
    head = head_of(m)
    fe, fb = head["top"], head["bottom"]
    keep_top = head["keep_top"]

    full_width = bool(m.get("full_width", True))
    boxes: List[Tuple[float, float, float, float, float, float]] = []      # x0, y0, x1, y1, extra cost, pad_y
    if full_width:
        minw = min(1.0, max(0.5, float(m.get("min_width_kept", MIN_WIDTH_KEPT))))
        boxes = _full_width_boxes(aspect, panel_aspect, fe, fb, minw, allow_pad, m.get("edge_rows") or (0.0, 0.0))
    if not boxes and (fallback or not full_width):
        boxes = _legacy_boxes(aspect, panel_aspect, fe, fb)
    if not boxes:
        return []
    B = np.array(boxes, dtype=np.float64)
    x0, y0, x1, y1, extra, pad_y = B[:, 0], B[:, 1], B[:, 2], B[:, 3], B[:, 4], B[:, 5]
    narrow = (x1 - x0) < 1.0 - 1e-9

    # Pixel rows and columns of each window, the same rule the scoring always used.
    r0 = (y0 * gh).astype(int)
    r1 = np.maximum(r0 + 1, (y1 * gh).astype(int))
    c0 = (x0 * gw).astype(int)
    c1 = np.maximum(c0 + 1, (x1 * gw).astype(int))
    area_px = ((r1 - r0) * (c1 - c0)).astype(np.float64)

    def window_mean(key: str) -> np.ndarray:
        table = np.zeros((gh + 1, gw + 1))
        table[1:, 1:] = m[key].cumsum(axis=0).cumsum(axis=1)
        total = table[r1, c1] - table[r0, c1] - table[r1, c0] + table[r0, c0]
        return total / area_px

    texty = window_mean("texty")
    soft = window_mean("soft")
    skin = window_mean("skin")
    blank = window_mean("blank")
    head_ok = np.ones(len(B), dtype=bool)
    et, eb = m.get("edge_rows") or (0.0, 0.0)
    if fe is not None:
        # The margin above a head cannot reach past the art itself (beyond it is white).
        head_ok &= y0 <= max(fe - HEAD_MARGIN, min(et, fe)) + 1e-9
        if fb is not None:
            head_ok &= y1 >= min(fb + HEAD_MARGIN, max(1.0 - eb, fb)) - 1e-9
    keep_cost = 0.0
    if keep_top:
        # A figure but no head located: never crop the top of the panel away; prefer keeping it all.
        head_ok &= y0 < keep_top
        keep_cost = 4.0 * y0

    def worst_overlap(regions) -> np.ndarray:
        worst = np.zeros(len(B))
        box_area = (x1 - x0) * (y1 - y0)
        for bx0, by0, bx1, by1 in regions:
            ix = np.maximum(0.0, np.minimum(x1, bx1) - np.maximum(x0, bx0))
            iy = np.maximum(0.0, np.minimum(y1, by1) - np.maximum(y0, by0))
            share = np.where(box_area > 0, ix * iy / np.maximum(box_area, 1e-12), 0.0)
            worst = np.maximum(worst, share)
        return worst

    bubble = worst_overlap(m["bubbles"])
    painted = worst_overlap(m["painted"])
    white = (np.maximum(0.0, et - y0) + np.maximum(0.0, y1 - (1.0 - eb))) / np.maximum(y1 - y0, 1e-9)
    score = (2.0 * texty + 4.0 * blank - 0.6 * soft - 0.5 * skin + extra + keep_cost + 6.0 * white
             + (soft < 0.02).astype(float) + np.where(head_ok, 0.0, 5.0)
             + 3.0 * painted                                    # a painted patch is flat: avoid it
             + 20.0 * bubble + np.where(bubble > BUBBLE_OK, 30.0, 0.0))  # bubble text outranks a head risk
    order = np.argsort(score, kind="stable")[:60]
    out: List[Dict[str, Any]] = []
    for i in order:
        box = (round(float(x0[i]), 4), round(float(y0[i]), 4), round(float(x1[i]), 4), round(float(y1[i]), 4))
        out.append({"box": box, "score": float(score[i]), "head_ok": bool(head_ok[i]),
                    "bubble_ok": bool(bubble[i] <= BUBBLE_OK), "blank": float(blank[i]), "soft": float(soft[i]),
                    "painted": float(painted[i]), "texty": float(texty[i]),
                    "pad_y": round(float(pad_y[i]), 4), "narrow": bool(narrow[i])})
    return out


def shape_crop(img: Image.Image, aspect: float, maps: Dict[str, Any] | None = None) -> Tuple[Box, float, bool, bool]:
    """Best window of exactly `aspect`: (box, score, head_ok, bubble_ok)."""
    best = shape_candidates(img, aspect, maps)[0]
    return best["box"], best["score"], best["head_ok"], best["bubble_ok"]


def choose_shape(img: Image.Image, shapes: List[float],
                 maps: Dict[str, Any] | None = None) -> Tuple[float, Box, bool, bool]:
    """Pick the allowed shape that shows this panel best: the lowest crop score,
    with a penalty for cutting the sides and a small pull toward the panel's own
    shape. Returns (shape, box, head_ok, bubble_ok)."""
    m = maps or _panel_maps(img)
    gw, gh = m["soft"].shape[1], m["soft"].shape[0]
    panel_aspect = gw / gh
    best = None
    for a in shapes:
        box, score, head_ok, bubble_ok = shape_crop(img, a, m)
        cost = score + 0.4 * max(0.0, np.log(panel_aspect / a)) + 0.02 * abs(np.log(a / panel_aspect))
        if best is None or cost < best[0]:
            best = (cost, a, box, head_ok, bubble_ok)
    return best[1], best[2], best[3], best[4]



# --- P1: shouting-face + dominant-bubble mask risk -------------------------
# Observed in real chapters (Ch2 P018-04 / P020-04): when a DOMINANT speech
# bubble overlaps a shouting face, painting the bubble out would leave a flat
# grey oval sitting on the face in the final render. The fix is never to
# paint over a face (painting is removed entirely: nothing is ever painted).
# Crops must steer around such bubbles, and when no
# clean crop exists the panel is DROPPED (verify.py) and its time is
# re-paired to a neighbour, instead of masking the face in render.
DOMINANT_BUBBLE_AREA = 0.12    # bubble box >= 12% of the panel: dominant
DOMINANT_BUBBLE_WIDTH = 0.35   # ... or >= 35% of the panel wide: dominant
PATCH_VISIBLE = 0.12          # patch_visibility() above this: visible patch


def head_vertical_range(maps: Dict[str, Any]) -> Tuple[float, float] | None:
    """(top, bottom) fractions of the panel where the head sits, or None."""
    head = head_of(maps)
    if head.get("top") is None or head.get("bottom") is None:
        return None
    return (float(head["top"]), float(head["bottom"]))


def bubble_overlaps_face(box: Box, face: Tuple[float, float] | None) -> bool:
    """True when a bubble box overlaps the face's vertical range (any size).

    The face box is (top, bottom) panel fractions from head_vertical_range.
    Any vertical overlap counts: even a small bubble that touches
    the head range leaves a flat patch on/near the face.
    """
    if face is None:
        return False
    top, bottom = face
    return box[1] < bottom and box[3] > top


def face_bubble_boxes(maps: Dict[str, Any]) -> List[Box]:
    """Bubble boxes overlapping the detected head's vertical range.

    Dominant ones are the mask-risk set (see mask_risk_bubbles); smaller
    ones are the mask-risk set (see mask_risk_bubbles). Nothing is ever
    painted now: the crop must exclude them, or the
    panel is dropped (verify.py).
    """
    face = head_vertical_range(maps)
    if face is None:
        return []
    return [r["box"] for r in maps.get("bubble_regions") or []
            if bubble_overlaps_face(r["box"], face)]


def mask_risk_bubbles(maps: Dict[str, Any]) -> List[Box]:
    """Bubble boxes that would have masked a face if painted out.

    A bubble is mask-risk when it is DOMINANT and its box overlaps the
    detected head's vertical range. Dominant = large in area or width.
    Shouting (open mouth) cannot be detected reliably from pixels, so the
    rule is deliberately conservative: any dominant bubble overlapping
    the head range counts. Painting is removed entirely (see clean_bubbles),
    so these panels are dropped instead of shown (verify.py).
    """
    out: List[Box] = []
    for box in face_bubble_boxes(maps):
        x0, y0, x1, y1 = box
        area = (x1 - x0) * (y1 - y0)
        dominant = area >= DOMINANT_BUBBLE_AREA or (x1 - x0) >= DOMINANT_BUBBLE_WIDTH
        if dominant:
            out.append(box)
    return out


def is_mask_risk(maps: Dict[str, Any]) -> bool:
    """True when this panel's bubbles would have masked a face if painted (painting removed)."""
    return bool(mask_risk_bubbles(maps))


def patch_visibility(img: Image.Image, painted_boxes: List[Box]) -> float:
    """How visible painted-bubble patches are in a panel, 0..1.

    Kept as the measurement for any legacy painted input. Nothing paints
    any more (clean_bubbles() is a no-op), so in practice this always sees
    an empty list and returns 0.0.
    A painted patch is a FLAT fill (one median colour) sitting in art that
    usually has texture. For each painted box, compares texture (grey
    standard deviation) inside the box with texture in the ring just
    outside it: a well-feathered patch on flat art scores ~0 (nothing to
    see), a flat disc on textured art scores high. Returns the worst box's
    score. 0.0 when there are no painted boxes.
    Used by verify.py as a QA flag (visible_patch), never as a drop reason:
    a visible patch is a reviewer warning, not a missing picture.
    """
    if not painted_boxes:
        return 0.0
    gray = np.asarray(img.convert("L"), dtype=np.float64) / 255.0
    h, w = gray.shape
    worst = 0.0
    for x0, y0, x1, y1 in painted_boxes:
        ix0, iy0 = max(0, int(x0 * w)), max(0, int(y0 * h))
        ix1, iy1 = min(w, int(x1 * w)), min(h, int(y1 * h))
        if ix1 - ix0 < 10 or iy1 - iy0 < 10:
            continue
        inside = gray[iy0 + 2:iy1 - 2, ix0 + 2:ix1 - 2]
        ring = np.concatenate([
            gray[max(0, iy0 - 6):iy0, ix0:ix1].ravel(),
            gray[iy1:min(h, iy1 + 6), ix0:ix1].ravel(),
            gray[iy0:iy1, max(0, ix0 - 6):ix0].ravel(),
            gray[iy0:iy1, ix1:min(w, ix1 + 6)].ravel(),
        ]) if True else np.array([])
        if inside.size == 0 or ring.size == 0:
            continue
        score = float(ring.std()) - float(inside.std())
        worst = max(worst, score)
    return round(max(0.0, float(worst)), 4)


def clean_bubbles(img: Image.Image, maps: Dict[str, Any] | None = None) -> Image.Image:
    """Return the panel UNCHANGED -- bubble painting was removed entirely.

    Painting bubbles out harmed more than it helped: the median-colour
    fill could land on a character face (a grey / peach / white patch),
    and no feathering fixed that reliably. So nothing is painted any
    more: bubbles stay visible in the panel, crops steer around them
    (bubble scoring in shape_candidates), and a panel with no clean
    crop -- its best window still shows a bubble -- is DROPPED by
    verify.py and re-paired instead of being painted. That is the P1
    mask-risk rule, now applied to every bubble that would have been
    painted.

    Kept as a no-op for call-site compatibility (panels.panel_image
    still calls it with clean=True): it returns the same image object,
    pixel-identical, with no painted_boxes set. `maps` is accepted and
    ignored. There is no flag to re-enable painting -- do not add one.
    """
    return img
