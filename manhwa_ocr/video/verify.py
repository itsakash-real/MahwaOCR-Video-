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


def pick_card(img: Image.Image, shapes: List[float], maps: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """The card for one panel: {"shape", "box", "pad_y", "problems", "clean"}.

    Clean windows beat unclean ones; among equals the lower score wins. The shape choice
    keeps the panel's own shape close. `box` always lies inside the panel; `pad_y` > 0 means
    the card is taller than the box by that many panel-heights above and below (a panel
    too wide for any allowed shape is shown whole on blurred padding, not side-cropped).
    """
    m = maps or _panel_maps(img)
    panel_aspect = img.width / max(1, img.height)
    best = None
    for fallback in (False, True):       # a shape that would cost the panel its sides only if none else works
        for a in shapes:
            for c in shape_candidates(img, a, m, fallback=fallback, allow_pad=True)[:40]:
                probs = problems_of(c)
                cost = (c["score"] + 0.4 * max(0.0, math.log(panel_aspect / a))
                        + 0.02 * abs(math.log(a / panel_aspect)))
                key = (len(probs) > 0, cost)
                if best is None or key < best[0]:
                    best = (key, a, c["box"], c.get("pad_y", 0.0), probs)
        if best is not None:
            break
    _, shape, box, pad_y, probs = best
    return {"shape": shape, "box": box, "pad_y": pad_y, "problems": probs, "clean": not probs}


def pick_crop(img: Image.Image, shapes: List[float], maps: Dict[str, Any] | None = None) -> Tuple[float, Tuple, List[str], bool]:
    """(shape, box, problems, clean) of pick_card(), for callers that don't draw the card
    (a padded card's box is the whole panel; its padding is in pick_card()["pad_y"])."""
    card = pick_card(img, shapes, maps)
    return card["shape"], card["box"], card["problems"], card["clean"]


def verify_chapter(panels: List[Dict[str, Any]], image_for: Callable[[Dict[str, Any]], Image.Image],
                   shapes: List[float], root: Path | None = None, chapter: str | None = None,
                   log: Callable = print, options: Dict[str, Any] | None = None) -> Dict[str, Any]:
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
        maps = _panel_maps(img, options)
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
