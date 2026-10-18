"""Fixes for one chapter's video, written by a reviewer and applied by the plan.

Both the IDE agent and the terminal reviewer (review.py) write the same file,
video/chapter_N.fixes.json, and nothing else. The plan reads it:

    {"version": 1,
     "drop": [{"panel": "P016-06", "reason": "sound-effect lettering only"}],
     "face": [{"panel": "P017-02", "top": 0.12, "bottom": 0.46}],
     "crop": [{"panel": "P018-05", "shape": "3:4", "box": [0.0, 0.10, 0.6, 0.85]}]}

Rules: every entry is checked against the chapter's real panels and the
allowed card shapes. A file with any bad entry is rejected as a whole with a
message for each problem, never half-applied. Panel ids for the cards themselves
(cards[k].panel) are never changed here: that is the alignment's decision.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..util import UserError, atomic_write_text, require_chapter
from .frames import LAYOUT_SHAPES
from .media import video_dir

VERSION = 1
ALLOWED_SHAPES = sorted({a for layouts in LAYOUT_SHAPES.values() for shapes in layouts.values() for a in shapes})
SHAPE_NAMES = {0.5625: "9:16", 0.75: "3:4", 1.0: "1:1", 1.333: "4:3", 1.777: "16:9"}


def fixes_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.fixes.json"


def empty() -> Dict[str, Any]:
    return {"version": VERSION, "drop": [], "face": [], "crop": []}


def _shape(value: Any) -> float | None:
    """'3:4' or 0.75 -> the allowed shape value, or None."""
    if isinstance(value, str) and ":" in value:
        try:
            w, h = (float(x) for x in value.split(":"))
            value = w / h
        except (ValueError, ZeroDivisionError):
            return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return next((a for a in ALLOWED_SHAPES if abs(v - a) < 0.01), None)


def validate(data: Any, panel_dims: Dict[str, Tuple[float, float]]) -> Tuple[Dict[str, Any], List[str]]:
    """Returns (clean, problems). `clean` holds plain lookups the plan can use:
    {"drop": {pid: reason}, "face": {pid: (top, bottom)}, "crop": {pid: (shape, box)}}."""
    problems: List[str] = []
    clean: Dict[str, Any] = {"drop": {}, "face": {}, "crop": {}}
    if not isinstance(data, dict):
        return clean, ["the file must be a JSON object"]
    if data.get("version", VERSION) != VERSION:
        problems.append(f"version must be {VERSION}")

    for i, e in enumerate(data.get("drop") or []):
        pid = e.get("panel") if isinstance(e, dict) else None
        if pid not in panel_dims:
            problems.append(f"drop[{i}]: unknown panel {pid!r}")
            continue
        clean["drop"][pid] = str((e.get("reason") or "")).strip() or "no reason given"

    for i, e in enumerate(data.get("face") or []):
        pid = e.get("panel") if isinstance(e, dict) else None
        if pid not in panel_dims:
            problems.append(f"face[{i}]: unknown panel {pid!r}")
            continue
        try:
            top, bottom = float(e["top"]), float(e["bottom"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"face[{i}] ({pid}): needs numbers \"top\" and \"bottom\" between 0 and 1")
            continue
        if not (0.0 <= top < bottom <= 1.0):
            problems.append(f"face[{i}] ({pid}): need 0 <= top < bottom <= 1, got {top}, {bottom}")
            continue
        clean["face"][pid] = (top, bottom)

    for i, e in enumerate(data.get("crop") or []):
        pid = e.get("panel") if isinstance(e, dict) else None
        if pid not in panel_dims:
            problems.append(f"crop[{i}]: unknown panel {pid!r}")
            continue
        shape = _shape(e.get("shape"))
        if shape is None:
            problems.append(f"crop[{i}] ({pid}): shape must be one of {', '.join(SHAPE_NAMES.values())}")
            continue
        box = e.get("box")
        try:
            x0, y0, x1, y1 = (float(v) for v in box)
        except (TypeError, ValueError):
            problems.append(f"crop[{i}] ({pid}): box must be [x0, y0, x1, y1]")
            continue
        if not (0.0 <= x0 < x1 - 0.05 and 0.0 <= y0 < y1 - 0.05 and x1 <= 1.0 and y1 <= 1.0):
            problems.append(f"crop[{i}] ({pid}): box must sit inside 0..1 and be at least 5% each way")
            continue
        w, h = panel_dims[pid]
        aspect = ((x1 - x0) * w) / max(1e-9, (y1 - y0) * h)
        if abs(math.log(aspect / shape)) > 0.02:
            problems.append(f"crop[{i}] ({pid}): box is {aspect:.2f} wide/high but shape is {SHAPE_NAMES[shape]}; "
                            "adjust the box so it keeps that shape")
            continue
        clean["crop"][pid] = (shape, (x0, y0, x1, y1))
    return clean, problems


def load(root: Path, chapter: str, panel_dims: Dict[str, Tuple[float, float]]) -> Dict[str, Any]:
    """The chapter's fixes as plan lookups. No file means no fixes. Raises UserError on bad entries."""
    path = fixes_path(root, chapter)
    if not path.exists():
        return validate(empty(), panel_dims)[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UserError(f"{path.name} is not valid JSON ({exc}). Fix the file, then rebuild the plan.") from None
    clean, problems = validate(data, panel_dims)
    if problems:
        raise UserError(f"{path.name} has problems (nothing from it was applied):\n  - " + "\n  - ".join(problems))
    return clean


def save(root: Path, chapter: str, data: Dict[str, Any]) -> Path:
    path = fixes_path(root, chapter)
    atomic_write_text(path, json.dumps(data, indent=1, ensure_ascii=False))
    return path
