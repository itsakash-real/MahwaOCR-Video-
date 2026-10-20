"""Cinematic video mode: full-screen panels, no cards.

Layout grammar reverse-engineered from the Frieren Manhwa viral recap
(1.28M views; browser-agent report + screenshots, 2026-10-20;
docs/VIDEO_STYLE_CINEMATIC.md):

- Full-bleed action art edge-to-edge is the default frame.
- Tall panels sit centred on a blurred, enlarged copy of themselves with
  the sides DARKER than the panel. No borders, no rounded corners, no
  letterbox bars.
- Hard cuts only (a brief black flash may separate sections). Slow
  push-in / pan over still art inside a shot.
- No subtitles, no channel branding, no grade/vignette/grain.
- Large bold white name caption with dark outline, lower third, on
  character introduction.
- Large white outlined callout (e.g. "172?") upper-right for emphasis
  beats. Art-embedded SFX stays in the art, never as overlays.
- Occasionally two tall panels sit side by side on the blurred
  background.
- Cutting is fast: median ~1.77s, mean ~1.87s, as short as ~0.33s in
  rapid sequences. Cuts are uniform, not tied to scene energy.

The classic card renderer (frames.ShotRenderer) is untouched; render.py
picks this module's CinematicShot when the plan shot carries
``"cinematic": True``.
"""

from __future__ import annotations

from typing import Dict, Any, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

LAYOUT_TALL = "tall"            # full-height, centred, darkened pillars
LAYOUT_FULLBLEED = "fullbleed"  # cover the whole frame
LAYOUT_PAIR = "pair"            # two tall panels side by side on dark pillars

# A panel this wide-or-wider (w/h) goes full-bleed; narrower goes tall.
FULLBLEED_ASPECT = 1.35
# Heavy blur for the pillar fill, in pixels at 1080p.
PILLAR_BLUR = 48
# Pillars are DARKER than the panel (report + screenshots): the blurred
# copy is dimmed to a bit over half brightness, never brightened.
PILLAR_BRIGHTNESS = 0.55
# Feather (px) blending the pillar into the panel edge.
PILLAR_FEATHER = 24
# A black flash separating sections lasts this long (seconds).
BLACK_FLASH_LEN = 0.12


def layout_for(aspect: float) -> str:
    """Pick the cinematic layout for a panel aspect ratio (w/h)."""
    try:
        a = float(aspect)
    except (TypeError, ValueError):
        return LAYOUT_TALL
    return LAYOUT_FULLBLEED if a >= FULLBLEED_ASPECT else LAYOUT_TALL


def _bold_font(size: int):
    """Big bold sans for name cards / callouts, same fallback as frames.py."""
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def dark_pillars(panel: Image.Image, width: int, height: int) -> Image.Image:
    """Side fill for a tall panel: the panel scaled to cover, heavily
    blurred, then DARKENED. The reference keeps the sides clearly darker
    than the panel itself."""
    cover = panel.copy()
    scale = max(width / cover.width, height / cover.height)
    cover = cover.resize((max(1, int(cover.width * scale)),
                          max(1, int(cover.height * scale))), Image.BILINEAR)
    x = (cover.width - width) // 2
    y = (cover.height - height) // 2
    cover = cover.crop((x, y, x + width, y + height))
    cover = cover.filter(ImageFilter.GaussianBlur(PILLAR_BLUR))
    arr = np.asarray(cover).astype(np.float32) * PILLAR_BRIGHTNESS
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


# Backwards-compatible alias (v1 name).
luminous_pillars = dark_pillars


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
    """Lower-third character name: big bold white text, dark outline.

    Shown ~2s on a named character's first appearance (the plan sets
    ``shot["name_card"]``); the renderer fades it with ``alpha``.
    """
    if not name or alpha <= 0.01:
        return
    w, h = img.size
    size = max(28, int(h * 0.085))
    font = _bold_font(size)
    draw = ImageDraw.Draw(img, "RGBA")
    bbox = draw.textbbox((0, 0), name, font=font, stroke_width=max(2, size // 16))
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = (w - tw) / 2 - bbox[0], h * 0.80 - th / 2 - bbox[1]
    a = int(255 * max(0.0, min(1.0, alpha)))
    draw.text((x, y), name, font=font, fill=(255, 255, 255, a),
              stroke_width=max(2, size // 16), stroke_fill=(0, 0, 0, a))


def draw_callout(img: Image.Image, text: str, alpha: float = 1.0) -> None:
    """Upper-right emphasis callout (e.g. "172?"): large bold white text
    with a dark outline, popped in over the art."""
    if not text or alpha <= 0.01:
        return
    w, h = img.size
    size = max(28, int(h * 0.11))
    font = _bold_font(size)
    draw = ImageDraw.Draw(img, "RGBA")
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=max(2, size // 14))
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # Upper right with a small margin; scale the pop with alpha.
    pop = 0.85 + 0.15 * max(0.0, min(1.0, alpha))
    x, y = w - tw * pop - w * 0.06 - bbox[0], h * 0.08 - bbox[1]
    a = int(255 * max(0.0, min(1.0, alpha)))
    draw.text((x, y), text, font=font, fill=(255, 255, 255, a),
              stroke_width=max(2, size // 14), stroke_fill=(0, 0, 0, a))


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
      panel: looked up in ``images`` via cards[0] (or cards[0:2] for "pair")
      cinematic_layout: "tall" | "fullbleed" | "pair"
      zoom_dir: +1 push-in | -1 pull-back (plan alternates; default +1)
      zoom: total zoom amount over the shot (default 0.06)
      focus: (fx, fy) focal point the zoom centres on (default 0.5, 0.5)
      name_card: character name for the lower-third overlay (or None)
      name_card_at: seconds into the shot when the card appears (default 0.15)
      callout: emphasis text for the upper-right overlay (or None)
      callout_at: seconds into the shot when the callout pops (default 0.3)
      black_flash_in: True to open the shot with a brief black flash
    """

    def __init__(self, shot: Dict[str, Any], images: Dict[str, Image.Image],
                 fmt, zoom: float = 0.06):
        self.shot = shot
        self.fmt = fmt
        self.start, self.end = float(shot["start"]), float(shot["end"])
        cards = shot.get("cards") or []
        pids = [c.get("panel") for c in cards if c.get("panel") in images]
        if not pids and images:
            pids = [next(iter(images))]
        self.panels = [images[pid].convert("RGB") for pid in pids]
        if not self.panels:
            raise ValueError("CinematicShot needs at least one panel image")
        _cl = shot.get("cinematic_layout")
        if _cl in (LAYOUT_TALL, LAYOUT_FULLBLEED, LAYOUT_PAIR):
            self.layout = _cl
        elif len(self.panels) >= 2:
            self.layout = LAYOUT_PAIR
        else:
            aspect = self.panels[0].width / max(1, self.panels[0].height)
            self.layout = layout_for(aspect)
        self.zoom_dir = -1 if shot.get("zoom_dir") == -1 else 1
        self.zoom = float(shot.get("zoom", zoom))
        fx, fy = shot.get("focus") or (0.5, 0.5)
        self.focus = (float(fx), float(fy))
        self.name_card = shot.get("name_card")
        self.name_card_at = float(shot.get("name_card_at", 0.15))
        self.callout = shot.get("callout")
        self.callout_at = float(shot.get("callout_at", 0.3))
        self.black_flash_in = bool(shot.get("black_flash_in"))
        self._base: Image.Image | None = None

    # -- composition -----------------------------------------------------
    def _compose(self) -> Image.Image:
        """The still frame before motion."""
        w, h = self.fmt.width, self.fmt.height
        if self._base is not None:
            return self._base
        if self.layout == LAYOUT_FULLBLEED:
            panel = self.panels[0]
            scale = max(w / panel.width, h / panel.height)
            base = panel.resize((max(1, int(panel.width * scale)),
                                 max(1, int(panel.height * scale))), Image.BILINEAR)
            x = (base.width - w) // 2
            y = (base.height - h) // 2
            base = base.crop((x, y, x + w, y + h))
        elif self.layout == LAYOUT_PAIR and len(self.panels) >= 2:
            base = dark_pillars(self.panels[0], w, h)
            # Two tall panels side by side with a gap, like the reference.
            gap = int(w * 0.04)
            pw = (w - gap * 3) // 2
            pair = []
            for panel in self.panels[:2]:
                scale = h / panel.height
                pair.append(panel.resize((max(1, int(panel.width * scale)), h),
                                         Image.BILINEAR))
            # If a panel is wider than its slot, centre-crop it.
            pair = [p if p.width <= pw else p.crop(((p.width - pw) // 2, 0,
                                                   (p.width + pw) // 2, h))
                    for p in pair]
            x = gap + (pw - pair[0].width) // 2
            for p in pair:
                _paste_feathered(base, p, x)
                x += pw + gap
        else:
            panel = self.panels[0]
            base = dark_pillars(panel, w, h)
            scale = h / panel.height
            pw, ph = max(1, int(panel.width * scale)), h
            tall = panel.resize((pw, ph), Image.BILINEAR)
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
            img = base.copy()
        # Name card: fade in fast, hold, fade out over its ~2s window.
        if self.name_card:
            lt = t - self.start - self.name_card_at
            if 0 <= lt <= 2.0:
                alpha = min(1.0, lt / 0.25, (2.0 - lt) / 0.35)
                draw_name_card(img, self.name_card, alpha)
        # Callout: pop in fast, hold ~1.2s.
        if self.callout:
            lt = t - self.start - self.callout_at
            if 0 <= lt <= 1.5:
                alpha = min(1.0, lt / 0.12, (1.5 - lt) / 0.3)
                draw_callout(img, self.callout, alpha)
        out = np.asarray(img, dtype=np.uint8)
        # Black flash at section breaks (brief fade from black).
        if self.black_flash_in:
            lt = t - self.start
            if 0 <= lt < BLACK_FLASH_LEN:
                out = (out.astype(np.float32) * (lt / BLACK_FLASH_LEN)).astype(np.uint8)
        return out

    def frame(self, t: float) -> Image.Image:
        return Image.fromarray(self.frame_array(t))
