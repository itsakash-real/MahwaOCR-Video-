"""Checks every card against its pixels before the video is rendered. No AI.

For each panel the crop search (crops.shape_candidates) gives ranked windows
for each allowed card shape. A window is clean when it shows none of:

  lettering  speech-bubble or caption lettering inside the card
  painted    a patch left where a bubble was painted out
  blank      mostly flat white/empty (a white screen)
  head_cut   a detected head would be cut (top or chin outside the card)

The first clean window wins. If a panel has no clean window:
  - a panel with a character is never dropped: the best window is kept and
    marked "needs eyes" for the reviewer;
  - a panel with no character (lettering-only, blank, logo) is dropped and
    listed with its page and panel id, so nothing silently goes missing.

The designer (plan.py) uses pick_crop() for the same decision, so the report
and the video agree.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from PIL import Image

from .crops import BUBBLE_OK, FIGURE_MIN, _panel_maps, figure_share, shape_candidates
from ..util import atomic_write_text, require_chapter
from .media import video_dir

PAINTED_OK = 0.02
BLANK_MAX = 0.55
TEXT_MAX = 0.75      # share of a window that is lettering strokes or flat white


def problems_of(c: Dict[str, Any]) -> List[str]:
    out = []
    if not c["bubble_ok"] or c["texty"] > TEXT_MAX:
        out.append("lettering")          # a bubble, or a window that is mostly lettering strokes
    if c["painted"] > PAINTED_OK:
        out.append("painted")
    if c["blank"] > BLANK_MAX or c["soft"] < 0.02:
        out.append("blank")
    if not c["head_ok"]:
        out.append("head_cut")
    return out


def pick_crop(img: Image.Image, shapes: List[float], maps: Dict[str, Any] | None = None) -> Tuple[float, Tuple, List[str], bool]:
    """(shape, box, problems, clean). Clean windows beat unclean ones; among equals
    the lower score wins. The shape choice keeps the panel's own shape close."""
    m = maps or _panel_maps(img)
    gw, gh = m["soft"].shape[1], m["soft"].shape[0]
    panel_aspect = gw / gh
    best = None
    for a in shapes:
        for c in shape_candidates(img, a, m)[:40]:
            probs = problems_of(c)
            cost = c["score"] + 0.4 * max(0.0, math.log(panel_aspect / a)) + 0.02 * abs(math.log(a / panel_aspect))
            key = (len(probs) > 0, cost)
            if best is None or key < best[0]:
                best = (key, a, c["box"], probs)
    _, shape, box, probs = best
    return shape, box, probs, not probs


def verify_chapter(panels: List[Dict[str, Any]], image_for: Callable[[Dict[str, Any]], Image.Image],
                   shapes: List[float], root: Path | None = None, chapter: str | None = None,
                   log: Callable = print) -> Dict[str, Any]:
    """Check every drawn panel. Returns the report (also written to
    video/chapter_N.verify.json when root and chapter are given)."""
    rows: Dict[str, Any] = {}
    dropped: List[Dict[str, Any]] = []
    needs_eyes: List[str] = []
    recropped: List[str] = []
    for p in panels:
        if p.get("text_only"):
            continue
        img = image_for(p)
        maps = _panel_maps(img)
        shape, box, probs, clean = pick_crop(img, shapes, maps)
        figure = figure_share(img) >= FIGURE_MIN
        if clean:
            action = "ok"
        elif figure:
            action = "needs eyes" if "head_cut" in probs else "kept (check)"
            if "head_cut" in probs:
                needs_eyes.append(p["id"])
        else:
            action = "dropped"
            dropped.append({"panel": p["id"], "reason": ", ".join(probs) or "no character"})
        rows[p["id"]] = {"shape": shape, "box": list(box), "problems": probs, "action": action,
                         "figure": figure}
        if action == "dropped":
            log(f"    {p['id']}: {', '.join(probs) or 'no character'} -> dropped")
    report = {"chapter": chapter, "panels": rows, "dropped": dropped,
              "needs_eyes": needs_eyes, "recropped": recropped}
    if root is not None and chapter is not None:
        atomic_write_text(video_dir(root) / f"chapter_{require_chapter(chapter)}.verify.json",
                          json.dumps(report, indent=1, ensure_ascii=False))
    return report
