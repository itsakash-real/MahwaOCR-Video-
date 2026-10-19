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

from .crops import (BUBBLE_OK, FIGURE_MIN, PATCH_VISIBLE, _panel_maps,
                    head_of, is_mask_risk, patch_visibility, shape_candidates)
from .cropcache import crop_key, load_crop, store_crop
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
    risk = is_mask_risk(m)
    best = None
    for fallback in (False, True):       # a shape that would cost the panel its sides only if none else works
        for a in shapes:
            for c in shape_candidates(img, a, m, fallback=fallback, allow_pad=True)[:40]:
                probs = problems_of(c)
                # P1: a mask-risk panel whose best window still overlaps a
                # bubble would mask a face if painted -> flag it so the
                # caller drops the panel instead of masking it. A clean
                # window (avoids the bubble) has no mask problem.
                if risk and not c["bubble_ok"] and "mask_risk" not in probs:
                    probs = [*probs, "mask_risk"]
                cost = (c["score"] + 0.4 * max(0.0, math.log(panel_aspect / a))
                        + 0.02 * abs(math.log(a / panel_aspect)))
                # P1 auto-switch: fewer problems always beats a nicer shape
                # (a clean 3:4 beats a 4:3 with a bubble/head-cut), and on a
                # tie the narrower shape wins (safer in pair layouts: the Ch4
                # lesson was a 4:3 crop failing once the panel landed in a pair).
                key = (len(probs), cost, a)
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


def layout_crop_problems(img: Image.Image, maps: Dict[str, Any] | None,
                          layout_shapes: Dict[str, List[float]] | None) -> Dict[str, List[str]]:
    """Per-layout crop problems for one panel (P1 layout-sensitive QA).

    `layout_shapes`: layout name -> allowed shapes for that layout
    (e.g. {"single": [...], "pair": [...]} from frames.shapes_for).
    Returns {layout: problems} using pick_card with that layout's shapes
    only. A panel clean in "single" but not in "pair" is layout-sensitive:
    its single-layout crop must not be trusted for a pair shot (Ch4
    lesson: a 4:3 crop passing in a single shot failed in a pair, fixed
    by switching to 3:4). Returns {} when layout_shapes is None.
    """
    if not layout_shapes:
        return {}
    out: Dict[str, List[str]] = {}
    for layout, shapes in layout_shapes.items():
        card = pick_card(img, list(shapes), maps)
        out[layout] = list(card["problems"])
    return out


def find_pair_safe_shape(img: Image.Image, maps: Dict[str, Any] | None,
                           layout_shapes: Dict[str, List[float]] | None,
                           layout: str = "single") -> Dict[str, Any] | None:
    """The auto-switch for a panel whose single/wide crop and pair crop are both
    clean but different shapes (P2).

    P1 only flagged layout-sensitive crops (clean in single, problems in pair).
    The remaining gap was consistency: the planner picked one crop per layout,
    so the same panel could show a 4:3 crop in a single shot and a 3:4 crop in
    a pair shot, and verify/QA judged a different crop than the one rendered
    (Ch4: a 4:3 crop passing in a single shot failed once the panel landed in
    a pair — fixed by hand by switching to 3:4). When BOTH layouts have a
    clean crop but disagree on shape, the planner now uses the pair-safe
    (narrower) shape everywhere, so one crop passes QA in every layout.

    Returns {"from_shape", "to_shape", "box", "pad_y", "reason"} or None when
    no switch applies (either layout has no clean crop — that stays a
    reviewer decision, never a silent switch to a problematic crop — or the
    shapes already agree). `maps` may be None (computed here).
    """
    if not layout_shapes or layout not in layout_shapes or "pair" not in layout_shapes:
        return None
    m = maps or _panel_maps(img)
    main = pick_card(img, list(layout_shapes[layout]), m)
    pair = pick_card(img, list(layout_shapes["pair"]), m)
    if not main["clean"] or not pair["clean"]:
        return None
    if abs(float(main["shape"]) - float(pair["shape"])) < 1e-9:
        return None
    return {"from_shape": float(main["shape"]), "to_shape": float(pair["shape"]),
            "box": list(pair["box"]), "pad_y": float(pair.get("pad_y") or 0.0),
            "reason": (f"{layout} crop {main['shape']:.3f} and pair crop {pair['shape']:.3f} "
                       f"are both clean; using the pair-safe {pair['shape']:.3f} everywhere "
                       f"so one crop passes QA in every layout (Ch4: 4:3 failing in a pair -> 3:4)")}


def verify_chapter(panels: List[Dict[str, Any]], image_for: Callable[[Dict[str, Any]], Image.Image],
                   shapes: List[float], root: Path | None = None, chapter: str | None = None,
                   log: Callable = print, options: Dict[str, Any] | None = None,
                   fingerprint: str | None = None,
                   layout_shapes: Dict[str, List[float]] | None = None) -> Dict[str, Any]:
    """Check every drawn panel. Returns the report (also written to
    video/chapter_N.verify.json when root and chapter are given)."""
    rows: Dict[str, Any] = {}
    dropped: List[Dict[str, Any]] = []
    needs_eyes: List[str] = []
    recropped: List[str] = []
    low_confidence: List[Dict[str, Any]] = []
    mask_risk: List[str] = []
    layout_sensitive: List[str] = []
    layout_problems: Dict[str, Dict[str, List[str]]] = {}
    layout_switches: Dict[str, Dict[str, Any]] = {}
    visible_patches: List[Dict[str, Any]] = []
    for p in panels:
        if p.get("text_only"):
            continue
        # Crop cache: on a hit the panel image is never even loaded.
        card = head = None
        ckey = None
        cached_risk = None
        panel_layout_problems: Dict[str, List[str]] = {}
        panel_patch = 0.0
        panel_switch: Dict[str, Any] | None = None
        if root is not None and chapter is not None and fingerprint:
            ckey = crop_key(fingerprint, p, shapes, options, None)
            cached = load_crop(root, chapter, ckey)
            if cached is not None:
                card, head = cached["pick"], cached["head"]
                # P1: cache entries written before P1 lack "mask_risk"; an
                # entry without it is upgraded below (image reloaded once,
                # then re-stored with the flag) so drop decisions stay
                # stable across cached runs.
                cached_risk = cached.get("mask_risk")
                # P2: cache entries written before P2 lack "layout_switch";
                # missing key means "unknown", recomputed below on the
                # fresh path only when the image is already being loaded.
                panel_switch = cached.get("layout_switch")
        if card is None or cached_risk is None:
            img = image_for(p)
            maps = _panel_maps(img, options)
            if card is None:
                card = pick_card(img, shapes, maps)
                head = head_of(maps)
            cached_risk = bool(is_mask_risk(maps))
            if layout_shapes is not None:
                panel_layout_problems = layout_crop_problems(img, maps, layout_shapes)
                # P2: the planner applies this switch (pair-safe shape);
                # verify reports it so the reviewer sees what will change.
                panel_switch = find_pair_safe_shape(img, maps, layout_shapes)
            panel_patch = patch_visibility(img, list(maps.get("painted") or []))
            if ckey is not None:
                store_crop(root, chapter, ckey,
                           {"pick": card, "head": head, "edge_rows": list(maps["edge_rows"]),
                            "mask_risk": cached_risk, "layout_switch": panel_switch})
        shape, box, probs, clean = card["shape"], card["box"], card["problems"], card["clean"]
        figure = bool(head["figure"])           # skin colour, or a pale/gray head blob
        if not head["confident"] and (head["source"] == "pale" or head["keep_top"]):
            low_confidence.append({"panel": p["id"], "reason": (
                "head found by shape and brightness, not skin colour (white/silver hair or gray skin): "
                "check it is whole in the card" if head["source"] == "pale" else
                "a character is in the panel but no head was located: the card keeps the top "
                "of the panel, check the head is whole")})
        panel_risk = bool(cached_risk)
        if panel_risk:
            mask_risk.append(p["id"])
        if panel_layout_problems:
            layout_problems[p["id"]] = panel_layout_problems
            single_probs = panel_layout_problems.get("single") or []
            pair_probs = panel_layout_problems.get("pair")
            if pair_probs is not None and not single_probs and pair_probs:
                layout_sensitive.append(p["id"])
        if panel_switch is not None:
            layout_switches[p["id"]] = panel_switch
        if panel_patch > PATCH_VISIBLE:
            visible_patches.append({"panel": p["id"], "visibility": panel_patch})
        if clean:
            action = "ok"
        elif panel_risk and "mask_risk" in probs:
            # P1: shouting face + dominant bubble with no clean crop.
            # Never mask the face in render: drop the panel and let
            # build.py re-pair its time to a neighbour (Ch2 P018-04/P020-04
            # were fixed exactly this way by hand).
            action = "dropped"
            dropped.append({"panel": p["id"],
                            "reason": "shouting face + dominant bubble would mask the face "
                                      "(no clean crop); drop and re-pair"})
        elif figure:
            action = "needs eyes" if "head_cut" in probs else "kept (check)"
            if "head_cut" in probs:
                needs_eyes.append(p["id"])
        else:
            action = "dropped"
            dropped.append({"panel": p["id"], "reason": ", ".join(probs) or "no character"})
        rows[p["id"]] = {"shape": shape, "box": list(box), "problems": probs, "action": action,
                         "figure": figure, "head": head["source"], "head_confident": bool(head["confident"]),
                         "mask_risk": panel_risk,
                         "layout_problems": panel_layout_problems,
                         "patch_visibility": panel_patch}
        if action == "dropped":
            log(f"    {p['id']}: {', '.join(probs) or 'no character'} -> dropped")
    report = {"chapter": chapter, "panels": rows, "dropped": dropped,
              "needs_eyes": needs_eyes, "recropped": recropped,
              # P1: mask-risk panels (dropped when no clean crop exists),
              # layout-sensitive crops (clean in single, not in pair), and
              # visible painted patches (reviewer warning, not a drop).
              "mask_risk": mask_risk, "layout_sensitive": layout_sensitive,
              "layout_problems": layout_problems, "layout_switches": layout_switches,
              "visible_patches": visible_patches,
              # Heads the heuristic is not sure of (it cannot be): where a reviewer should look first.
              "low_confidence_heads": low_confidence}
    if root is not None and chapter is not None:
        atomic_write_text(video_dir(root) / f"chapter_{require_chapter(chapter)}.verify.json",
                          json.dumps(report, indent=1, ensure_ascii=False))
    return report
