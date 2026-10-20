"""Drawing video frames in the recap-channel style.

Style (see manhwa_recap_style_spec.md, measured from a real recap channel):
  * Hard cuts between shots. P3: continuous Ken Burns motion across the
    whole shot (push_in 100->115%, pull_out 118->100%, pans traverse the
    crop) with ease_in_out, so the frame is never frozen. `hold` keeps the
    old ease-out-then-hold, for hand-set payoff beats only.
  * Panels are shown as cards: a focal crop of the panel, thin white border,
    rounded corners and a strong black drop shadow.
  * Layouts: single (centred card), pair (first card centred, then slides
    left as the second slides in from the right), inset (small card over a
    blurred close-up), wide (near full-width panel).
  * Backgrounds: blurred + darkened + vignetted copy of the panel, or plain
    black, or plain white.
  * CapCut-style entrances (fade_in, slide_*, pull_down, zoom_in,
    fit_in_frame, blur_in, swing_right), all 0.6-0.8s with ease-out curves:
    deliberately unhurried.
  * Defect fix (CapCut bugfix): a shot is NEVER returned as a frozen static
    frame unless every card is a hand-set `hold` (see ShotRenderer.is_static).
    Continuous-motion shots are re-drawn every frame, so a >3s panel cannot
    read as a 0.5s cut/glitch. Joins are frame-exact (plan.shot_frame_bounds);
    concat is a hard cut by design, never a crossfade.

Every function here is a pure function of (shot, time), so frames can be drawn
in any order and the storyboard shows exactly what the video will.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

LAYOUTS = ("single", "pair", "inset", "wide")
BACKGROUNDS = ("blur", "black", "white")
MOTIONS = ("push_in", "pull_out", "pan_left", "pan_right", "pan_up", "pan_down", "hold")
ENTRANCES = ("cut", "fade_in", "slide_left", "slide_right", "slide_up", "slide_down", "pull_down",
             "zoom_in", "fit_in_frame", "blur_in", "swing_right", "whip_in")
EXITS = ("cut", "fade_out")
EFFECTS = ("shake", "punch", "flash", "glitch")
# CapCut-style shot transitions (how a shot ARRIVES; see transition_enter).
# cut is the default (90%); fade 10%; slide / zoom / whip / glitch only on
# impact / reveal beats, and glitch at most once per 60s. Every transition
# is a pure function of (shot, time): frame-exact, no flicker by design.
TRANSITIONS = ("cut", "fade", "slide", "zoom", "whip", "glitch")
TRANSITION_WEIGHTS = [("cut", 90), ("fade", 10)]
TRANSITION_ENTERS = {"cut": "cut", "fade": "fade_in", "slide": "slide_left",
                     "zoom": "zoom_in", "whip": "whip_in", "glitch": "cut"}
GLITCH_LEN = 0.2          # RGB-split glitch length, seconds (CapCut-style, impact beats only)
GLITCH_MIN_GAP = 60.0     # at most one glitch per 60s of video

ENTER_DUR = {"cut": 0.0, "fade_in": 0.6, "slide_left": 0.7, "slide_right": 0.7, "slide_up": 0.7,
             "slide_down": 0.7, "pull_down": 0.75, "zoom_in": 0.7, "fit_in_frame": 0.75, "blur_in": 0.8,
             "swing_right": 0.8, "whip_in": 0.25}
def transition_enter(transition: str) -> str:
    """The card entrance a shot transition arrives with (TRANSITIONS).
    "cut" and "fade" keep their own entrances; slide / zoom / whip map to
    the matching entrance; glitch cuts in hard and the RGB split carries it.
    Unknown transitions fall back to a hard cut, never to an animation.
    """
    return TRANSITION_ENTERS.get(transition, "cut")


KB_MAX = 2.6          # P3: only `hold` still settles after this long; other motions run the whole shot
PAIR_MOVE = 0.7       # first card's slide to the left when the second arrives
EXIT_FADE = 0.6
SHAKE_LEN, PUNCH_LEN, FLASH_LEN = 0.5, 0.85, 0.6
SRC_HEADROOM = 1.12   # source pre-scaled this much beyond the card for zoom room


@dataclass(frozen=True)
class Format:
    width: int
    height: int
    fps: int

    @property
    def portrait(self) -> bool:
        return self.height > self.width

    @property
    def unit(self) -> float:
        return min(self.width, self.height) / 1080.0


def format_from(name: str, fps: int, preview: bool = False) -> Format:
    portrait = str(name).replace(" ", "") in {"9:16", "shorts", "portrait", "vertical"}
    w, h = (1080, 1920) if portrait else (1920, 1080)
    if preview:
        w, h = (480, 854) if portrait else (854, 480)
    return Format(w, h, int(fps))


# ------------------------------------------------------------------ easing


def ease_out(p: float) -> float:
    p = min(max(p, 0.0), 1.0)
    return 1 - (1 - p) ** 3


def ease_out_back(p: float, s: float = 1.2) -> float:
    p = min(max(p, 0.0), 1.0) - 1
    return 1 + (s + 1) * p ** 3 + s * p ** 2


def ease_in_out(p: float) -> float:
    p = min(max(p, 0.0), 1.0)
    return 0.5 - 0.5 * math.cos(math.pi * p)


# ------------------------------------------------------------------ layout geometry


# Dedicated caption bar: the bottom 8% of the frame is a black bar drawn
# under captioned shots (ShotRenderer), and cards are capped to the top
# 92% so no caption ever overlaps panel art. Must match
# captions.CAPTION_BAR_TOP_FRACTION (kept as a literal here so frames.py
# stays importable without the captions module).
CAPTION_BAR_TOP_FRACTION = 0.92


def card_box(layout: str, fmt: Format) -> Tuple[float, float]:
    """Largest card size (w, h) for a layout; the crop's aspect decides the actual fit.

    Card heights are capped to the top 92% of the frame: the bottom 8%
    is the dedicated caption bar, and a card must never extend into it.
    """
    W, H = fmt.width, fmt.height
    if fmt.portrait:
        return {"single": (0.92 * W, 0.70 * H), "pair": (0.90 * W, 0.42 * H),
                "inset": (0.55 * W, 0.28 * H), "wide": (0.96 * W, 0.55 * H)}.get(layout, (0.92 * W, 0.7 * H))
    return {"single": (0.62 * W, 0.92 * H), "pair": (0.42 * W, 0.80 * H),
            "inset": (0.34 * W, 0.60 * H), "wide": (0.96 * W, 0.92 * H)}.get(layout, (0.62 * W, 0.92 * H))


# Crop shape ranges (w/h) per layout. Tall panels are cut to a focal region of
# this shape rather than shrunk into a thin strip: a single card should take
# roughly 40-60% of the frame width, a pair card is portrait like the reference.
_CROP_RANGE = {
    False: {"single": (0.82, 1.2), "pair": (0.62, 0.95), "inset": (0.85, 1.25)},
    True: {"single": (0.75, 1.3), "pair": (0.9, 1.6), "inset": (0.9, 1.5)},
}


def crop_aspect(layout: str, fmt: Format, panel_aspect: float) -> float:
    """The crop shape (w/h) a panel should be cut to for this layout."""
    bw, bh = card_box(layout, fmt)
    box = bw / bh
    if layout == "wide":
        return min(panel_aspect, box)
    lo, hi = _CROP_RANGE[fmt.portrait].get(layout, (0.8, 1.2))
    return min(max(panel_aspect, lo), min(hi, box))


# The only card shapes a video uses (w/h). Landscape video: 3:4, 1:1, 4:3, 16:9.
# Portrait (9:16) video: 9:16, 3:4, 1:1. Each layout gets its own short list.
LAYOUT_SHAPES = {
    False: {"single": [0.5625, 0.75, 1.0, 1.333, 1.777], "pair": [0.5625, 0.75, 1.0], "inset": [0.75, 1.0],
            "wide": [1.333, 1.777]},
    True: {"single": [0.5625, 0.75, 1.0], "pair": [0.5625, 0.75], "inset": [0.5625, 0.75],
           "wide": [0.75, 1.0]},
}


def shapes_for(layout: str, fmt: Format) -> List[float]:
    return LAYOUT_SHAPES[fmt.portrait].get(layout, [1.0])


def layout_area(layout: str, fmt: Format) -> float:
    """One card area (in pixels) for every shape of a layout, so cards of
    different shapes in the same layout carry the same visual weight. Chosen as
    the largest area at which every shape still fits its layout box."""
    bw, bh = card_box(layout, fmt)
    return 0.98 * min(min(bw * bw / a, a * bh * bh) for a in shapes_for(layout, fmt))


def fit(aspect: float, box: Tuple[float, float], area: float | None = None) -> Tuple[int, int]:
    """Card size (w, h) for an aspect. With `area`, every shape gets that same
    area (only shrunk if it still doesn't fit the box)."""
    bw, bh = box
    if area:
        cw, ch = (area * aspect) ** 0.5, (area / aspect) ** 0.5
        k = min(1.0, bw / cw, bh / ch)
        cw, ch = cw * k, ch * k
        return max(8, int(cw)), max(8, int(ch))
    cw = min(bw, bh * aspect)
    return max(8, int(cw)), max(8, int(cw / aspect))


def slot_centres(layout: str, fmt: Format, size: Tuple[int, int], index: int) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """(start centre, resting centre) for card `index` in this layout.

    Cards centre on the top 92% of the frame (0.46 * H): the bottom 8%
    is the dedicated caption bar, and no card may extend into it.
    """
    W, H = fmt.width, fmt.height
    cw, ch = size
    if layout == "pair":
        if fmt.portrait:
            rest = (W / 2, 0.23 * H) if index == 0 else (W / 2, 0.67 * H)
        else:
            rest = (0.27 * W, 0.46 * H) if index == 0 else (0.73 * W, 0.46 * H)
        start = (W / 2, 0.46 * H) if index == 0 else rest
        return start, rest
    if layout == "inset":
        if fmt.portrait:
            c = (W / 2, 0.92 * H - ch / 2)
        else:
            c = (0.05 * W + cw / 2, 0.92 * H - ch / 2)
        return c, c
    c = (W / 2, 0.46 * H)
    return c, c


# ------------------------------------------------------------------ backgrounds


@lru_cache(maxsize=8)
def _vignette_grid(width: int, height: int) -> np.ndarray:
    ys, xs = np.ogrid[0:height, 0:width]
    nx = (xs - width / 2) / (width / 2)
    ny = (ys - height / 2) / (height / 2)
    d = np.sqrt(nx ** 2 + ny ** 2) / math.sqrt(2)
    return (1.0 - 0.45 * np.clip(d, 0, 1) ** 2).astype(np.float32)[..., None]


def _vignette(fmt: Format) -> np.ndarray:
    # The vignette depends only on the frame size: identical for every shot
    # in a chapter, so build it once. Callers must not mutate the result
    # (background() only multiplies by it).
    return _vignette_grid(fmt.width, fmt.height)


def background(kind: str, panel: Image.Image | None, fmt: Format) -> np.ndarray:
    if kind == "black" or (kind == "blur" and panel is None):
        return np.zeros((fmt.height, fmt.width, 3), np.uint8)
    if kind == "white":
        return np.full((fmt.height, fmt.width, 3), 255, np.uint8)
    # Blur: the panel scaled to cover the frame, blurred, darkened, vignetted.
    sw, sh = max(4, fmt.width // 8), max(4, fmt.height // 8)
    k = max(sw / panel.width, sh / panel.height)
    small = panel.convert("RGB").resize((max(4, int(panel.width * k) + 1), max(4, int(panel.height * k) + 1)),
                                        Image.BILINEAR)
    left, top = (small.width - sw) // 2, (small.height - sh) // 2
    small = small.crop((left, top, left + sw, top + sh)).filter(ImageFilter.GaussianBlur(4.5))
    big = np.asarray(small.resize((fmt.width, fmt.height), Image.BICUBIC), dtype=np.float32)
    return np.clip(big * 0.72 * _vignette(fmt), 0, 255).astype(np.uint8)


# ------------------------------------------------------------------ P4 caches
# Two per-shot rebuilds that P4 caches. Both hold a strong reference to
# the source panel in the entry, so a recycled id() can never hit a stale
# entry (the entry keeps its panel alive; eviction drops the reference).
# Cached arrays are never mutated by callers: frame_array copies the bg
# (Image.fromarray / PIL copy) and only reads Card.src via transform.
_BG_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_BG_CACHE_MAX = 8
_SRC_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_SRC_CACHE_MAX = 32


def _cached_background(kind: str, panel: Image.Image | None, fmt: Format,
                       bg_panel_id: str | None) -> np.ndarray:
    """background() with a cross-shot cache. Consecutive shots usually
    share a bg_panel (same panel across sentences); rebuilding the
    blurred + vignetted 1080p backdrop per shot re-pays the upscale +
    vignette multiply every time. Key includes the panel's identity so a
    different image under the same panel id can never hit a stale entry.
    Solid (black/white) backgrounds are cached by kind + frame size."""
    if panel is None:
        key = (kind, fmt.width, fmt.height, None, None)
        hit = _BG_CACHE.get(key)
        if hit is not None:
            _BG_CACHE.move_to_end(key)
            return hit[1]
        arr = background(kind, None, fmt)
        _BG_CACHE[key] = (None, arr)
    else:
        key = (kind, fmt.width, fmt.height, bg_panel_id, id(panel))
        hit = _BG_CACHE.get(key)
        if hit is not None and hit[0] is panel:
            _BG_CACHE.move_to_end(key)
            return hit[1]
        arr = background(kind, panel, fmt)
        _BG_CACHE[key] = (panel, arr)
    while len(_BG_CACHE) > _BG_CACHE_MAX:
        _BG_CACHE.popitem(last=False)
    return arr


def _cached_src(panel: Image.Image, region: Image.Image, crop_key: tuple,
                iw: int, ih: int) -> Image.Image:
    """The LANCZOS resize of a card's crop (the ~20ms part of Card init).
    The resize is a pure function of (panel, crop, pad_y, iw, ih); sharing
    the result across Cards that show the same panel the same way is safe
    because transform() only reads src."""
    key = (id(panel), crop_key, iw, ih)
    hit = _SRC_CACHE.get(key)
    if hit is not None and hit[0] is panel:
        _SRC_CACHE.move_to_end(key)
        return hit[1]
    src = region.convert("RGB").resize((max(2, round(iw * SRC_HEADROOM)),
                                        max(2, round(ih * SRC_HEADROOM))), Image.LANCZOS)
    _SRC_CACHE[key] = (panel, src)
    while len(_SRC_CACHE) > _SRC_CACHE_MAX:
        _SRC_CACHE.popitem(last=False)
    return src


# ------------------------------------------------------------------ cards


def padded_region(panel: Image.Image, box: List[float], pad_y: float) -> Image.Image:
    """The card's picture when it is taller than its crop: the crop in full width, centred,
    with `pad_y` panel-heights above and below filled by a blurred, darkened copy of the
    panel itself (a wide panel is shown whole rather than losing its sides)."""
    x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in box)
    pw, ph = panel.size
    inner = panel.convert("RGB").crop((int(x0 * pw), int(y0 * ph), max(int(x0 * pw) + 2, int(x1 * pw)),
                                       max(int(y0 * ph) + 2, int(y1 * ph))))
    pad = max(0, round(float(pad_y) * ph))
    cw, ch = inner.width, inner.height + 2 * pad
    k = max(cw / inner.width, ch / inner.height)
    cover = inner.resize((max(cw, round(inner.width * k)), max(ch, round(inner.height * k))), Image.BILINEAR)
    left, top = (cover.width - cw) // 2, (cover.height - ch) // 2
    canvas = cover.crop((left, top, left + cw, top + ch)).filter(ImageFilter.GaussianBlur(max(4, ch * 0.03)))
    canvas = Image.eval(canvas, lambda v: int(v * 0.7))
    canvas.paste(inner, (0, pad))
    return canvas


@lru_cache(maxsize=32)
def _shadow_mask(sw: int, sh: int, m: int, off: int, cw: int, ch: int, radius: int, u: float) -> Image.Image:
    # The blurred drop shadow depends only on the card geometry, and cards
    # share a handful of sizes: build it once per size instead of blurring
    # a ~1300x1900 mask for every card. The returned image is shared
    # (putalpha reads from it without mutating it); callers must not draw on it.
    shadow = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        (m - round(4 * u), m + off - round(2 * u), m + cw + round(4 * u),
         m + ch + off + round(6 * u)), radius + 4, fill=235)
    return shadow.filter(ImageFilter.GaussianBlur(20 * u))


class Card:
    """One panel card: focal crop, border, rounded corners, black shadow, motion."""

    def __init__(self, panel: Image.Image, spec: Dict[str, Any], layout: str, index: int,
                 shot_start: float, shot_end: float, fmt: Format, pair_move_at: float | None):
        self.fmt = fmt
        self.layout, self.index = layout, index
        u = fmt.unit
        crop = spec.get("crop") or [0, 0, 1, 1]
        x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in crop)
        if x1 - x0 < 0.02 or y1 - y0 < 0.02:
            x0, y0, x1, y1 = 0.0, 0.0, 1.0, 1.0
        pw, ph = panel.size
        region = panel.crop((int(x0 * pw), int(y0 * ph), max(int(x0 * pw) + 2, int(x1 * pw)),
                             max(int(y0 * ph) + 2, int(y1 * ph))))
        pad_y = float(spec.get("pad_y") or 0.0)
        if pad_y > 0:                               # a wide panel shown whole on blurred padding
            region = padded_region(panel, [x0, y0, x1, y1], pad_y)
        aspect = region.width / region.height
        if spec.get("wide_fill"):
            # System-window cards fill the frame width: no equal-area
            # shrink (a 1.777 system banner must reach >=90% of the
            # frame width, which the equal-area rule never allows).
            self.cw, self.ch = fit(aspect, card_box(layout, fmt), None)
        else:
            self.cw, self.ch = fit(aspect, card_box(layout, fmt), layout_area(layout, fmt))
        # Wide mode ("video.mode: wide"): the card is full frame height and
        # at least 8.5/16 of the frame wide -- no thin strips, ever. Pairs
        # split the frame in two. Portrait formats keep classic sizing.
        self.wide_display = bool(spec.get("wide_display")) and fmt.width > fmt.height
        if self.wide_display:
            H, W = fmt.height, fmt.width
            if layout == "pair":
                self.cw, self.ch = W // 2, H  # 8/16 each: two panels fill the frame
            else:
                self.ch = H
                self.cw = max(round(0.53125 * W), min(W, round(H * aspect)))
        self.border = max(1, round(3 * u))
        self.radius = max(2, round(12 * u))
        self.margin = max(6, round(46 * u))
        iw, ih = self.cw - 2 * self.border, self.ch - 2 * self.border
        self.iw, self.ih = iw, ih
        # P4: cache the LANCZOS resize; same panel + crop + target size reuses it.
        self.src = _cached_src(panel, region, (x0, y0, x1, y1, pad_y), iw, ih)
        if self.wide_display and region.width < iw:
            # Wide mode upscale: the 480p-look fix -- mild sharpen after Lanczos.
            self.src = self.src.copy().filter(ImageFilter.UnsharpMask(radius=2.0, percent=60, threshold=3))

        # Static parts of the sprite, built once: shadow + white rounded border.
        m = self.margin
        sw, sh = self.cw + 2 * m, self.ch + 2 * m
        off = round(10 * u)
        shadow = _shadow_mask(sw, sh, m, off, self.cw, self.ch, self.radius, u)
        base = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        base.putalpha(shadow)
        card_mask = Image.new("L", (sw, sh), 0)
        ImageDraw.Draw(card_mask).rounded_rectangle((m, m, m + self.cw - 1, m + self.ch - 1), self.radius, fill=255)
        base.paste(Image.new("RGBA", (sw, sh), (255, 255, 255, 255)), (0, 0), card_mask)
        self.base = base
        self.inner_mask = Image.new("L", (iw, ih), 0)
        ImageDraw.Draw(self.inner_mask).rounded_rectangle((0, 0, iw - 1, ih - 1), max(1, self.radius - self.border),
                                                          fill=255)

        self.appear = shot_start + max(0.0, float(spec.get("at") or 0.0))
        self.enter = spec.get("enter") if spec.get("enter") in ENTRANCES else "cut"
        self.enter_dur = ENTER_DUR[self.enter]
        self.motion = spec.get("motion") if spec.get("motion") in MOTIONS else "push_in"
        # P3: non-hold motions run the whole visible shot (continuous Ken
        # Burns); `hold` keeps the old settle-then-hold (payoff beats only).
        if self.motion == "hold":
            self.kb_dur = max(0.01, min(shot_end - self.appear, KB_MAX))
        else:
            self.kb_dur = max(0.01, shot_end - self.appear)
        self.start_c, self.rest_c = slot_centres(layout, fmt, (self.cw, self.ch), index)
        if self.wide_display:
            cy = fmt.height / 2.0
            if layout == "pair":
                lx, rx = self.cw / 2.0, fmt.width - self.cw / 2.0
                self.rest_c = (lx, cy) if index == 0 else (rx, cy)
                self.start_c = (fmt.width / 2.0, cy) if index == 0 else self.rest_c
            else:
                self.start_c = self.rest_c = (fmt.width / 2.0, cy)
        self.pair_move_at = pair_move_at if (layout == "pair" and index == 0) else None

    # -------------------------------------------------- time-varying parts

    def visible(self, t: float) -> bool:
        return t >= self.appear - 1e-9

    def settled_at(self) -> float:
        end = max(self.appear + self.enter_dur, self.appear + self.kb_dur)
        if self.pair_move_at is not None:
            end = max(end, self.pair_move_at + PAIR_MOVE)
        return end

    def _viewport(self, t: float, extra_zoom: float) -> Tuple[float, float, float, float]:
        sw, sh = self.src.size
        m = self.motion
        # P3: non-hold motions ease in-out across the whole shot (continuous
        # motion); `hold` keeps ease-out-then-hold.
        p = ease_out((t - self.appear) / self.kb_dur) if m == "hold" else ease_in_out((t - self.appear) / self.kb_dur)
        zoom = {"push_in": 1.0 + 0.15 * p, "pull_out": 1.18 - 0.18 * p, "hold": 1.01 + 0.02 * p}.get(m, 1.08)
        zoom *= 1 + extra_zoom
        # The source is the whole crop (at extra resolution, so zoomed views stay
        # sharp); zoom 1 shows all of it, zoom 1.1 shows the middle 1/1.1.
        vw, vh = sw / zoom, sh / zoom
        cx, cy = sw / 2, sh / 2
        sx, sy = (sw - vw) / 2 * 0.85, (sh - vh) / 2 * 0.85
        q = 2 * p - 1  # -1 -> +1 across the move
        if m == "pan_left":
            cx += sx * -q
        elif m == "pan_right":
            cx += sx * q
        elif m == "pan_up":
            cy += sy * -q
        elif m == "pan_down":
            cy += sy * q
        return cx - vw / 2, cy - vh / 2, vw, vh

    def centre(self, t: float) -> Tuple[float, float]:
        if self.pair_move_at is None:
            return self.rest_c
        p = ease_out((t - self.pair_move_at) / PAIR_MOVE)
        (ax, ay), (bx, by) = self.start_c, self.rest_c
        return ax + (bx - ax) * p, ay + (by - ay) * p

    def pair_move_done(self, t: float) -> bool:
        """True once the pair slide has finished (or there is no pair move)."""
        return self.pair_move_at is None or t >= self.pair_move_at + PAIR_MOVE

    def entrance(self, t: float) -> Dict[str, float]:
        """Offsets/scale/rotation/blur/alpha for the entrance animation at time t."""
        out = {"dx": 0.0, "dy": 0.0, "scale": 1.0, "rot": 0.0, "blur": 0.0, "alpha": 1.0}
        if self.enter == "cut" or t >= self.appear + self.enter_dur:
            return out
        u = t - self.appear
        d = self.enter_dur
        p = ease_out(u / d)
        W, H, e = self.fmt.width, self.fmt.height, self.enter
        fade = min(1.0, u / (0.55 * d))
        if e == "fade_in":
            out["alpha"] = ease_in_out(u / d)
        elif e == "slide_left":       # comes in from the right
            out["dx"], out["alpha"] = (1 - p) * 0.38 * W, fade
        elif e == "slide_right":      # comes in from the left
            out["dx"], out["alpha"] = -(1 - p) * 0.38 * W, fade
        elif e == "slide_up":         # rises from below
            out["dy"], out["alpha"] = (1 - p) * 0.35 * H, fade
        elif e == "slide_down":       # drops from above
            out["dy"], out["alpha"] = -(1 - p) * 0.35 * H, fade
        elif e == "pull_down":        # drops in and settles with a slight overshoot
            out["dy"], out["alpha"] = -(1 - ease_out_back(u / d)) * 0.4 * H, fade
        elif e == "zoom_in":
            out["scale"], out["alpha"] = 0.80 + 0.20 * p, ease_in_out(u / d)
        elif e == "fit_in_frame":     # starts oversized, shrinks into place
            out["scale"], out["alpha"] = 1.28 - 0.28 * p, fade
        elif e == "blur_in":
            out["blur"], out["alpha"] = (1 - p) * 22 * self.fmt.unit, fade
        elif e == "swing_right":
            out["rot"], out["dx"], out["alpha"] = -11 * (1 - p), -(1 - p) * 0.06 * W, ease_in_out(u / d)
        elif e == "whip_in":       # fast push: sweeps in from the right, settles in 0.25s
            out["dx"], out["scale"], out["alpha"] = (1 - p) * 0.22 * W, 1.22 - 0.22 * p, fade
        return out

    def sprite(self, t: float, extra_zoom: float = 0.0) -> Image.Image:
        x0, y0, vw, vh = self._viewport(t, extra_zoom)
        content = self.src.transform((self.iw, self.ih), Image.AFFINE,
                                     (vw / self.iw, 0.0, x0, 0.0, vh / self.ih, y0), resample=Image.BILINEAR)
        spr = self.base.copy()
        off = self.margin + self.border
        spr.paste(content, (off, off), self.inner_mask)
        return spr


# ------------------------------------------------------------------ effects


def shake_offset(t: float, times: List[float], unit: float) -> Tuple[float, float]:
    """A damped wobble, not random jitter: reads as weight, not glitch."""
    for te in times:
        dt = t - te
        if 0 <= dt < SHAKE_LEN:
            amp = 10 * unit * math.exp(-5.5 * dt)
            return amp * math.sin(2 * math.pi * 7 * dt), 0.5 * amp * math.cos(2 * math.pi * 5 * dt)
    return 0.0, 0.0


def punch_amount(t: float, times: List[float]) -> float:
    best = 0.0
    for te in times:
        dt = t - te
        if 0 <= dt < 0.25:
            best = max(best, ease_out(dt / 0.25))
        elif 0.25 <= dt < PUNCH_LEN:
            best = max(best, 1 - ease_in_out((dt - 0.25) / (PUNCH_LEN - 0.25)))
    return 0.04 * best


def flash_alpha(t: float, times: List[float]) -> float:
    best = 0.0
    for te in times:
        dt = t - te
        if 0 <= dt < 0.12:
            best = max(best, 0.55 * dt / 0.12)
        elif 0.12 <= dt < FLASH_LEN:
            best = max(best, 0.55 * (1 - (dt - 0.12) / (FLASH_LEN - 0.12)))
    return best



def glitch_offset(t: float, times: List[float], unit: float) -> int:
    """RGB-split offset in pixels at time t (0 when no glitch is active).

    A glitch lasts GLITCH_LEN (0.2s) and decays linearly: the split is
    widest on the beat, then settles. It shifts colour channels, never
    covers a face: no pixels are hidden, only separated by a few px.
    """
    best = 0.0
    for te in times:
        dt = t - te
        if 0 <= dt < GLITCH_LEN:
            best = max(best, 1.0 - dt / GLITCH_LEN)
    return max(0, int(round(8 * unit * best)))


def glitch_split(arr: np.ndarray, px: int) -> np.ndarray:
    """RGB split: R shifts left, B shifts right by px; G stays. Edges repeat
    (no wrap-around, no black bars). Returns a copy; input untouched.
    """
    if px <= 0:
        return arr
    h, w = arr.shape[:2]
    px = min(px, max(1, w // 8))
    out = arr.copy()
    out[:, :w - px, 0] = arr[:, px:, 0]
    out[:, w - px:, 0] = arr[:, -1:, 0]
    out[:, px:, 2] = arr[:, :w - px, 2]
    out[:, :px, 2] = arr[:, :1, 2]
    return out


def grade_subtle(arr: np.ndarray) -> np.ndarray:
    """Gentle grade: contrast +5%, saturation +5%. Subtle by design --
    the art still looks like the art, just a touch more present.
    """
    f = arr.astype(np.float32)
    f = (f - 128.0) * 1.05 + 128.0
    gray = f.mean(axis=2, keepdims=True)
    f = gray + (f - gray) * 1.05
    return np.clip(f, 0, 255).astype(np.uint8)
# ------------------------------------------------------------------ P3 overlays

# P4: truetype() re-reads + parses the font file on every call; a callout
# active for 1.2s would reload it ~36x. Fonts are read-only after load.
@lru_cache(maxsize=16)
def _overlay_font(size: int):
    """Bold sans-serif for callouts / chapter cards, with a plain fallback.

    DejaVuSans-Bold ships with most Pillow installs; when it is absent the
    default bitmap font still renders (smaller, but never crashes a render).
    """
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _draw_callout(img: Image.Image, callout: dict, fmt: "Format", t: float, caption_bar: bool = False) -> None:
    """One keyword callout: bottom banner, max 2 lines, never over faces.

    Faces sit in the upper part of a card; the banner sits in the bottom
    14% of the frame (lower-third), where cards show torso / background.
    `keyword` style is yellow; other styles are white. No-op outside the
    callout's [t, t+dur) window or when the text is empty.
    """
    try:
        start = float(callout.get("t") or 0)
        dur = float(callout.get("dur") or 1.2)
    except (TypeError, ValueError):
        return
    if not start <= t < start + dur:
        return
    text = str(callout.get("text") or "").strip()
    if not text:
        return
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:2]
    if not lines:
        return
    W, H = fmt.width, fmt.height
    u = fmt.unit
    font = _overlay_font(max(10, int(44 * u)))
    draw = ImageDraw.Draw(img, "RGBA")
    lh = max(10, int(58 * u))
    band_h = lh * len(lines) + int(18 * u)
    # With a caption bar, the banner sits above the bar (its bottom edge
    # is the bar's top), never over the captions.
    _bottom = int(H * CAPTION_BAR_TOP_FRACTION) if caption_bar else H
    y0 = _bottom - band_h - int(8 * u)
    draw.rectangle((0, y0, W, _bottom), fill=(0, 0, 0, 140))
    fill = (255, 214, 90) if callout.get("style") == "keyword" else (255, 255, 255)
    for i, ln in enumerate(lines):
        w = draw.textlength(ln, font=font)
        draw.text(((W - w) / 2, y0 + int(9 * u) + i * lh), ln, font=font, fill=fill,
                  stroke_width=max(1, int(3 * u)), stroke_fill=(0, 0, 0))


def _draw_chapter_card(img: Image.Image, card: dict, fmt: "Format", t: float) -> bool:
    """A chapter card: full-screen dark overlay + centred stakes title.

    Returns True when a card was active at t (the overlay covers the panels
    for its 1.5-2s window — a deliberate pattern interrupt, not a glitch).
    """
    try:
        start = float(card.get("t") or 0)
        dur = float(card.get("dur") or 1.75)
    except (TypeError, ValueError):
        return False
    if not start <= t < start + dur:
        return False
    title = str(card.get("title") or "").strip()
    if not title:
        return False
    W, H = fmt.width, fmt.height
    u = fmt.unit
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 150))
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(img)
    font = _overlay_font(max(12, int(72 * u)))
    w = draw.textlength(title, font=font)
    draw.text(((W - w) / 2, H / 2 - int(36 * u)), title, font=font, fill=(255, 255, 255),
              stroke_width=max(1, int(4 * u)), stroke_fill=(0, 0, 0))
    rule = max(2, int(4 * u))
    draw.rectangle(((W - w) / 2, H / 2 + int(40 * u), (W + w) / 2, H / 2 + int(40 * u) + rule),
                   fill=(255, 214, 90))
    return True


# ------------------------------------------------------------------ shot


def _mix(a: np.ndarray, b: np.ndarray, k: float) -> np.ndarray:
    """(1-k)*a + k*b in integer math; far faster than float on 1080p frames."""
    w = int(round(max(0.0, min(1.0, k)) * 256))
    return ((a.astype(np.uint16) * (256 - w) + b.astype(np.uint16) * w) >> 8).astype(np.uint8)


class ShotRenderer:
    """Draws one shot at any time t (seconds on the video timeline)."""

    def __init__(self, shot: Dict[str, Any], images: Dict[str, Image.Image], fmt: Format,
                 effects_on: Dict[str, bool] | None = None, video_end: float | None = None,
                 end_fade: bool = False, chapter_cards: List[Dict[str, Any]] | None = None):
        # P3: shot-level callouts ride on the shot dict; plan-level chapter
        # cards are passed in (render.py) so a clip can shift them with the shot.
        self.caption_bar = bool(shot.get("caption_bar"))
        self.callouts = [c for c in (shot.get("callouts") or []) if isinstance(c, dict)]
        self.chapter_cards = list(chapter_cards or [])
        self.fmt = fmt
        self.shot = shot
        self.start, self.end = float(shot["start"]), float(shot["end"])
        layout = shot.get("layout") if shot.get("layout") in LAYOUTS else "single"
        cards = [c for c in shot.get("cards") or [] if c.get("panel") in images]
        if layout == "pair" and len(cards) < 2:
            layout = "single"
        cards = cards[:2] if layout == "pair" else cards[:1]
        self.layout = layout
        pair_move_at = None
        if layout == "pair":
            # A clip that starts after the second card arrived carries the real
            # (negative) arrival time here, so the first card is already in place.
            move = shot.get("pair_move_at", cards[1].get("at"))
            pair_move_at = self.start + float(move or 0.0)
        # Defect 2: pair_move runs at most once per pair shot. A shot that
        # already played its pair move (re-render / clip with pair_move_done)
        # starts with the first card already in place -- the slide never re-triggers.
        if shot.get("pair_move_done"):
            pair_move_at = self.start - PAIR_MOVE
        self.cards = [Card(images[c["panel"]], c, layout, i, self.start, self.end, fmt, pair_move_at)
                      for i, c in enumerate(cards)]

        bg_kind = shot.get("background") if shot.get("background") in BACKGROUNDS else "blur"
        bg_panel = shot.get("bg_panel") if shot.get("bg_panel") in images else (cards[0]["panel"] if cards else None)
        # P4: cross-shot bg cache; bg_panel id is the shot's panel id (None
        # keeps the old behaviour for shots without one).
        self.bg = _cached_background(bg_kind, images.get(bg_panel) if bg_panel else None,
                                     fmt, bg_panel)
        # P4: Image.fromarray(self.bg) per frame re-validates + copies the 6MB
        # backdrop; one PIL copy per frame from a cached PIL image is cheaper.
        self._bg_pil = Image.fromarray(self.bg)

        on = {"shake": True, "punch": False, "flash": False}
        on.update({k: bool(v) for k, v in (effects_on or {}).items()})
        fx = [e for e in shot.get("effects") or [] if isinstance(e, dict) and on.get(e.get("type"))]
        self.shakes = [float(e["t"]) for e in fx if e["type"] == "shake"]
        self.punches = [float(e["t"]) for e in fx if e["type"] == "punch"]
        self.flashes = [float(e["t"]) for e in fx if e["type"] == "flash"]
        self.glitches = [float(e["t"]) for e in fx if e["type"] == "glitch"]
        # CapCut grade: "subtle" only, off by default (planner sets it from
        # video.grade). Anything else is ignored, never a surprise grade.
        self.grade = shot.get("grade") if shot.get("grade") == "subtle" else None

        try:     # a darker grade for the whole shot (the intro preset sets it)
            self.darken = max(0.0, min(0.6, float(shot.get("darken") or 0.0)))
        except (TypeError, ValueError):
            self.darken = 0.0
        self.exit = shot.get("exit") if shot.get("exit") in EXITS else "cut"
        self.fade_from = self.end - EXIT_FADE if self.exit == "fade_out" else None
        if end_fade and video_end is not None and abs(video_end - self.end) < 0.05:
            self.fade_from = min(self.fade_from or 1e9, self.end - 1.0)
        if self.fade_from is not None:
            self.fade_from = max(self.start, self.fade_from)  # a short last shot must not start dark

        # From this moment until any exit fade, every frame is identical.
        settle = max([c.settled_at() for c in self.cards] + [self.start])
        for times, length in ((self.shakes, SHAKE_LEN), (self.punches, PUNCH_LEN),
                                (self.flashes, FLASH_LEN), (self.glitches, GLITCH_LEN)):
            for te in times:
                settle = max(settle, te + length)
        self.static_from = settle
        # Defect 1: only a shot whose every card is a hand-set `hold` may
        # freeze. Continuous-motion shots must be re-drawn every frame.
        self._can_freeze = all(c.motion == "hold" for c in self.cards)
        self._static: np.ndarray | None = None

    def is_static(self, t: float) -> bool:
        return self._can_freeze and t >= self.static_from and (self.fade_from is None or t < self.fade_from)

    def pair_move_done(self, t: float) -> bool:
        """True once every card's pair slide has finished (pair_move fires once)."""
        return all(c.pair_move_done(t) for c in self.cards)

    def frame_array(self, t: float) -> np.ndarray:
        if self.is_static(t) and self._static is not None:
            return self._static
        img = self._bg_pil.copy()
        if self.caption_bar:
            # Dedicated caption bar: solid black below the cards, so
            # captions (burned in later by ffmpeg) sit on black, never
            # over panel art.
            _bar_top = int(self.fmt.height * CAPTION_BAR_TOP_FRACTION)
            ImageDraw.Draw(img).rectangle((0, _bar_top, self.fmt.width, self.fmt.height), fill=(0, 0, 0))
        dx_s, dy_s = shake_offset(t, self.shakes, self.fmt.unit)
        extra = punch_amount(t, self.punches)
        for card in self.cards:
            if not card.visible(t):
                continue
            spr = card.sprite(t, extra)
            ent = card.entrance(t)
            if ent["blur"] > 0.3:
                spr = spr.filter(ImageFilter.GaussianBlur(ent["blur"]))
            if abs(ent["scale"] - 1) > 1e-3:
                spr = spr.resize((max(2, int(spr.width * ent["scale"])), max(2, int(spr.height * ent["scale"]))),
                                 Image.BILINEAR)
            if abs(ent["rot"]) > 0.05:
                spr = spr.rotate(ent["rot"], resample=Image.BILINEAR, expand=True)
            alpha = spr.getchannel("A")
            if ent["alpha"] < 0.999:
                a = ent["alpha"]
                alpha = alpha.point(lambda v, a=a: int(v * a))
            cx, cy = card.centre(t)
            x = int(round(cx + ent["dx"] + dx_s - spr.width / 2))
            y = int(round(cy + ent["dy"] + dy_s - spr.height / 2))
            img.paste(spr.convert("RGB"), (x, y), alpha)
        for _co in self.callouts:
            _draw_callout(img, _co, self.fmt, t, caption_bar=self.caption_bar)
        for _cc in self.chapter_cards:
            _draw_chapter_card(img, _cc, self.fmt, t)
        out = np.asarray(img, dtype=np.uint8)
        if self.darken > 0:
            out = (out.astype(np.float32) * (1.0 - self.darken)).astype(np.uint8)
        if self.grade == "subtle":
            out = grade_subtle(out)
        _glitch = glitch_offset(t, self.glitches, self.fmt.unit)
        if _glitch > 0:
            out = glitch_split(out, _glitch)
        fa = flash_alpha(t, self.flashes)
        if fa > 0.001:
            out = _mix(out, np.full_like(out, 255), fa)
        if self.fade_from is not None and t > self.fade_from:
            span = max(1e-3, self.end - self.fade_from)
            out = _mix(np.zeros_like(out), out, max(0.0, (self.end - t) / span))
        if self.is_static(t):
            self._static = out
        return out

    def frame(self, t: float) -> Image.Image:
        return Image.fromarray(self.frame_array(t))
