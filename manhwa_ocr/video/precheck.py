"""Phase 0.2 — read-only defect pre-checker.

Runs the detector's cached boxes against the planner's windows and lists
the shots where a window BISECTS a text element (bubble, in-bubble text,
or free text): the box is neither wholly inside the window nor wholly
outside it, so the render will slice it. Nothing is changed — the report
(.work/video/chN/precheck.json) just lets the auditor review the flagged
shots first.

Scope, stated plainly because the bake-off depends on it: this sees ONE
defect class, text bisection, and only where the detector found a box at
all. Sliced faces, wrong zones and caption-band placements are invisible
to it, and SFX glyphs are the detector's weak class (it misses many), so
silence here is never proof a shot is clean. Recall is therefore always
reported per defect class, never as one number.

The window tested is the plan's crop (the start frame). Motion end-frame
geometry is the solver's problem (Build 4 models it explicitly).

B1f adds a second, separate review surface in the same report:
"needs_slice_hint" flags panels whose interior carries a strong
DIAGONAL light band (a white slash cutting the panel corner-to-corner —
the cut trim_v2's row slicing cannot see). The flag is a review item
only: precheck never slices, drops or re-pairs anything; the editor
answers a flag by writing a cues.json "slice_hints" entry by hand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
from PIL import Image

from ..util import UserError, atomic_write_text, require_chapter
from ..workspace import work_root

EPS = 0.003  # an edge within this of the window edge counts as flush, not cut

SCOPE_NOTE = ("text-bisection only (bubble / text_bubble / text_free boxes vs the "
              "plan window); face, zone and caption-band defects are out of scope, "
              "and panels without detector boxes were not checked")

# ------------------------------------------------- B1f: diagonal bands
#
# A diagonal white band (the "police slash" class) has no pure gutter
# row, so trim_v2 cannot slice it — but its signature is cheap to see:
# in each row it crosses, the LIGHT pixels sit in a narrow x-range, and
# that range's centre (the row's light centroid) walks steadily across
# the panel as the row descends. A horizontal gutter's centroid never
# moves; a vertical band's never moves either. Only a diagonal walks.

DIAG_LIGHT_MIN = 242      # a pixel is light when its darkest channel >= this
                          # (the codebase's white level, panels.WHITE_THRESHOLD)
DIAG_ROW_LIGHT_SHARE = 0.15   # a row counts when >= this share of it is light
DIAG_MIN_SPAN_SHARE = 0.25    # the walk must span >= this share of the height
DIAG_MIN_SHIFT_SHARE = 0.30   # ... and move the centroid >= this share of the width
DIAG_MIN_R2 = 0.85            # ... steadily: linear-fit R^2 at least this


def diagonal_light_band(img: Image.Image) -> Dict[str, Any] | None:
    """Detect a strong diagonal light band in one panel image.

    Returns {"shift": signed centroid travel as a share of the width,
    "span": [f0, f1] panel-height fractions the walk covers,
    "rows": qualifying row count, "r2": fit quality} when a band is
    found, else None. Pure: reads pixels, changes nothing, and never
    proposes a cut — the flag it feeds is a human review item."""
    arr = np.asarray(img.convert("RGB"))
    h, w = arr.shape[0], arr.shape[1]
    if h < 8 or w < 8:
        return None
    light = arr.min(axis=2) >= DIAG_LIGHT_MIN
    share = light.mean(axis=1)
    rows = np.flatnonzero(share >= DIAG_ROW_LIGHT_SHARE)
    if len(rows) < max(8, int(DIAG_MIN_SPAN_SHARE * h)):
        return None
    xs = np.arange(w, dtype=np.float64)
    counts = light.sum(axis=1)
    centroid = (light * xs).sum(axis=1) / np.maximum(counts, 1)
    ys = rows.astype(np.float64)
    cs = centroid[rows] / float(w)
    # Least-squares line centroid ~ row over the qualifying rows.
    a_mat = np.vstack([ys, np.ones_like(ys)]).T
    slope, base = np.linalg.lstsq(a_mat, cs, rcond=None)[0]
    fit = slope * ys + base
    ss_res = float(((cs - fit) ** 2).sum())
    ss_tot = float(((cs - cs.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    shift = slope * (ys[-1] - ys[0])
    span = [float(ys[0]) / h, float(ys[-1] + 1) / h]
    if abs(shift) < DIAG_MIN_SHIFT_SHARE or r2 < DIAG_MIN_R2:
        return None
    if span[1] - span[0] < DIAG_MIN_SPAN_SHARE:
        return None
    return {"shift": round(float(shift), 3), "span": [round(span[0], 3), round(span[1], 3)],
            "rows": int(len(rows)), "r2": round(float(r2), 3)}


def window_relation(window, box, eps: float = EPS) -> tuple[str, List[str]]:
    """('inside' | 'outside' | 'bisected', [cut edges]) for a box vs a window.

    Both are [x0, y0, x1, y1] panel fractions."""
    wx0, wy0, wx1, wy1 = (float(v) for v in window)
    bx0, by0, bx1, by1 = (float(box[k]) for k in ("x0", "y0", "x1", "y1"))
    if bx1 <= wx0 + eps or bx0 >= wx1 - eps or by1 <= wy0 + eps or by0 >= wy1 - eps:
        return "outside", []
    cut = []
    if bx0 < wx0 - eps:
        cut.append("left")
    if bx1 > wx1 + eps:
        cut.append("right")
    if by0 < wy0 - eps:
        cut.append("top")
    if by1 > wy1 + eps:
        cut.append("bottom")
    if not cut:
        return "inside", []
    return "bisected", cut


def precheck_chapter(root: Path, chapter: str) -> Dict[str, Any]:
    """Check every planned window of a chapter against its panels' boxes."""
    from .detect import load_boxes
    from .media import video_dir
    from .panels import load_panels

    chapter = require_chapter(chapter)
    plan_path = video_dir(root) / f"chapter_{chapter}.plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UserError(f"No plan for chapter {chapter}; run video plan first.") from None
    data = load_panels(root, chapter)
    if not data:
        raise UserError(f"No panels.json for chapter {chapter}; run video panels first.")

    findings: List[Dict[str, Any]] = []
    cards = 0
    panels_checked = set()
    panels_without: List[str] = []
    for shot in plan.get("shots", []):
        for card in shot.get("cards", []):
            cards += 1
            panel_id = card.get("panel")
            window = card.get("crop")
            if not panel_id or not window:
                continue
            rec = load_boxes(root, chapter, panel_id)
            if rec is None:
                if panel_id not in panels_without:
                    panels_without.append(panel_id)
                continue
            panels_checked.add(panel_id)
            for box in rec["boxes"]:
                rel, cut = window_relation(window, box)
                if rel == "bisected":
                    findings.append({
                        "shot": shot.get("id"),
                        "panel": panel_id,
                        "label": box["label"],
                        "score": box["score"],
                        "box": [box["x0"], box["y0"], box["x1"], box["y1"]],
                        "window": [float(v) for v in window],
                        "cut": cut,
                    })
    if cards and not panels_checked:
        raise UserError(f"No detector boxes for chapter {chapter}; run video detect first, "
                        "then precheck. Nothing was checked, so no result is reported.")
    # B1f: diagonal-band review flags for the panels this plan shows.
    # Flag only — a flag never slices or drops anything; the editor
    # answers it with a hand-written cues.json "slice_hints" entry.
    # A panel whose image cannot be read is skipped, never fatal: this
    # is a review aid riding on the bisection report, not a gate.
    slice_flags: List[Dict[str, Any]] = []
    by_id = {p["id"]: p for p in data["panels"]}
    shots_of: Dict[str, List[Any]] = {}
    for shot in plan.get("shots", []):
        for card in shot.get("cards", []):
            if card.get("panel"):
                shots_of.setdefault(card["panel"], []).append(shot.get("id"))
    if shots_of:
        from .panels import panel_image

        for pid, shot_ids in shots_of.items():
            rec = by_id.get(pid)
            if rec is None:
                continue
            try:
                band = diagonal_light_band(panel_image(root, chapter, rec, data["norm_width"]))
            except Exception:
                continue
            if band is not None:
                slice_flags.append({
                    "panel": pid,
                    "shots": shot_ids,
                    "flag": "needs_slice_hint",
                    "reason": ("interior carries a strong diagonal light band "
                               "(light centroid walks across the panel) — trim_v2 "
                               "cannot slice it; add a cues.json \"slice_hints\" "
                               "entry by hand if the band is a panel cut"),
                    **band,
                })
    report = {
        "chapter": chapter,
        "shots": len(plan.get("shots", [])),
        "cards": cards,
        "panels_checked": len(panels_checked),
        "panels_without_boxes": panels_without,
        "findings": findings,
        "needs_slice_hint": slice_flags,
        "scope": SCOPE_NOTE + ("; diagonal-band slice-hint flags (needs_slice_hint) "
                                "are a separate image-based review surface: flag only, "
                                "never an automatic slice or drop"),
    }
    out = work_root(root) / "video" / f"ch{chapter}" / "precheck.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out, json.dumps(report, indent=1, ensure_ascii=False))
    return report
