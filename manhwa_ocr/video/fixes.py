"""Fixes for one chapter's video, written by a reviewer and applied by the plan.

Both the IDE agent and the terminal reviewer (review.py) write the same file,
video/chapter_N.fixes.json, and nothing else. The plan reads it:

    {"version": 1,
     "drop": [{"panel": "P016-06", "reason": "sound-effect lettering only"}],
     "face": [{"panel": "P017-02", "top": 0.12, "bottom": 0.46}],
     "crop": [{"panel": "P018-05", "shape": "3:4", "box": [0.0, 0.10, 0.6, 0.85]}],
     "shot": [{"panel": "P019-01", "background": "black", "enter": "fade_in"}],
     "rotate": [{"panel": "P012-08", "degrees": 90}],
     "chapter_cards": [{"shot_id": "S012", "title": "The Exam That Kills"}],
     "callouts": [{"shot_id": "S007", "text": "SS-RANK"}]}

"shot" overrides how a panel is presented: "background" (blur or black) applies to the shot whose
first card is that panel, "enter" (how the card arrives) to that panel's card. Both are optional.
"rotate" turns a panel that the source drew sideways upright: "degrees" is CLOCKWISE (90, 180 or
270), applied to the panel image before either panelfit layer is built, with the fit dimensions
swapped to match. Panelfit mode only — classic cards ignore it. Slice hints (cues.json) cut the
UNROTATED panel; the kept band is then rotated whole.
Everything a person or agent could once only change by hand-editing chapter_N.plan.json (a drop,
a head position, a crop, a background, an entrance) belongs here, because `video plan --force`
rebuilds the plan and re-applies this file every time. The plan itself is presentation only.

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
from .frames import BACKGROUNDS, ENTRANCES, LAYOUT_SHAPES
from .media import video_dir

VERSION = 1
ALLOWED_SHAPES = sorted({a for layouts in LAYOUT_SHAPES.values() for shapes in layouts.values() for a in shapes})
SHAPE_NAMES = {0.5625: "9:16", 0.75: "3:4", 1.0: "1:1", 1.333: "4:3", 1.777: "16:9"}


def fixes_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.fixes.json"


def empty() -> Dict[str, Any]:
    return {"version": VERSION, "drop": [], "face": [], "crop": [], "shot": [],
            "rotate": [],
            # P5: reviewer overrides for chapter-card titles and callout text,
            # keyed by shot id (e.g. "S012"). See shorts.validate_override_entry.
            "chapter_cards": [], "callouts": []}


ROTATE_DEGREES = (90, 180, 270)   # clockwise; the only sideways corrections a
                                  # panel can need — anything else is a stretch


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
    {"drop": {pid: reason}, "face": {pid: (top, bottom)}, "crop": {pid: (shape, box)},
     "shot": {pid: {"background": ..., "enter": ...}}}."""
    problems: List[str] = []
    clean: Dict[str, Any] = {"drop": {}, "face": {}, "crop": {}, "shot": {},
                             "rotate": {}, "chapter_cards": {}, "callouts": {}}
    if not isinstance(data, dict):
        return clean, ["the file must be a JSON object"]
    if data.get("version", VERSION) != VERSION:
        problems.append(f"version must be {VERSION}")

    for i, e in enumerate(data.get("rotate") or []):
        pid = e.get("panel") if isinstance(e, dict) else None
        if pid not in panel_dims:
            problems.append(f"rotate[{i}]: unknown panel {pid!r}")
            continue
        deg = e.get("degrees")
        if isinstance(deg, bool) or deg not in ROTATE_DEGREES:
            problems.append(f"rotate[{i}] ({pid}): degrees must be one of "
                            f"{', '.join(str(d) for d in ROTATE_DEGREES)} (clockwise)")
            continue
        clean["rotate"][pid] = int(deg)

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

    for i, e in enumerate(data.get("shot") or []):
        pid = e.get("panel") if isinstance(e, dict) else None
        if pid not in panel_dims:
            problems.append(f"shot[{i}]: unknown panel {pid!r}")
            continue
        entry: Dict[str, str] = {}
        bg, enter = e.get("background"), e.get("enter")
        if bg is not None and bg not in BACKGROUNDS:
            problems.append(f"shot[{i}] ({pid}): background must be one of {', '.join(BACKGROUNDS)}")
            continue
        if enter is not None and enter not in ENTRANCES:
            problems.append(f"shot[{i}] ({pid}): enter must be one of {', '.join(ENTRANCES)}")
            continue
        if bg is None and enter is None:
            problems.append(f"shot[{i}] ({pid}): give a \"background\" or an \"enter\" (or both)")
            continue
        if bg is not None:
            entry["background"] = bg
        if enter is not None:
            entry["enter"] = enter
        clean["shot"][pid] = entry

    # P5: chapter-card title and callout text overrides, keyed by shot id.
    # A duplicate shot_id is ambiguous (which title wins?) so it is a
    # problem, like every other bad fixes.json entry: nothing is applied.
    from .shorts import validate_override_entry

    for kind, field in (("chapter_cards", "title"), ("callouts", "text")):
        seen: set = set()
        for i, e in enumerate(data.get(kind) or []):
            errs = validate_override_entry(kind, e)
            for err in errs:
                problems.append(f"{kind}[{i}]: {err}")
            if errs:
                continue
            sid = e.get("shot_id")
            if sid in seen:
                problems.append(f"{kind}[{i}]: duplicate shot_id {sid!r} (one override per shot)")
                continue
            seen.add(sid)
            clean[kind][sid] = e.get(field)
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
