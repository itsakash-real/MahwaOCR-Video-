"""Cinematic video mode: full-screen panels, no cards.

Layout grammar reverse-engineered from viral manhwa recaps
(docs/VIDEO_STYLE_CINEMATIC.md):

- Tall panels are shown full-frame-height, centred, edge-to-edge vertically.
  The side pillars are the panel itself scaled-to-cover + heavy blur, kept
  luminous (no dark vignette, no black/white bars).
- Wide panels go full-bleed: they cover the entire 16:9 frame.
- Hard cuts only; a slow continuous zoom runs inside every shot.
- No caption bar, no card borders, no floating boxes.

The classic card renderer (frames.ShotRenderer) is untouched; render.py
picks this module's CinematicShot when the plan shot carries
``"cinematic": True``.
"""

from __future__ import annotations

from typing import Dict, Any, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

LAYOUT_TALL = "tall"            # full-height, centred, luminous pillars
LAYOUT_FULLBLEED = "fullbleed"  # cover the whole frame

# A panel this wide-or-wider (w/h) goes full-bleed; narrower goes tall.
FULLBLEED_ASPECT = 1.35
# Heavy blur for the pillar fill, in pixels at 1080p.
PILLAR_BLUR = 48
# Pillars stay luminous: multiplied by this (1.0 = panel brightness).
PILLAR_BRIGHTNESS = 0.92
# Feather (px) blending the pillar into the panel edge.
PILLAR_FEATHER = 24


def layout_for(aspect: float) -> str:
    """Pick the cinematic layout for a panel aspect ratio (w/h)."""
    try:
        a = float(aspect)
    except (TypeError, ValueError):
        return LAYOUT_TALL
    return LAYOUT_FULLBLEED if a >= FULLBLEED_ASPECT else LAYOUT_TALL


def _name_font(size: int):
    """Big bold sans for name cards, same fallback chain as frames.py."""
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def luminous_pillars(panel: Image.Image, width: int, height: int) -> Image.Image:
    """Side fill for a tall panel: the panel scaled to cover, heavily blurred,
    kept bright. Never darkened, never vignetted."""
    cover = panel.copy()
    # Scale to cover the frame, then crop.
    scale = max(width / cover.width, height / cover.height)
    cover = cover.resize((max(1, int(cover.width * scale)),
                          max(1, int(cover.height * scale))), Image.BILINEAR)
    x = (cover.width - width) // 2
    y = (cover.height - height) // 2
    cover = cover.crop((x, y, x + width, y + height))
    cover = cover.filter(ImageFilter.GaussianBlur(PILLAR_BLUR))
    if PILLAR_BRIGHTNESS != 1.0:
        arr = np.asarray(cover).astype(np.float32) * PILLAR_BRIGHTNESS
        cover = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    return cover


def _paste_feathered(base: Image.Image, panel: Image.Image, x: int) -> None:
    """Paste the full-height panel into the pillar base with a soft edge."""
    mask = Image.new("L", (panel.width, panel.height), 255)
    if PILLAR_FEATHER > 0 and panel.width > 2 * PILLAR_FEATHER:
        fade = Image.new("L", (PILLAR_FEATHER, panel.height), 0)
        for i in range(PILLAR_FEATHER):
            v = int(255 * (i + 1) / PILLAR_FEATHER)
            ImageDraw.Draw(fade).line([(i, 0), (i, panel.height)], fill=v)
        mask.paste(fade, (0, 0))
        mask.paste(fade.transpose(Image.FLIP_LEFT_RIGHT),
                   (panel.width - PILLAR_FEATHER, 0))
    base.paste(panel, (x, 0), mask)


def draw_name_card(img: Image.Image, name: str, alpha: float = 1.0) -> None:
    """Lower-third character name overlay: big white text, black outline.

    Shown ~2s on a named character's first appearance (the plan sets
    ``shot["name_card"]``); the renderer fades it with ``alpha``.
    """
    if not name or alpha <= 0.01:
        return
    w, h = img.size
    size = max(24, int(h * 0.075))
    font = _name_font(size)
    draw = ImageDraw.Draw(img, "RGBA")
    # Centre the text in the lower third.
    bbox = draw.textbbox((0, 0), name, font=font, stroke_width=max(2, size // 18))
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = (w - tw) / 2 - bbox[0], h * 0.80 - th / 2 - bbox[1]
    a = int(255 * max(0.0, min(1.0, alpha)))
    draw.text((x, y), name, font=font, fill=(255, 255, 255, a),
              stroke_width=max(2, size // 18), stroke_fill=(0, 0, 0, a))


def _zoom_rect(w: int, h: int, cx: float, cy: float, zoom: float) -> Tuple[int, int, int, int]:
    """Crop box of size (w,h)/zoom centred on (cx,cy), clamped to the image."""
    cw, ch = w / zoom, h / zoom
    x = min(max(cx - cw / 2, 0), w - cw)
    y = min(max(cy - ch / 2, 0), h - ch)
    return int(x), int(y), int(x + cw), int(y + ch)


class CinematicShot:
    """Draws one cinematic shot at any time t (seconds on the timeline).

    Same interface as frames.ShotRenderer (frame_array / frame) so render.py
    can swap it in per shot. The shot dict carries:
      panel: PIL image id -> looked up in ``images``
      layout: "tall" | "fullbleed" (plan-computed; recomputed from aspect if missing)
      zoom_dir: +1 push-in | -1 pull-back (plan alternates; default +1)
      zoom: total zoom amount over the shot (default 0.06)
      name_card: character name for the lower-third overlay (or None)
      name_card_at: seconds into the shot when the card appears (default 0.15)
    """

    def __init__(self, shot: Dict[str, Any], images: Dict[str, Image.Image],
                 fmt, zoom: float = 0.06):
        self.shot = shot
        self.fmt = fmt
        self.start, self.end = float(shot["start"]), float(shot["end"])
        pid = (shot.get("cards") or [{}])[0].get("panel") or shot.get("panel")
        panel = images.get(pid) if pid else None
        if panel is None and images:
            panel = next(iter(images.values()))
        if panel is None:
            raise ValueError("CinematicShot needs at least one panel image")
        self.panel = panel.convert("RGB")
        aspect = self.panel.width / max(1, self.panel.height)
        _cl = shot.get("cinematic_layout")
        self.layout = _cl if _cl in (LAYOUT_TALL, LAYOUT_FULLBLEED) else layout_for(aspect)
        self.zoom_dir = -1 if shot.get("zoom_dir") == -1 else 1
        self.zoom = float(shot.get("zoom", zoom))
        fx, fy = shot.get("focus") or (0.5, 0.5)
        self.focus = (float(fx), float(fy))
        self.name_card = shot.get("name_card")
        self.name_card_at = float(shot.get("name_card_at", 0.15))
        self._base: Image.Image | None = None

    # -- composition -----------------------------------------------------
    def _compose(self) -> Image.Image:
        """The still frame before motion: pillars or full-bleed."""
        w, h = self.fmt.width, self.fmt.height
        if self._base is not None:
            return self._base
        if self.layout == LAYOUT_FULLBLEED:
            base = self.panel.copy()
            scale = max(w / base.width, h / base.height)
            base = base.resize((max(1, int(base.width * scale)),
                                max(1, int(base.height * scale))), Image.BILINEAR)
            x = (base.width - w) // 2
            y = (base.height - h) // 2
            base = base.crop((x, y, x + w, y + h))
        else:
            base = luminous_pillars(self.panel, w, h)
            scale = h / self.panel.height
            pw, ph = max(1, int(self.panel.width * scale)), h
            tall = self.panel.resize((pw, ph), Image.BILINEAR)
            _paste_feathered(base, tall, (w - pw) // 2)
        self._base = base
        return base

    def _zoom_at(self, t: float) -> float:
        dur = max(1e-3, self.end - self.start)
        p = max(0.0, min(1.0, (t - self.start) / dur))
        # ease-in-out so the motion never feels mechanical
        p = p * p * (3 - 2 * p)
        if self.zoom_dir < 0:
            return 1.0 + self.zoom * (1.0 - p)
        return 1.0 + self.zoom * p

    # -- public interface --------------------------------------------------
    def frame_array(self, t: float) -> np.ndarray:
        base = self._compose()
        w, h = base.size
        z = self._zoom_at(t)
        if abs(z - 1.0) > 1e-4:
            cx, cy = w * self.focus[0], h * self.focus[1]
            box = _zoom_rect(w, h, cx, cy, z)
            img = base.crop(box).resize((w, h), Image.BILINEAR)
        else:
            img = base
        # Name card: fade in fast, hold, fade out over its ~2s window.
        if self.name_card:
            lt = t - self.start - self.name_card_at
            if 0 <= lt <= 2.0:
                alpha = min(1.0, lt / 0.25, (2.0 - lt) / 0.35)
                draw_name_card(img, self.name_card, alpha)
        return np.asarray(img, dtype=np.uint8)

    def frame(self, t: float) -> Image.Image:
        return Image.fromarray(self.frame_array(t))
