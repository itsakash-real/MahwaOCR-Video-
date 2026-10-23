"""Title Card Generator — the reference channel's time-skip device.

Measured from the reference video (see
~/workspace/your_files/reference-video-measured-spec.md): at story
time-skips the edit hard-cuts to a full-frame BLACK card carrying a
split-flap counter — a small serif label ("Age") over two beige flap
tiles with zero-padded digits (the tiles+digits block measures 33% of
frame height) — and one bold condensed ALL-CAPS line under it ("WEEK 6
OF POSSESSION.", 12.2% of frame height, ~58% of frame width). Four
cards in 201s, always on a timeline cue in the narration.

This module is the whole device, hardcoded like every other style rule
in the codebase (the user's rule: style lives in code, never in an
agent's head):

* `detect_title_cue(text)` parses one narration sentence into
  (label, number, line) or None. It fires ONLY on cue-shaped sentences:
  a sentence that OPENS with "Age <n>", a "<n> <unit> later" opener, or
  a "<Unit> <n> ..." opener. Numbers may be digits or number-words
  (recaps spell numbers out for TTS). Ordinary sentences that merely
  mention an age, a week or a count never trigger — the timeline
  keyword must carry the sentence.
* `card_duration(span)` clamps a cue's spoken span to [1.4, 2.1]s
  (the reference's four measured cards run 1.40-2.13s; B1d F6).
* `render_title_card(w, h, label, number, line)` draws the card with
  PIL: black field, serif label, two split-flap tiles (rounded beige
  tiles, centre seam, dark numerals), and the big line in Anton (the
  bundled OFL font; metric fallback Liberation/DejaVu Sans Bold).

The planner (plan.insert_title_cards) turns cue sentences into card
shots; the renderer (frames.ShotRenderer) draws them through
render_title_card. Nothing here knows about panels or audio.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from PIL import Image, ImageDraw, ImageFont

# Card timing: the reference's measured card window (1.40-2.13s on
# screen); the plan clamps the cue sentence's own duration into it.
CARD_MIN_DUR = 1.4
CARD_MAX_DUR = 2.1

# Measured geometry (fractions of frame height unless noted).
DIGITS_BLOCK_H = 0.33        # tiles + digits block height / frame height
TILE_W_RATIO = 0.82          # one tile's width / its height
TILE_GAP_RATIO = 0.055       # gap between the tiles / tile height
LABEL_SIZE = 0.052           # serif label font size / frame height
LABEL_GAP = 0.030            # label baseline to tiles top / frame height
LINE_SIZE = 0.14             # big-line font size / frame height (shrinks to fit);
                             # the RENDERED ink measures ~12.2% of frame height at
                             # this size (B1d F6; at 0.105 it measured only 9.3%)
LINE_GAP = 0.048             # tiles bottom to big line top / frame height
LINE_MAX_W = 0.80            # big line never wider than this share of frame width
                             # (raised from 0.66 with LINE_SIZE: the shrink-to-fit
                             # loop must not pull the line back under its measured
                             # height; Anton is wider than the reference's font,
                             # so height is the matched dimension)
LINE_TRACKING = 0.035        # letter tracking, share of the line font size

TILE_FILL = (219, 208, 182)  # split-flap beige
TILE_EDGE = (122, 111, 88)
TILE_SEAM = (88, 79, 60)
DIGIT_FILL = (28, 28, 30)
LABEL_FILL = (238, 238, 238)
LINE_FILL = (255, 255, 255)

_UNITS = ("week", "month", "year", "day")
_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
         "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
         "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
         "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_ONES_ALT = "|".join(sorted(_ONES, key=len, reverse=True))
_TENS_ALT = "|".join(sorted(_TENS, key=len, reverse=True))
_SMALL_ALT = "|".join(sorted((k for k in _ONES if _ONES[k] < 10), key=len, reverse=True))
_NUMWORD = (r"(?:\d{1,3}|(?:" + _ONES_ALT + r")(?:[-\s](?:" + _ONES_ALT + r"))?|"
            r"(?:" + _TENS_ALT + r")(?:[-\s](?:" + _SMALL_ALT + r"))?)")
_AGE_RE = re.compile(r"^age\s+(" + _NUMWORD + r")\b", re.IGNORECASE)
_UNIT_OF_RE = re.compile(r"\b(week|month|year|day)s?\s+(" + _NUMWORD + r")\s+of\s+[^.!?;]*",
                         re.IGNORECASE)
_UNIT_START_RE = re.compile(r"^(week|month|year|day)s?\s+(" + _NUMWORD + r")\b[^.!?;]*",
                            re.IGNORECASE)
_LATER_RE = re.compile(r"^(" + _NUMWORD + r")\s+(weeks?|months?|years?|days?)\s+later\b",
                       re.IGNORECASE)


def _number(tok: str) -> Optional[int]:
    """'4' / 'four' / 'twenty-four' / 'twenty four' -> int, else None."""
    tok = (tok or "").strip().lower()
    if tok.isdigit():
        return int(tok)
    parts = re.split(r"[-\s]+", tok)
    if len(parts) == 1:
        if parts[0] in _ONES:
            return _ONES[parts[0]]
        if parts[0] in _TENS:
            return _TENS[parts[0]]
        return None
    if len(parts) == 2 and parts[0] in _TENS and _ONES.get(parts[1], 99) < 10:
        return _TENS[parts[0]] + _ONES[parts[1]]
    return None


def _clean_line(clause: str) -> str:
    """A clause -> the card's big line: ALL CAPS, single trailing period."""
    line = " ".join((clause or "").split()).strip(" .")
    return (line.upper() + ".") if line else ""


def detect_title_cue(text: Any) -> Optional[Dict[str, Any]]:
    """Parse one narration sentence into a title-card cue, or None.

    Returns {"label": str, "number": int, "line": str}. A cue whose
    line is "" is an Age FRAGMENT ("Age 4." alone): the planner merges
    it with a following unit cue ("Week 6 of possession.") into one
    card, exactly as the reference's narration pairs them.
    """
    t = " ".join(str(text or "").split())
    if not t:
        return None
    m = _LATER_RE.match(t)
    if m:
        n = _number(m.group(1))
        if n is not None:
            unit = m.group(2).lower().rstrip("s")
            phrase = t[:m.start(1)] + str(n) + t[m.end(1):m.end(0)]
            return {"label": unit.capitalize(), "number": n,
                    "line": _clean_line(phrase)}
    m = _AGE_RE.match(t)
    if m:
        n = _number(m.group(1))
        if n is None:
            return None
        rest = t[m.end():].strip(" .")
        unit = _UNIT_OF_RE.search(t)
        if unit:
            # The card's line shows the count as digits, like the
            # reference's, even when the narration spells it out.
            clause = unit.group(0)
            a, b = unit.span(2)
            a -= unit.start(0)
            b -= unit.start(0)
            return {"label": "Age", "number": n,
                    "line": _clean_line(clause[:a] + str(_number(unit.group(2))) + clause[b:])}
        if not rest:
            return {"label": "Age", "number": n, "line": ""}
        return {"label": "Age", "number": n, "line": _clean_line(rest)}
    m = _UNIT_START_RE.match(t)
    if m:
        n = _number(m.group(2))
        if n is not None:
            line = t[:m.start(2)] + str(n) + t[m.end(2):]
            return {"label": m.group(1).capitalize(), "number": n,
                    "line": _clean_line(line)}
    return None


def card_duration(span: float) -> float:
    """The card's on-screen time for a cue spanning `span` seconds of
    narration: the span itself, clamped to [CARD_MIN_DUR, CARD_MAX_DUR]."""
    return round(min(CARD_MAX_DUR, max(CARD_MIN_DUR, float(span))), 3)


# ------------------------------------------------------------------ fonts

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


@lru_cache(maxsize=32)
def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    """Card fonts by role: "line" = bundled Anton (bold condensed caps),
    "digit" = bold sans for the flap numerals, "label" = the serif.
    Every role degrades to a system font, never to a crash."""
    size = max(8, int(size))
    if kind == "line":
        try:
            return ImageFont.truetype(str(_FONT_DIR / "Anton-Regular.ttf"), size)
        except OSError:
            pass
        names = ("LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf")
    elif kind == "digit":
        names = ("LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf")
    else:  # label — the reference's "Age" is a small serif
        names = ("LiberationSerif-Regular.ttf", "DejaVuSerif.ttf",
                 "LiberationSans-Regular.ttf", "DejaVuSans.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _tracked_width(draw: ImageDraw.ImageDraw, text: str, font, tracking: float) -> float:
    if not text:
        return 0.0
    w = sum(draw.textlength(ch, font=font) for ch in text)
    return w + tracking * max(0, len(text) - 1)


def _draw_tracked(draw: ImageDraw.ImageDraw, cx: float, y: float, text: str,
                  font, fill, tracking: float) -> None:
    x = cx - _tracked_width(draw, text, font, tracking) / 2.0
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def render_title_card(width: int, height: int, label: str, number: int,
                      line: str) -> Image.Image:
    """Draw one title card at (width, height): solid black, the serif
    label over two beige split-flap tiles, the big line under them in
    Anton. The whole stack is centred; the tiles block is
    DIGITS_BLOCK_H of the frame height (measured)."""
    W, H = int(width), int(height)
    img = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    u = H / 1080.0  # layout unit: everything scales off the 1080p design

    digits = f"{int(number):02d}" if 0 <= int(number) <= 99 else str(int(number))
    tile_h = DIGITS_BLOCK_H * H
    tile_w = tile_h * TILE_W_RATIO
    if len(digits) > 2:
        tile_w = tile_h * TILE_W_RATIO * (len(digits) / 2.0)
    gap = tile_h * TILE_GAP_RATIO
    block_w = tile_w * len(digits) + gap * max(0, len(digits) - 1)

    label = (label or "").strip()
    label_font = _font("label", LABEL_SIZE * H)
    label_h = (LABEL_SIZE * H * 1.25) if label else 0.0
    line = (line or "").strip()
    line_font = None
    line_h = 0.0
    if line:
        size = LINE_SIZE * H
        while size > 10:
            line_font = _font("line", size)
            if _tracked_width(draw, line, line_font, LINE_TRACKING * size) <= LINE_MAX_W * W:
                break
            size *= 0.92
        line_h = size * 1.25
    stack_h = label_h + (LABEL_GAP * H if label else 0.0) + tile_h \
        + ((LINE_GAP * H + line_h) if line else 0.0)
    y = (H - stack_h) / 2.0

    if label:
        draw.text((W / 2.0, y), label, font=label_font, fill=LABEL_FILL, anchor="ma")
        y += label_h + LABEL_GAP * H

    # Split-flap tiles: beige rounded tiles, a centre seam, dark numerals.
    digit_size = tile_h * 0.92
    x = (W - block_w) / 2.0
    radius = max(2.0, tile_h * 0.06)
    seam_w = max(2, int(0.005 * H))
    for ch in digits:
        box = (x, y, x + tile_w, y + tile_h)
        draw.rounded_rectangle(box, radius=radius, fill=TILE_FILL, outline=TILE_EDGE,
                               width=max(1, int(2 * u)))
        mid = y + tile_h / 2.0
        draw.line((x + 1, mid, x + tile_w - 1, mid), fill=TILE_SEAM, width=seam_w)
        while digit_size > 8:
            dfont = _font("digit", digit_size)
            if draw.textlength(ch, font=dfont) <= tile_w * 0.78:
                break
            digit_size *= 0.9
        draw.text((x + tile_w / 2.0, mid), ch, font=dfont, fill=DIGIT_FILL, anchor="mm")
        x += tile_w + gap
    y += tile_h

    if line:
        y += LINE_GAP * H
        _draw_tracked(draw, W / 2.0, y, line, line_font, LINE_FILL,
                      LINE_TRACKING * (line_font.size if hasattr(line_font, "size") else LINE_SIZE * H))
    return img
