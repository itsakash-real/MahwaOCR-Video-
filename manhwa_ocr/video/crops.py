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

from typing import List, Tuple

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
# list per layout. The window is slid over the panel and scored: it avoids
# lettering and flat white, keeps the head inside (a face's top edge is never
# cut), and prefers real art. A window of the target shape always exists, so
# nothing is dropped for lack of a crop.

HEAD_MARGIN = 0.04       # fraction of the panel's height kept above a detected head
FIGURE_MIN = 0.004       # skin share above this means a character is drawn here


def _panel_maps(img: Image.Image) -> Dict[str, np.ndarray]:
    """Analysis maps for one panel, computed once and sliced per candidate."""
    rgb = _small(img, 120)
    soft, hard, flat_white = _art_maps(rgb)
    return {"soft": soft, "texty": np.clip(hard + flat_white, 0, 1), "skin": _skin(rgb),
            "blank": flat_white}


def face_top(maps: Dict[str, np.ndarray]) -> float | None:
    """Fraction of the panel's height where the topmost character's skin starts,
    searched in the upper 55% (where heads are). None if no figure is found.

    Heuristic: a run of three rows that are at least 3% skin. Pale or heavily
    stylised art can miss; the QA report counts those panels as head_risk."""
    rows = maps["skin"].mean(axis=1)
    h = len(rows)
    run = 0
    for y in range(int(0.55 * h)):
        run = run + 1 if rows[y] >= 0.03 else 0
        if run >= 3:
            return max(0.0, (y - 2) / h)
    return None


def figure_share(img: Image.Image) -> float:
    """Share of the panel that is skin-coloured: a rough test for a character."""
    return round(float(_panel_maps(img)["skin"].mean()), 4)


def shape_crop(img: Image.Image, aspect: float, maps: Dict[str, np.ndarray] | None = None) -> Tuple[Box, float, bool]:
    """Best window of exactly `aspect` (w/h in pixels) inside the panel.

    Returns (box, score, head_ok). Lower score is better. A window that is
    narrower than the panel cuts the sides; a wider-than-target panel is cut
    only at the top and bottom, so panels keep their full width when they can.
    """
    m = maps or _panel_maps(img)
    gh, gw = m["soft"].shape
    panel_aspect = gw / gh
    r = aspect / panel_aspect                       # box width / box height, as fractions
    fe = face_top(m)
    best: Tuple[float, Box, bool] | None = None
    if r <= 1.0:
        # narrower than the panel: smaller windows of the same shape (sides may be cut)
        sizes = [(r * s, s, s) for s in (1.0, 0.85, 0.7, 0.55)]
    else:
        # wider than the target: full width only, so only top and bottom are cut
        sizes = [(1.0, 1.0 / r, 1.0)]
    for bw, bh, s in sizes:
        xs = [0.0] if bw >= 1.0 - 1e-9 else list(np.linspace(0.0, 1.0 - bw, 7))
        ys = [0.0] if bh >= 1.0 - 1e-9 else list(np.linspace(0.0, 1.0 - bh, 11))
        for x0 in xs:
            for y0 in ys:
                x1, y1 = x0 + bw, y0 + bh
                r0, r1 = int(y0 * gh), max(int(y0 * gh) + 1, int(y1 * gh))
                c0, c1 = int(x0 * gw), max(int(x0 * gw) + 1, int(x1 * gw))
                texty = float(m["texty"][r0:r1, c0:c1].mean())
                soft = float(m["soft"][r0:r1, c0:c1].mean())
                skin = float(m["skin"][r0:r1, c0:c1].mean())
                blank = float(m["blank"][r0:r1, c0:c1].mean())
                head_ok = fe is None or y0 <= fe - HEAD_MARGIN
                score = (2.0 * texty + 4.0 * blank - 0.6 * soft - 0.5 * skin + 0.1 * (1.0 - s)
                         + (1.0 if soft < 0.02 else 0.0) + (5.0 if not head_ok else 0.0))
                if best is None or score < best[0]:
                    box = (round(float(x0), 4), round(float(y0), 4), round(float(x1), 4), round(float(y1), 4))
                    best = (score, box, head_ok)
    score, box, head_ok = best
    return box, score, head_ok


def choose_shape(img: Image.Image, shapes: List[float], maps: Dict[str, np.ndarray] | None = None) -> Tuple[float, Box, bool]:
    """Pick the allowed shape that shows this panel best: the lowest crop score,
    with a penalty for cutting the sides and a small pull toward the panel's own
    shape. Returns (shape, box, head_ok)."""
    m = maps or _panel_maps(img)
    gw, gh = m["soft"].shape[1], m["soft"].shape[0]
    panel_aspect = gw / gh
    best = None
    for a in shapes:
        box, score, head_ok = shape_crop(img, a, m)
        cost = (score + 0.4 * max(0.0, np.log(panel_aspect / a)) + 0.02 * abs(np.log(a / panel_aspect)))
        if best is None or cost < best[0]:
            best = (cost, a, box, head_ok)
    return best[1], best[2], best[3]
