"""Pixel QC on the RENDERED video — the gate of record for the user's
presentation rejections (research mission, Phase 4 fix 4; incidents 10, 12,
15, 19).

Every gate before this one judges the PLAN: plan specs, storyboard tiles,
sync-audit samples of the plan JSON. None of them looked at the delivered
MP4, which is how "white space in every page" shipped (the white sat INSIDE
the foreground, invisible to frame-level band checks) and how caption-band
collisions were only found by the user watching the render.

This module decodes a few frames per shot from the finished MP4 (the bundled
ffmpeg, raw RGB) and checks, per shot:

  white_margins   near-white share of the outer margin band on each edge of
                  the FOREGROUND rect (the placed spec's own geometry) —
                  baked-in page margins reaching the rendered frame
                  (incident 10).
  wall_touch      on the walls the spec says the panel touches, the art
                  must actually reach the wall (no near-white slab at the
                  contact row) — the floating rejection, measured (incident 8).
  motion          for a placed shot (NO motion by rule), the mean absolute
                  difference between sampled frames must be codec noise
                  (~<3/255) — the scrolling rejection, measured (incident 9).

Failures are hard for placed mode (the current standard, where every rule
above is a user-locked spec); other modes are reported informationally so
the artifact exists for every chapter without risking legacy builds. The
FULL report is written to video/chapter_N.frameqc.json — never truncated,
never only-the-last-lines (incident 19).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List

import numpy as np

from ..util import UserError, atomic_write_text, require_chapter
from .ffmpeg import ffmpeg_exe
from .media import video_dir
from .panels import load_panels
from .plan import load_plan, plan_path
from .render import output_path

FRAMEQC_VERSION = 3
NEAR_WHITE = 242          # every channel >= this counts as near-white (placed_trim_rect's value)
MARGIN_BAND = 0.06        # outer share of the foreground rect each edge check covers
WHITE_FAIL = 0.55         # a margin band this near-white is suspected white space
WALL_WHITE_FAIL = 0.90    # a contact row this near-white means no art reaches the wall
INK_DARK = 160            # a pixel below this (darkest channel) is ink: strokes, borders, lettering
INK_FREE = 0.005          # a band with under this share of ink pixels carries no art structure
TRIM_CAP = 0.40           # mirrors plan._PLACED_TRIM_CAP: a trim rect edge here means the shave hit its cap
MOTION_FAIL = 3.0         # mean abs diff (0-255) allowed inside a NO-MOTION (placed) shot
PLAN_W, PLAN_H = 1920, 1080  # the canvas placed specs are authored on
FIT_TOL = 2.0             # px: a fitted dimension within this of the frame counts as touching the wall


def frameqc_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.frameqc.json"


def _decode_frame(video: Path, t: float, width: int, height: int) -> np.ndarray | None:
    """One RGB frame at time t (or None when the seek lands past the end)."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
           "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) < width * height * 3:
        return None
    return np.frombuffer(proc.stdout[:width * height * 3], dtype=np.uint8).reshape(height, width, 3)


def _near_white_share(arr: np.ndarray) -> float:
    if arr.size == 0:
        return 0.0
    return float((arr.min(axis=2) >= NEAR_WHITE).mean())


def _ink_share(arr: np.ndarray) -> float:
    """Share of INK pixels (dark strokes: SFX lettering, panel borders, bubble
    outlines, art itself). A near-white band WITH ink is the art's own
    composition — SFX overflowing a page margin, a diagonal panel border, a
    logo's white canvas — which the standing trim ruling (1158c9e) keeps
    whole because shaving it would amputate content. Only an ink-free
    near-white band is baked white space."""
    if arr.size == 0:
        return 0.0
    return float((arr.min(axis=2) < INK_DARK).mean())


# ---------------------------------------------------------------- panelfit
#
# Panel-fit shots (video.mode: panelfit — this repo's standard) move, so
# the placed-mode motion check does not apply; their white-space exposure
# does. The renderer anchors the foreground CENTRE at the spec's base
# size and grows it by the zoom interpolation, so the rect is exact at
# the shot's first frame (and, for zooming kinds, at its last). A drift's
# base is oversized vertically by construction (a panel edge must never
# show), so only its side bands are ever visible. White semantics are
# the placed mode's, keyed on the art-body record (bodytrim.py): a panel
# whose body record shows a completed two-sided crop is GUARANTEED free
# of canvas margin on that axis, so an ink-free white band at a wall the
# fit touches is a record/render divergence — a real bug, and a failure.
# Everywhere else (white art, capped shaves, no-border panels, body_trim
# off) the band stays a reviewer note: pixels cannot separate foam from
# a margin.

Y_BLOCKERS = {"no_border", "min_keep", "multi_art", "capped_top", "capped_bottom"}
X_BLOCKERS = {"no_border_x", "min_keep_x", "multi_art_x", "capped_left", "capped_right"}


def _axis_guaranteed(body: Dict[str, Any], sides: tuple) -> bool:
    """True when the body pass completed a two-sided crop on the axis that
    owns `sides` — prep guarantees no canvas margin remains there."""
    if not isinstance(body, dict) or not body:
        return False
    flags = set(body.get("flags") or [])
    if sides and sides[0] in ("top", "bottom"):
        return float(body.get("top") or 0) > 0 and float(body.get("bottom") or 0) > 0 \
            and not (flags & Y_BLOCKERS)
    return float(body.get("left") or 0) > 0 and float(body.get("right") or 0) > 0 \
        and not (flags & X_BLOCKERS)


def _panelfit_rects(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The foreground rects to measure for one panelfit card, on the plan
    canvas: {"at": "start"|"end", "x","y","w","h"}. Centre-anchored like
    the renderer; zooming kinds are measured at zoom_start and zoom_end,
    a drift at its constant (vertically oversized) base."""
    kind = spec.get("kind")
    bw, bh = float(spec.get("base_w") or 0), float(spec.get("base_h") or 0)
    if bw <= 0 or bh <= 0:
        return []

    def rect(k: float) -> Dict[str, Any]:
        w, h = bw * k, bh * k
        return {"x": (PLAN_W - w) / 2.0, "y": (PLAN_H - h) / 2.0, "w": w, "h": h}

    if kind in ("zoom", "fill"):
        zs = float(spec.get("zoom_start") or 1.0)
        ze = max(zs, float(spec.get("zoom_end") or 1.0))
        return [{"at": "start", **rect(zs)}, {"at": "end", **rect(ze)}]
    if kind == "drift":
        return [{"at": "start", **rect(1.0)}, {"at": "end", **rect(1.0)}]
    return []  # traverse/duo/legacy: geometry is not centre-anchored; notes only


def _rect_regions(f: np.ndarray, r: Dict[str, Any], scale: float) -> Dict[str, np.ndarray] | None:
    """The outer margin bands of a plan-canvas rect, clamped to the decoded
    frame. A side's band is measured whenever that edge lies inside the
    frame (a floating capped pillar's top edge counts); an edge bled past
    the frame shows interior art, not the foreground's own edge, and is
    omitted."""
    ih, iw = f.shape[:2]
    x0, y0 = int(round(r["x"] * scale)), int(round(r["y"] * scale))
    x1, y1 = int(round((r["x"] + r["w"]) * scale)), int(round((r["y"] + r["h"]) * scale))
    cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(iw, x1), min(ih, y1)
    if cx1 - cx0 < 8 or cy1 - cy0 < 8:
        return None
    band = max(1, int(round(MARGIN_BAND * min(x1 - x0, y1 - y0))))
    out: Dict[str, np.ndarray] = {}
    if y0 >= -FIT_TOL:
        out["top"] = f[cy0:cy0 + band, cx0:cx1]
    if y1 <= ih + FIT_TOL:
        out["bottom"] = f[cy1 - band:cy1, cx0:cx1]
    if x0 >= -FIT_TOL:
        out["left"] = f[cy0:cy1, cx0:cx0 + band]
    if x1 <= iw + FIT_TOL:
        out["right"] = f[cy0:cy1, cx1 - band:cx1]
    return out or None


def _shot_failures(shot: Dict[str, Any], frames: Dict[str, np.ndarray],
                   scale: float, bodies: Dict[str, Dict[str, Any]] | None = None) -> Dict[str, Any]:
    """The per-shot checks. `frames` = {"start","mid","end"} RGB arrays.
    `bodies` maps panel id -> its art-body record (bodytrim.py), which
    decides whether a white band at a touching wall is a defect (prep
    had guaranteed that axis clean) or the art's own white.

    White-space semantics (measured on the regressor ch1 pilot): a near-white
    edge band is a DEFECT only when the prep pass could not guarantee the
    unit's art bounds — a trim FALLBACK unit (placed untrimmed), a shave
    that hit its 40% cap on that side (margin possibly left behind), or —
    panelfit — a body record claiming a completed crop while the rendered
    frame still shows an ink-free white band at a wall the fit touches
    (a record/render divergence). Art that is legitimately white at its
    edge (soap foam, a logo's own canvas) and bands carrying art (SFX
    overflowing a margin, diagonal panel borders) are reviewer NOTES —
    prep already judged them content, and no pixel metric can separate
    foam from a page margin."""
    checks: Dict[str, Any] = {"white_margins": None, "wall_touch": None, "motion": None}
    fails: List[str] = []
    notes: List[str] = []
    strict = bool(shot.get("placed"))
    spec = None
    for c in shot.get("cards") or []:
        if isinstance(c.get("placed"), dict):
            spec = c["placed"]
            break
    if spec is not None:
        trim = spec.get("trim") if isinstance(spec.get("trim"), list) else None
        fallback_unit = bool(spec.get("trim_fallback"))

        def _trim_capped(side: str) -> bool:
            """True when the unit's shave hit its cap on `side` (the rect edge
            sits at TRIM_CAP): margin may remain that prep could not shave."""
            if not trim or len(trim) != 4 or fallback_unit:
                return False
            try:
                x0, y0, x1, y1 = (float(v) for v in trim)
            except (TypeError, ValueError):
                return False
            return {"top": abs(y0 - TRIM_CAP) < 0.01, "bottom": abs(1.0 - y1 - TRIM_CAP) < 0.01,
                    "left": abs(x0 - TRIM_CAP) < 0.01, "right": abs(1.0 - x1 - TRIM_CAP) < 0.01}.get(side, False)

        def _defect_side(side: str) -> bool:
            # A strict white-space violation needs prep unable to guarantee
            # the bounds on this side (fallback unit or capped shave).
            return fallback_unit or _trim_capped(side)
        x = spec["off_x"] * scale
        y = spec["off_y"] * scale
        w = spec["base_w"] * scale
        h = spec["base_h"] * scale
        f = frames.get("mid")
        if f is not None and w >= 8 and h >= 8:
            ih, iw = f.shape[:2]
            x0, y0 = max(0, int(round(x))), max(0, int(round(y)))
            x1, y1 = min(iw, int(round(x + w))), min(ih, int(round(y + h)))
            band = max(1, int(round(MARGIN_BAND * min(w, h))))
            regions = {
                "top": f[y0:y0 + band, x0:x1],
                "bottom": f[y1 - band:y1, x0:x1],
                "left": f[y0:y1, x0:x0 + band],
                "right": f[y0:y1, x1 - band:x1],
            }
            margins = {k: _near_white_share(v) for k, v in regions.items()}
            inks = {k: _ink_share(v) for k, v in regions.items()}
            checks["white_margins"] = {k: round(v, 4) for k, v in margins.items()}
            checks["ink_shares"] = {k: round(v, 4) for k, v in inks.items()}
            for side, white in margins.items():
                if white <= WHITE_FAIL:
                    continue
                if inks[side] < INK_FREE and _defect_side(side):
                    # near-white, ink-free, AND prep could not guarantee the
                    # bounds here (fallback unit / capped shave): baked margin
                    fails.append(f"white margin inside the foreground ({side}={white:.0%}, ink-free, "
                                 f"{'trim fallback' if fallback_unit else 'trim capped'})")
                elif inks[side] < INK_FREE:
                    # white art at its own edge (foam, sky, logo canvas):
                    # indistinguishable from a margin by pixels — reviewer note
                    notes.append(f"{side} band {white:.0%} near-white, ink-free — art is white "
                                 f"at its edge (verify it is the art, not a margin)")
                else:
                    # near-white but carrying art (SFX overflow, border, logo
                    # canvas): content by the trim ruling — reviewer info
                    notes.append(f"{side} band {white:.0%} white but carries art "
                                 f"(ink {inks[side]:.1%}) — content, kept whole")
            # Wall contact: on the axes the spec says the panel touches, the
            # art (not an ink-free white slab) must reach the frame wall —
            # the contact band is the same outer margin band measured above.
            touches = str(spec.get("touches") or "")
            sides = (("top", "bottom") if touches in ("top_bottom", "both") else ()) \
                + (("left", "right") if touches in ("left_right", "both") else ())
            probes = {side: margins[side] for side in sides}
            if probes:
                checks["wall_touch"] = {k: round(v, 4) for k, v in probes.items()}
                for side, white in probes.items():
                    if white > WALL_WHITE_FAIL and inks[side] < INK_FREE and _defect_side(side) and strict:
                        fails.append(f"art does not reach a wall it must touch ({side}={white:.0%}, ink-free, "
                                     f"{'trim fallback' if fallback_unit else 'trim capped'})")
    if strict and frames.get("start") is not None and frames.get("mid") is not None:
        d1 = float(np.abs(frames["mid"].astype(np.int16) - frames["start"].astype(np.int16)).mean())
        d2 = 0.0
        if frames.get("end") is not None:
            d2 = float(np.abs(frames["end"].astype(np.int16) - frames["start"].astype(np.int16)).mean())
        motion = round((d1 + d2) / 2.0, 3)
        checks["motion"] = motion
        if motion > MOTION_FAIL:
            fails.append(f"placed shot moves (mean frame diff {motion:.1f} > {MOTION_FAIL:g}; "
                         "placed panels never move)")
    if shot.get("panelfit") and not strict:
        for c in shot.get("cards") or []:
            spec = c.get("panelfit")
            if not isinstance(spec, dict):
                continue
            pid = str(c.get("panel") or "")
            body = (bodies or {}).get(pid) or {}
            for r in _panelfit_rects(spec):
                f = frames.get(r["at"])
                if f is None:
                    continue
                regions = _rect_regions(f, r, scale)
                if not regions:
                    continue
                margins = {k: _near_white_share(v) for k, v in regions.items()}
                inks = {k: _ink_share(v) for k, v in regions.items()}
                checks.setdefault("panelfit_margins", {})[f"{pid}@{r['at']}"] = \
                    {k: round(v, 4) for k, v in margins.items()}
                for side, white in margins.items():
                    if white <= WHITE_FAIL:
                        continue
                    if inks[side] < INK_FREE:
                        # Wall contact is not required: a capped pillar floats,
                        # and white canvas at ITS edge is the same rejection.
                        if _axis_guaranteed(body, (side,)):
                            fails.append(
                                f"white margin inside the panelfit foreground ({pid} {side}="
                                f"{white:.0%} at {r['at']}, ink-free, body crop was applied) — "
                                "record/render divergence")
                        else:
                            notes.append(f"{pid} {side} band {white:.0%} near-white, ink-free — "
                                         "art is white at its edge, or the body pass could not "
                                         f"guarantee this axis (flags: {body.get('flags') or 'none'})")
                    else:
                        notes.append(f"{pid} {side} band {white:.0%} white but carries art "
                                     f"(ink {inks[side]:.1%}) — content, kept whole")
    return {"checks": checks, "failures": fails, "notes": notes}


def qc_chapter(root: Path, chapter: str, cfg, *, log: Callable[[str], None] = print,
               preview: bool = False) -> Dict[str, Any]:
    """Run the frame QC on the rendered chapter video. Writes the FULL report
    to video/chapter_N.frameqc.json and returns it. Raises UserError when a
    strict-mode (placed) violation was found — the caller decides whether
    that stops the build."""
    chapter = require_chapter(chapter)
    plan_file = plan_path(root, chapter)
    if not plan_file.exists():
        raise UserError(f"No plan for chapter {chapter}; nothing to QC.")
    plan = load_plan(root, chapter)
    video = output_path(root, chapter, preview)
    if not video.exists():
        raise UserError(f"No rendered video at {video}; render first.")
    probe = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(video)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    import re

    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", probe.stderr or "")
    if not m:
        raise UserError(f"Couldn't read the video size of {video.name}.")
    width, height = int(m.group(1)), int(m.group(2))
    scale = width / float(PLAN_W)

    rows: List[Dict[str, Any]] = []
    failures: List[str] = []
    notes: List[str] = []
    strict_shots = 0
    pdata = load_panels(root, chapter) or {}
    bodies = {str(p.get("id")): (p.get("body") or {}) for p in pdata.get("panels", [])}
    for shot in plan.get("shots") or []:
        start, end = float(shot["start"]), float(shot["end"])
        dur = max(0.0, end - start)
        times = {"start": start + min(0.05, dur / 4), "mid": start + dur / 2, "end": end - min(0.05, dur / 4)}
        frames = {k: _decode_frame(video, t, width, height) for k, t in times.items()}
        got = {k: v for k, v in frames.items() if v is not None}
        if not got:
            rows.append({"id": shot.get("id"), "error": "no frames decoded"})
            continue
        row = {"id": shot.get("id"), "start": round(start, 3), "end": round(end, 3),
               **_shot_failures(shot, got, scale, bodies)}
        if shot.get("placed") or shot.get("panelfit"):
            strict_shots += 1
            for f in row["failures"]:
                failures.append(f"{shot.get('id')}: {f}")
            for n in row.get("notes") or []:
                notes.append(f"{shot.get('id')}: {n}")
        rows.append(row)

    report = {
        "version": FRAMEQC_VERSION, "chapter": chapter,
        "video": {"file": video.name, "width": width, "height": height},
        "mode": plan.get("mode"),
        "strict_shots": strict_shots,
        "thresholds": {"near_white": NEAR_WHITE, "margin_band": MARGIN_BAND,
                       "white_fail": WHITE_FAIL, "wall_white_fail": WALL_WHITE_FAIL,
                       "ink_dark": INK_DARK, "ink_free": INK_FREE,
                       "motion_fail": MOTION_FAIL},
        "shots": rows,
        "failures": failures,
        "notes": notes,
        "pass": not failures,
    }
    atomic_write_text(frameqc_path(root, chapter), json.dumps(report, indent=1, ensure_ascii=False))
    if failures:
        log(f"  frame QC: FAIL — {len(failures)} strict violation(s) "
            f"(full report: video/chapter_{chapter}.frameqc.json)")
        for f in failures[:8]:
            log(f"    {f}")
    else:
        log(f"  frame QC: pass ({len(rows)} shot(s) checked, {strict_shots} strict; "
            f"{len(notes)} content note(s); report: video/chapter_{chapter}.frameqc.json)")
    return report
