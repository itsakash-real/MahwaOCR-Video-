"""Build 3 — OCR reader (RapidOCR): cross-check first, drafting aid second.

Two read-only tools, both writing only under .work/video/chN/:

- ``video ocr-check``: OCR every panel and diff the lines against the
  hand transcript's quoted text. Lines the transcript does NOT contain
  are candidate MISSED dialogue — the one OCR job with proven value.
  OCR wording itself is rough (bake-off: mean WER 0.26 against the
  hand transcript on Solo ch1), so candidates are leads for a
  human/agent to verify against the panel, never auto-insertions.

- ``video ocr-draft``: per panel, the OCR lines grouped by the
  detector's boxes — lines inside a text_bubble box are dialogue,
  the rest are narration/SFX/other. A drafting aid for the transcript
  stage, measured in the bake-off; it does not write transcripts.

RapidOCR is an optional, lazily imported dependency (its own venv,
like the detector's onnxruntime). Lines are cached per panel under
.work/video/chN/ocr/, keyed by the panel-image hash.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..util import UserError, atomic_write_text, require_chapter
from ..workspace import work_root
from .detect import panel_key

MIN_SCORE = 0.5
MIN_MATCH_CHARS = 12     # shorter OCR fragments are too noisy to judge
FOUND_RATIO = 0.80       # difflib ratio vs a transcript line counts as found


# ---------------------------------------------------------------- engine

_engine = None


def get_engine():
    """The RapidOCR engine, or a clean UserError naming the fix."""
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            raise UserError(
                "RapidOCR is not installed in this Python environment. It is an "
                "optional dependency: run video ocr-check / ocr-draft with a "
                "Python that has rapidocr-onnxruntime (pip install "
                "rapidocr-onnxruntime), e.g. a dedicated venv — the production "
                "environment intentionally stays lean."
            ) from None
        _engine = RapidOCR()
    return _engine


def ocr_image(img, engine=None) -> List[Dict[str, Any]]:
    """OCR one image -> [{text, score, box}] with box as panel fractions."""
    import numpy as np

    engine = engine or get_engine()
    result, _elapsed = engine(np.array(img.convert("RGB")))
    lines: List[Dict[str, Any]] = []
    w, h = img.size
    for item in result or []:
        quad, text, score = item[0], str(item[1]), float(item[2])
        if score < MIN_SCORE or not text.strip():
            continue
        xs = [pt[0] / w for pt in quad]
        ys = [pt[1] / h for pt in quad]
        lines.append({"text": text.strip(), "score": round(score, 3),
                      "box": [round(min(xs), 4), round(min(ys), 4),
                              round(max(xs), 4), round(max(ys), 4)]})
    lines.sort(key=lambda ln: (ln["box"][1], ln["box"][0]))
    return lines


# ---------------------------------------------------------------- cache

def ocr_dir(root: Path, chapter: str) -> Path:
    return work_root(root) / "video" / f"ch{chapter}" / "ocr"


def ocr_panel(root: Path, chapter: str, panel: Dict[str, Any], norm_width: int,
              engine=None, force: bool = False) -> List[Dict[str, Any]]:
    """A panel's OCR lines, cached by panel-image hash."""
    from .panels import panel_image

    d = ocr_dir(root, chapter)
    path = d / f"{panel['id']}.json"
    img = panel_image(root, chapter, panel, norm_width)
    key = panel_key(img)
    if not force and path.exists():
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            if rec.get("key") == key:
                return rec["lines"]
        except (OSError, ValueError):
            pass
    lines = ocr_image(img, engine=engine)
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(
        {"panel": panel["id"], "key": key, "lines": lines},
        indent=1, ensure_ascii=False))
    return lines


def load_ocr(root: Path, chapter: str, panel_id: str) -> Optional[List[Dict[str, Any]]]:
    path = ocr_dir(root, chapter) / f"{panel_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))["lines"]
    except (OSError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------- matching

_ws = re.compile(r"[^a-z0-9 ]+")


def normalize(text: str) -> str:
    return " ".join(_ws.sub(" ", text.lower()).split())


_speaker = re.compile(r"^\*\*[^*]+:\*\*\s*(?:\*\([^)]*\)\*\s*)?(.*)$")
_quoted = re.compile(r'"([^"]+)"')


def transcript_lines(root: Path, chapter: str) -> List[str]:
    """The transcript's text lines, in both house formats: `> TEXT` blocks
    and `**Speaker:** "TEXT"` dialogue (an optional *(stage)* tag may sit
    between the speaker and the quote). Description lines never count."""
    path = root / f"chapter_{chapter}.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raise UserError(f"No transcript chapter_{chapter}.md in the session; "
                        "the cross-check needs the hand transcript first.") from None
    out = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        quoted_block = line.startswith(">")
        if quoted_block:
            line = line.lstrip("> ").strip()
        m = _speaker.match(line)
        if m:
            rest = m.group(1)
            segments = _quoted.findall(rest)
            if segments:
                out.extend(s for s in segments if s.strip())
            elif rest.strip():
                out.append(rest.strip('\" '))
        elif quoted_block and line:
            out.append(line)
    return out


def _is_found(norm_line: str, blob: str, norm_lines: List[str],
              squashed_blob: str = "") -> bool:
    if len(norm_line) < MIN_MATCH_CHARS:
        return True  # too short to judge: never a candidate
    if norm_line in blob:
        return True
    # OCR glues words together in stylized lettering ("ANDSHAMANSAND"):
    # compare with every space removed before calling a line missed.
    squashed = norm_line.replace(" ", "")
    if len(squashed) >= MIN_MATCH_CHARS and squashed in squashed_blob:
        return True
    if any(difflib.SequenceMatcher(None, norm_line, tl).ratio() >= FOUND_RATIO
           for tl in norm_lines):
        return True
    # A fragment is only part of its transcript sentence: score it against
    # same-length word windows of each line, not the whole line.
    words = norm_line.split()
    w = len(words)
    if w >= 2:
        for tl in norm_lines:
            tw = tl.split()
            for size in (w - 1, w, w + 1):
                if size < 1 or size > len(tw):
                    continue
                for i in range(len(tw) - size + 1):
                    window = " ".join(tw[i:i + size])
                    if difflib.SequenceMatcher(None, norm_line, window).ratio() >= FOUND_RATIO:
                        return True
    return False


def cross_check(root: Path, chapter: str, engine=None) -> Dict[str, Any]:
    """OCR vs the hand transcript: candidate missed lines per panel."""
    from .panels import load_panels

    chapter = require_chapter(chapter)
    data = load_panels(root, chapter)
    if not data:
        raise UserError(f"No panels.json for chapter {chapter}; run video panels first.")
    tlines = transcript_lines(root, chapter)
    norm_lines = [normalize(t) for t in tlines]
    blob = " " + " ".join(norm_lines) + " "
    squashed_blob = blob.replace(" ", "")

    candidates: List[Dict[str, Any]] = []
    total_lines = 0
    for panel in data["panels"]:
        lines = ocr_panel(root, chapter, panel, data["norm_width"], engine=engine)
        total_lines += len(lines)
        norms = [normalize(ln["text"]) for ln in lines]
        # a bubble's sentence arrives as several fragments: if the panel's
        # joined text is in the transcript, none of its lines is a candidate
        joined_found = _is_found(" ".join(norms), blob, norm_lines, squashed_blob)
        for ln, nl in zip(lines, norms):
            if joined_found or _is_found(nl, blob, norm_lines, squashed_blob):
                continue
            candidates.append({"panel": panel["id"], "text": ln["text"],
                               "score": ln["score"], "box": ln["box"]})
    report = {"chapter": chapter, "panels": len(data["panels"]),
              "ocr_lines": total_lines, "transcript_lines": len(tlines),
              "candidates": candidates,
              "note": ("candidates are OCR lines with no transcript counterpart: "
                       "verify each against its panel before treating it as "
                       "missed dialogue (OCR garbles and SFX are expected noise)")}
    out = work_root(root) / "video" / f"ch{chapter}" / "ocr_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out, json.dumps(report, indent=1, ensure_ascii=False))
    return report


def draft_chapter(root: Path, chapter: str, engine=None) -> Dict[str, Any]:
    """Per-panel draft: OCR lines grouped dialogue vs other by the boxes."""
    from .detect import load_boxes
    from .panels import load_panels

    chapter = require_chapter(chapter)
    data = load_panels(root, chapter)
    if not data:
        raise UserError(f"No panels.json for chapter {chapter}; run video panels first.")

    panels_out: Dict[str, Any] = {}
    for panel in data["panels"]:
        lines = ocr_panel(root, chapter, panel, data["norm_width"], engine=engine)
        rec = load_boxes(root, chapter, panel["id"])
        bubbles = [b for b in (rec["boxes"] if rec else []) if b["label"] == "text_bubble"]
        dialogue, other = [], []
        for ln in lines:
            cx = (ln["box"][0] + ln["box"][2]) / 2.0
            cy = (ln["box"][1] + ln["box"][3]) / 2.0
            inside = any(b["x0"] - 0.02 <= cx <= b["x1"] + 0.02
                         and b["y0"] - 0.02 <= cy <= b["y1"] + 0.02 for b in bubbles)
            (dialogue if inside else other).append(ln["text"])
        panels_out[panel["id"]] = {"dialogue": dialogue, "other": other}
    report = {"chapter": chapter, "panels": panels_out,
              "note": "OCR draft grouped by detector text_bubble boxes; wording is rough"}
    out = work_root(root) / "video" / f"ch{chapter}" / "ocr_draft.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out, json.dumps(report, indent=1, ensure_ascii=False))
    return report
