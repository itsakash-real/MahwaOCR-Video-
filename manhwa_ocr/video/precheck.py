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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..util import UserError, atomic_write_text, require_chapter
from ..workspace import work_root

EPS = 0.003  # an edge within this of the window edge counts as flush, not cut

SCOPE_NOTE = ("text-bisection only (bubble / text_bubble / text_free boxes vs the "
              "plan window); face, zone and caption-band defects are out of scope, "
              "and panels without detector boxes were not checked")


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
    report = {
        "chapter": chapter,
        "shots": len(plan.get("shots", [])),
        "cards": cards,
        "panels_checked": len(panels_checked),
        "panels_without_boxes": panels_without,
        "findings": findings,
        "scope": SCOPE_NOTE,
    }
    out = work_root(root) / "video" / f"ch{chapter}" / "precheck.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out, json.dumps(report, indent=1, ensure_ascii=False))
    return report
