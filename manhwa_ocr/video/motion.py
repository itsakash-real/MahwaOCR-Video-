"""Parameterized motion engine (video motion, scope 2026-10-26).

This module is the SINGLE place motion values live. It changes no existing
behavior on its own: it is pure functions over numbers, and the calibration
pass against the reference video will edit ONLY the constants in
MotionParams (or pass a custom MotionParams) -- no rewrites, no rewiring
for values.

Design (see video-motion-audit.md):
- Routing is one rule, any canvas: width-fit height > frame height
  (plus epsilon) -> "slide"; else "zoom". A panel taller than the frame
  slides through its full height; anything else zooms.
- Zoom span mode (default): rate = (end - start) / duration, so the zoom
  completes exactly over the shot -- continuous motion, never a hold,
  never static. Rate mode (fixed %/s with a total cap) is available for
  a reference that turns out to use fixed-speed zooms.
- Slide always spans the shot: speed = delta_y / duration, so the slide
  is synced to speech duration by construction.
- Alternation is chapter-global in card order: slide directions alternate
  down/up, zoom directions alternate in/out, and no two adjacent motions
  are ever equal (which also guarantees consecutive SHOTS differ).
- Overscan (the ONLY staging this module implies, and only as numbers
  for a renderer to stage): zoom -> source pre-scaled to
  max(zoom_start, zoom_end); slide -> staged height = frame height +
  delta_y. Both cover their full motion range by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal

Direction = Literal["in", "out", "down", "up"]
Kind = Literal["zoom", "slide"]


@dataclass(frozen=True)
class MotionParams:
    """Tunable motion constants. Defaults are the user's written spec --
    NOT measured reference values. The calibration pass (follow-up, once
    the reference video's motion profile arrives) edits these numbers
    and nothing else.

    zoom_in/out_start/end: span-mode zoom endpoints (scale factors).
    zoom_mode: "span" (rate derived from duration) or "rate" (fixed %/s).
    zoom_rate: fixed rate for "rate" mode (scale/second, signed by dir).
    zoom_cap: total zoom ceiling for "rate" mode (hold at cap).
    slide_headroom: extra staged height beyond delta_y, as a fraction of
        frame height (0.0 = endpoints sit exactly on the frame edges).
    slide_eps: width-fit height must exceed the frame by this fraction
        to count as a tall strip (else the panel zooms).
    overscan: documented staging margin for zooms, max(zoom_start,
        zoom_end) -- kept as a param so a calibration can widen it
        without touching code (must stay >= max zoom endpoint).
    """

    zoom_in_start: float = 1.0
    zoom_in_end: float = 1.2
    zoom_out_start: float = 1.2
    zoom_out_end: float = 1.0
    zoom_mode: str = "span"
    zoom_rate: float = 0.05
    zoom_cap: float = 1.2
    slide_headroom: float = 0.0
    slide_eps: float = 0.001
    overscan: float = 1.2


DEFAULT_PARAMS = MotionParams()


def classify(panel_w: float, panel_h: float, frame_w: float, frame_h: float,
             params: MotionParams = DEFAULT_PARAMS) -> Kind:
    """"slide" if the panel is taller than the frame (width-fit height
    exceeds frame height by more than slide_eps), else "zoom"."""
    pw, ph = max(1.0, float(panel_w)), max(1.0, float(panel_h))
    fw, fh = max(1.0, float(frame_w)), max(1.0, float(frame_h))
    return "slide" if ph * (fw / pw) > fh * (1.0 + params.slide_eps) else "zoom"


def slide_distance(panel_w: float, panel_h: float, frame_w: float, frame_h: float) -> float:
    """Delta-Y for a tall strip: width-fit height minus frame height.
    Zero (never negative) for panels that are not tall strips."""
    pw, ph = max(1.0, float(panel_w)), max(1.0, float(panel_h))
    fw, fh = max(1.0, float(frame_w)), max(1.0, float(frame_h))
    return max(0.0, ph * (fw / pw) - fh)


def zoom_for(direction: Literal["in", "out"], duration: float,
             params: MotionParams = DEFAULT_PARAMS) -> Dict[str, float]:
    """(start, end, rate) for a zoom in one direction over `duration`.

    Span mode: rate = (end - start) / duration, so the zoom completes
    exactly over the shot. Rate mode: fixed params.zoom_rate (signed by
    direction), end = start + rate * duration capped at params.zoom_cap
    for zoom-in (zoom-out is uncapped downward toward 1.0, its floor).
    """
    dur = max(0.5, float(duration))
    if direction == "in":
        start, end = params.zoom_in_start, params.zoom_in_end
    else:
        start, end = params.zoom_out_start, params.zoom_out_end
    if params.zoom_mode == "rate":
        rate = params.zoom_rate if direction == "in" else -params.zoom_rate
        end = start + rate * dur
        if direction == "in":
            end = min(end, params.zoom_cap)
        else:
            end = max(end, 1.0)
        return {"start": start, "end": end, "rate": rate}
    rate = (end - start) / dur
    return {"start": start, "end": end, "rate": rate}


def slide_for(direction: Literal["down", "up"], panel_w: float, panel_h: float,
              frame_w: float, frame_h: float, duration: float,
              params: MotionParams = DEFAULT_PARAMS) -> Dict[str, object]:
    """Slide spec for a tall strip: delta_y, speed (delta_y / duration,
    so the slide spans the shot exactly), and staged height (frame
    height + delta_y + headroom)."""
    dy = slide_distance(panel_w, panel_h, frame_w, frame_h)
    dur = max(0.5, float(duration))
    staged_h = max(1.0, float(frame_h)) + dy + params.slide_headroom * max(1.0, float(frame_h))
    return {"direction": direction, "delta_y": dy, "speed": dy / dur,
            "staged_h": staged_h}


def overscan_for(motion: Dict[str, object], params: MotionParams = DEFAULT_PARAMS) -> Dict[str, float]:
    """Minimal staging margin strictly required for `motion` to execute
    without revealing background. For a zoom: the source scale the
    renderer must pre-stage (max endpoint; defaults to params.overscan
    when it covers the endpoints). For a slide: the staged height
    (frame height + delta_y + headroom). This is the ONLY staging this
    module implies -- no fit, backdrop, or colour change."""
    if motion.get("kind") == "slide":
        return {"source_scale": 1.0,
                "staged_extra": float(motion.get("delta_y", 0.0))}
    start = float(motion.get("zoom_start", 1.0))
    end = float(motion.get("zoom_end", 1.0))
    need = max(start, end)
    return {"source_scale": max(need, params.overscan),
            "staged_extra": 0.0}


def motion_is_moving(motion: Dict[str, object]) -> bool:
    """True when the motion actually moves: zoom endpoints differ, or
    slide delta_y > 0. A motion failing this must never be emitted."""
    if motion.get("kind") == "slide":
        return float(motion.get("delta_y", 0.0)) > 0.0
    return float(motion.get("zoom_start", 1.0)) != float(motion.get("zoom_end", 1.0))


def assign_motions(items: List[Dict[str, float]],
                   frame_w: float, frame_h: float,
                   params: MotionParams = DEFAULT_PARAMS) -> List[Dict[str, object]]:
    """Assign one motion per item, in order, with chapter-global
    alternation. Each item: {"panel_w", "panel_h", "duration"}.
    Returns motions: zoom -> {"kind", "dir", "zoom_start", "zoom_end",
    "zoom_rate"}; slide -> {"kind", "dir", "delta_y", "speed",
    "staged_h"}. Slide directions alternate down/up, zoom directions
    alternate in/out, and no two adjacent motions are ever equal --
    which also guarantees consecutive shots differ, since a shot's first
    card always differs from the previous shot's last card.
    """
    motions: List[Dict[str, object]] = []
    prev: str | None = None
    last_slide_dir: Direction = "up"    # first tall strip slides down
    last_zoom_dir: Direction = "out"    # first zoomer zooms in
    for it in items:
        kind = classify(it["panel_w"], it["panel_h"], frame_w, frame_h, params)
        dur = float(it["duration"])
        if kind == "slide":
            d: Direction = "down" if last_slide_dir == "up" else "up"
            last_slide_dir = d
            spec = slide_for(d, it["panel_w"], it["panel_h"], frame_w, frame_h, dur, params)
            motion: Dict[str, object] = {"kind": "slide", "dir": d,
                                         "delta_y": spec["delta_y"],
                                         "speed": spec["speed"],
                                         "staged_h": spec["staged_h"]}
        else:
            d = "in" if last_zoom_dir == "out" else "out"
            last_zoom_dir = d
            z = zoom_for(d, dur, params)  # type: ignore[arg-type]
            motion = {"kind": "zoom", "dir": d,
                      "zoom_start": z["start"], "zoom_end": z["end"],
                      "zoom_rate": z["rate"]}
        name = f"{motion['kind']}_{motion['dir']}"
        assert name != prev, "motion alternation broke"
        assert motion_is_moving(motion), "emitted a static motion"
        prev = name
        motions.append(motion)
    return motions
