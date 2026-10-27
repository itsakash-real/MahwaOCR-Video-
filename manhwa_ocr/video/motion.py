"""Parameterized motion engine (video motion, scope 2026-10-26).

CALIBRATION TARGET -- LOCKED BY USER DECISION 2026-10-26: match the
reference video (https://youtu.be/5Bf9zpjEs8g, "this is refrence vidoe",
3:21). This SUPERSEDES the earlier written spec (motion on every shot +
alternation) for this job. The reference, measured over 22 sampled shots,
is almost entirely STATIC holds (2-4 s shots, hard cuts; >=18/22 with no
detectable move) with exactly two narrow exceptions:
  1. a slow zoom-in (~1.0x -> ~1.15-1.2x with a slight downward drift)
     reserved for WIDE TEXT-HEAVY images (e.g. comments/list screenshots)
     -- observed once, at ~1:42-1:47, on a wide text/comments image;
  2. at most a very slight vertical drift on tall strips.
There is NO regular zoom/pan alternation pattern in the reference, so this
module does NOT alternate: each shot's motion is routed independently,
and adjacent holds are expected (the reference is mostly holds).

This module is the SINGLE place motion values live. It changes no existing
behavior on its own: it is pure functions over numbers, and tuning means
editing ONLY the constants in MotionParams (or passing a custom
MotionParams) -- no rewrites, no rewiring for values. In practice the fix
this module enables is REMOVAL/neutralization of unwanted motion (pans
and zooms that deviate from the reference style), not adding motion
everywhere: assign_motions() ignores any incoming motion and assigns
from routing alone, so wiring it in REPLACES existing motion assignment.

Deliberately NOT in this module (do not add without a new user decision):
- No zoom-out, no pan, no full-height slide, no shake/flash, no
  alternation -- none of those appear in the reference, and inventing
  additional profiles is out of scope.
- No text detection: the caller supplies text_heavy (from OCR coverage,
  manual tagging, or panel flags). The module routes on the flag; it
  does not guess it.
- No framing, backdrop, transition, or chapter-end behavior: those are
  the user's area. The ONLY staging this module implies is the overscan
  margin in overscan_for(), strictly required for its own motions to
  execute without revealing background.

Wiring + finalization still await the second video's analysis (the user's
own video, findings not yet forwarded): once it arrives, the user's
current motion is compared against this target and exactly which motion
behaviors change is finalized. Until then, no existing mode's rendered
motion changes -- this module is unwired by design.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal

Kind = Literal["hold", "zoom_text", "drift"]

MOTION_KINDS = ("hold", "zoom_text", "drift")


@dataclass(frozen=True)
class MotionParams:
    """Tunable motion constants for the locked reference target.

    zoom_text_start/end: the slow zoom-in span for wide text-heavy
        images. End default 1.18 = midpoint of the MEASURED band
        1.15-1.2 (the one observed zoom, ~1:42-1:47). Tune within the
        band; do not widen it without new measurement.
    zoom_text_drift: downward drift over a zoom_text shot, as a fraction
        of frame height. ESTIMATED ("slight" was observed but not
        quantified) -- tunable, flagged for refinement if the second
        video's analysis quantifies it.
    tall_drift_total: total vertical drift over a tall-strip shot, as a
        fraction of frame height. ESTIMATED ("very slight" observed but
        not quantified) -- deliberately far below panelfit's 5-13%
        travel; tunable.
    tall_drift_dir: drift direction for tall strips. Default "down",
        assumed from the only observed drift direction (the zoom's
        downward drift); tunable -- tall-strip direction was NOT
        measured.
    slide_eps: width-fit height must exceed the frame by this fraction
        to count as a tall strip (else the panel holds).
    overscan: staging margin for zoom_text (must stay >= zoom_text_end).
    """

    zoom_text_start: float = 1.0
    zoom_text_end: float = 1.18
    zoom_text_drift: float = 0.02
    tall_drift_total: float = 0.02
    tall_drift_dir: str = "down"
    slide_eps: float = 0.001
    overscan: float = 1.2


DEFAULT_PARAMS = MotionParams()


def _is_tall_strip(panel_w: float, panel_h: float, frame_w: float, frame_h: float,
                   params: MotionParams) -> bool:
    """True when the panel is taller than the frame: its width-fit
    height exceeds the frame height by more than slide_eps."""
    pw, ph = max(1.0, float(panel_w)), max(1.0, float(panel_h))
    fw, fh = max(1.0, float(frame_w)), max(1.0, float(frame_h))
    return ph * (fw / pw) > fh * (1.0 + params.slide_eps)


def route(panel_w: float, panel_h: float, frame_w: float, frame_h: float,
          text_heavy: bool = False,
          params: MotionParams = DEFAULT_PARAMS) -> Kind:
    """The reference target's routing, in priority order:
    1. tall strip -> "drift" (very slight vertical drift);
    2. wide (aspect > 1) AND text_heavy -> "zoom_text" (the reference's
       one observed exception: wide text/comments images);
    3. everything else -> "hold" (static hold -- the default).
    No other profile exists. A text-heavy panel that is neither tall nor
    wide holds: the reference exception is specifically for WIDE text
    images, and inventing a tall-text or square-text profile would be
    guessing beyond the measurement."""
    pw, ph = max(1.0, float(panel_w)), max(1.0, float(panel_h))
    fw, fh = max(1.0, float(frame_w)), max(1.0, float(frame_h))
    if _is_tall_strip(pw, ph, fw, fh, params):
        return "drift"
    if text_heavy and (pw / ph) > 1.0:
        return "zoom_text"
    return "hold"


def zoom_text_for(duration: float,
                  params: MotionParams = DEFAULT_PARAMS) -> Dict[str, float]:
    """The zoom_text exception over `duration`: slow zoom-in spanning
    exactly the shot (rate = span / duration), plus the slight downward
    drift (total fraction of frame height, and its per-second speed)."""
    dur = max(0.5, float(duration))
    span = params.zoom_text_end - params.zoom_text_start
    return {"start": params.zoom_text_start,
            "end": params.zoom_text_end,
            "rate": span / dur,
            "drift": params.zoom_text_drift,
            "drift_speed": params.zoom_text_drift / dur}


def drift_for(duration: float,
              params: MotionParams = DEFAULT_PARAMS) -> Dict[str, object]:
    """The tall-strip drift over `duration`: total drift capped at
    tall_drift_total (fraction of frame height), speed = total /
    duration. Deliberately tiny -- this is a near-hold, not a slide:
    the full-height travel of the old spec is gone."""
    dur = max(0.5, float(duration))
    return {"dir": params.tall_drift_dir,
            "total": params.tall_drift_total,
            "speed": params.tall_drift_total / dur}


def overscan_for(motion: Dict[str, object],
                 params: MotionParams = DEFAULT_PARAMS) -> Dict[str, float]:
    """Minimal staging margin strictly required for `motion` to execute
    without revealing background. Hold needs none (1.0). Zoom_text
    needs the source pre-scaled to its endpoint (params.overscan covers
    it). Drift needs staged extra equal to its tiny total. This is the
    ONLY staging this module implies -- no fit, backdrop, or colour
    change."""
    kind = motion.get("kind")
    if kind == "zoom_text":
        return {"source_scale": max(params.zoom_text_end, params.overscan),
                "staged_extra": 0.0}
    if kind == "drift":
        return {"source_scale": 1.0,
                "staged_extra": float(motion.get("total", params.tall_drift_total))}
    return {"source_scale": 1.0, "staged_extra": 0.0}


def motion_is_moving(motion: Dict[str, object]) -> bool:
    """True when the motion actually moves. A hold is static BY DESIGN
    (the reference default) -- it is a valid motion, not an error."""
    if motion.get("kind") == "hold":
        return False
    if motion.get("kind") == "zoom_text":
        return float(motion.get("start", 1.0)) != float(motion.get("end", 1.0))
    return float(motion.get("total", 0.0)) > 0.0


def assign_motions(items: List[Dict[str, object]],
                   frame_w: float, frame_h: float,
                   params: MotionParams = DEFAULT_PARAMS) -> List[Dict[str, object]]:
    """Assign one motion per item, routed independently -- NO alternation
    (the reference has none; adjacent holds are expected and correct).
    Each item: {"panel_w", "panel_h", "duration", "text_heavy": bool
    (optional, default False -- caller-supplied, never guessed here)}.
    Returns motions: hold -> {"kind": "hold"}; zoom_text -> {"kind",
    "zoom_text", "start", "end", "rate", "drift", "drift_speed"}; drift
    -> {"kind", "dir", "total", "speed"}. Every returned motion is one
    of MOTION_KINDS -- never a pan, zoom-out, full slide, or shake.
    NOTE: this REPLACES any incoming motion assignment (it takes geometry
    + flags, not prior motions). That is the neutralization mechanism:
    wiring this in removes pans/zooms that deviate from the reference."""
    motions: List[Dict[str, object]] = []
    for it in items:
        kind = route(float(it["panel_w"]), float(it["panel_h"]),
                     frame_w, frame_h, bool(it.get("text_heavy", False)), params)
        dur = float(it["duration"])
        if kind == "zoom_text":
            z = zoom_text_for(dur, params)
            motion: Dict[str, object] = {"kind": "zoom_text",
                                         "start": z["start"], "end": z["end"],
                                         "rate": z["rate"], "drift": z["drift"],
                                         "drift_speed": z["drift_speed"]}
        elif kind == "drift":
            d = drift_for(dur, params)
            motion = {"kind": "drift", "dir": d["dir"],
                      "total": d["total"], "speed": d["speed"]}
        else:
            motion = {"kind": "hold"}
        assert motion["kind"] in MOTION_KINDS, "emitted an unknown motion profile"
        motions.append(motion)
    return motions
