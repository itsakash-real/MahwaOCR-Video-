"""Build 4 — safe-crop solver (proposal engine, read-only).

For every planned card whose window bisects a detector box, propose the
nearest window of the SAME height that bisects nothing — at the start
frame and at the motion's extreme frame, because the render shows both
(Build 4 clause 1: solve against the rendered visible region). Excluding
the text wholly is a designed outcome, on equal footing with holding it
whole (clause 2). When no seam exists within a sane move, the proposal
is the whole panel and the card is flagged unsolved — never a sliced
window (clause 3's fallback; blur-padding was not approved).

The solver sees detector boxes only. It cannot see faces or SFX glyphs
the detector missed (F2/F3 measured that blindness), so its proposals
are DRAFTS for the auditor, anchored by minimal change from the
planner's window — not replacements for the audit.

Report: .work/video/chN/solve.json (nothing in the plan is modified).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..util import UserError, atomic_write_text, require_chapter
from ..workspace import work_root
from .precheck import window_relation

SEAM = 0.006        # clearance kept between a window edge and a box edge
MAX_MOVE = 0.35     # farther than this is a reframe, not a seam thread
PUSH_INSET = 0.065  # push_in end-frame centre-crop, per edge (measured)
PAN_OVERSHOOT = 0.04  # a pan's visible reach past the plan window (measured)
CAPTION_BAND = 0.80   # window-relative y where the caption band starts
CAPTION_COST = 0.25   # soft penalty per text box parked in the caption band


def _frames(window, motion: Optional[str]) -> List[tuple[float, float]]:
    """Visible y-ranges as (offset_a, offset_b) from the window's y0: the
    start frame plus the motion's extreme frame. All are affine in y0."""
    _x0, y0, _x1, y1 = (float(v) for v in window)
    h = y1 - y0
    frames = [(0.0, h)]
    m = (motion or "static").lower()
    if m == "push_in":
        frames.append((PUSH_INSET * h, h - PUSH_INSET * h))
    elif m == "pull_out":
        frames.append((-PUSH_INSET * h, h + PUSH_INSET * h))
    elif m == "pan_down":
        s = min(PAN_OVERSHOOT, max(0.0, 1.0 - y1))
        frames.append((s, h + s))
    elif m == "pan_up":
        s = min(PAN_OVERSHOOT, max(0.0, y0))
        frames.append((-s, h - s))
    return frames


def _bisected_in_frames(y0: float, window, boxes, frames) -> bool:
    x0, _y0, x1, _y1 = (float(v) for v in window)
    for a, b in frames:
        w = [x0, y0 + a, x1, y0 + b]
        for box in boxes:
            if window_relation(w, box)[0] == "bisected":
                return True
    return False


def _caption_cost(y0: float, h: float, boxes) -> float:
    cost = 0.0
    for box in boxes:
        rel_mid = ((box["y0"] + box["y1"]) / 2.0 - y0) / h if h > 0 else 0.0
        if 0.0 <= rel_mid <= 1.0 and rel_mid >= CAPTION_BAND:
            cost += CAPTION_COST
    return cost


def solve_card(boxes: List[Dict[str, Any]], window, motion: Optional[str]) -> Dict[str, Any]:
    """Propose a safe window for one card. Pure geometry; no I/O."""
    x0, y0, x1, y1 = (float(v) for v in window)
    h = y1 - y0
    frames = _frames(window, motion)
    bad = [b for b in boxes
           if any(window_relation([x0, y0 + fa, x1, y0 + fb], b)[0] == "bisected"
                  for fa, fb in frames)]
    if not bad and not _bisected_in_frames(y0, window, boxes, frames):
        return {"status": "already_safe", "window": [x0, y0, x1, y1], "moved": 0.0,
                "kind": None, "bisected": []}

    candidates = {round(y0, 6), 0.0, round(1.0 - h, 6)}
    for a, b in frames:
        for box in boxes:
            candidates.add(round(box["y1"] + SEAM - b, 6))   # box flush inside frame bottom
            candidates.add(round(box["y0"] - SEAM - b, 6))   # frame bottom just above box top
            candidates.add(round(box["y0"] - SEAM - a, 6))   # box flush inside frame top
            candidates.add(round(box["y1"] + SEAM - a, 6))   # frame top just below box bottom

    best = None  # (cost, -inside_count, y0')
    for cand in candidates:
        cy0 = min(max(cand, 0.0), max(0.0, 1.0 - h))
        if abs(cy0 - y0) > MAX_MOVE:
            continue
        if _bisected_in_frames(cy0, window, boxes, frames):
            continue
        w = [x0, cy0, x1, cy0 + h]
        inside = sum(1 for b in boxes if window_relation(w, b)[0] == "inside")
        cost = abs(cy0 - y0) + _caption_cost(cy0, h, boxes)
        key = (round(cost, 6), -inside, round(abs(cy0 - y0), 6))
        if best is None or key < best[0]:
            best = (key, cy0)
    names = [f"{b['label']}@{b['y0']:.3f}-{b['y1']:.3f}" for b in bad]
    if best is None:
        return {"status": "unsolved", "window": [0.0, 0.0, 1.0, 1.0],
                "moved": None, "kind": "fallback_whole_panel", "bisected": names}
    cy0 = best[1]
    w = [x0, cy0, x1, cy0 + h]
    rels = {window_relation(w, b)[0] for b in bad}
    kind = ("solved_include" if rels == {"inside"}
            else "solved_exclude" if rels == {"outside"} else "solved_mixed")
    return {"status": "solved", "window": w, "moved": round(abs(cy0 - y0), 4),
            "kind": kind, "bisected": names}


def solve_chapter(root: Path, chapter: str) -> Dict[str, Any]:
    """Solve every bisected card in a chapter's plan; write the report."""
    from .detect import load_boxes
    from .media import video_dir
    from .panels import load_panels

    chapter = require_chapter(chapter)
    plan_path = video_dir(root) / f"chapter_{chapter}.plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UserError(f"No plan for chapter {chapter}; run video plan first.") from None
    if not load_panels(root, chapter):
        raise UserError(f"No panels.json for chapter {chapter}; run video panels first.")

    proposals: List[Dict[str, Any]] = []
    cards = safe = no_boxes = 0
    for shot in plan.get("shots", []):
        for card in shot.get("cards", []):
            cards += 1
            panel_id, window = card.get("panel"), card.get("crop")
            if not panel_id or not window:
                continue
            rec = load_boxes(root, chapter, panel_id)
            if rec is None:
                no_boxes += 1
                continue
            res = solve_card(rec["boxes"], window, card.get("motion"))
            if res["status"] == "already_safe":
                safe += 1
                continue
            proposals.append({"shot": shot.get("id"), "panel": panel_id,
                              "motion": card.get("motion"),
                              "from": [float(v) for v in window], **res})
    report = {
        "chapter": chapter, "cards": cards, "already_safe": safe,
        "cards_without_boxes": no_boxes, "proposals": proposals,
        "note": ("proposals are drafts for the auditor: the solver sees detector "
                 "boxes only (faces and missed SFX glyphs are invisible to it)"),
    }
    out = work_root(root) / "video" / f"ch{chapter}" / "solve.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out, json.dumps(report, indent=1, ensure_ascii=False))
    return report
