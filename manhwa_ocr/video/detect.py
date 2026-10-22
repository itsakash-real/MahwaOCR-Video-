"""Detector — bubble/text boxes per panel (shared step for Builds 3 and 4).

Model: ogkalu/comic-text-and-bubble-detector (RT-DETR v2, Apache-2.0),
run through its int8 ONNX export on CPU. Classes: `bubble` (the balloon),
`text_bubble` (the lettering inside a balloon), `text_free` (free text /
SFX). The export takes the panel squashed to 640x640 plus the original
size and returns boxes already rescaled to original pixels; this module
thresholds them and stores boxes as fractions of the panel, so consumers
(the defect pre-checker, the safe-crop solver, the OCR reader) never care
about pixel sizes.

Boxes are computed once per panel and cached in
.work/video/chN/boxes/<panel>.json, keyed by a hash of the panel image —
re-runs and revisions skip the compute. The model file itself lives
outside the repo (~/.config/manhwa/models/ by default, override with the
MANHWA_DETECTOR_MODEL env var or the video.detector.model config) and
onnxruntime is imported lazily: nothing in the existing pipeline pays for
this step unless it is asked for.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from PIL import Image

from ..util import UserError, atomic_write_text, require_chapter
from ..workspace import work_root

MODEL_ID = "ogkalu/comic-text-and-bubble-detector"
MODEL_REVISION = "16e8a622f91fabc6b5b65c96d32d1183f8843546"
MODEL_FILE = "ogkalu-detector-int8.onnx"
LABELS = {0: "bubble", 1: "text_bubble", 2: "text_free"}
INPUT_SIZE = 640
DEFAULT_THRESHOLD = 0.35

_SESSIONS: Dict[str, Any] = {}


def default_model_path() -> Path:
    env = os.environ.get("MANHWA_DETECTOR_MODEL")
    if env:
        return Path(env)
    return Path.home() / ".config" / "manhwa" / "models" / MODEL_FILE


def get_session(model_path: Path):
    """A cached ONNX session for the model. Clear errors, never a traceback
    about a missing optional dependency."""
    model_path = Path(model_path)
    key = str(model_path)
    if key in _SESSIONS:
        return _SESSIONS[key]
    if not model_path.is_file():
        raise UserError(
            f"Detector model not found: {model_path}\n"
            f"  Download {MODEL_FILE} from https://huggingface.co/{MODEL_ID} "
            f"(revision {MODEL_REVISION[:8]}) into ~/.config/manhwa/models/, "
            "or set MANHWA_DETECTOR_MODEL / video.detector.model.")
    try:
        import onnxruntime as ort
    except ImportError:
        raise UserError(
            "The detector needs the optional 'onnxruntime' package "
            "(pip install onnxruntime). The rest of the pipeline is unaffected."
        ) from None
    sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    _SESSIONS[key] = sess
    return sess


def detect_boxes(img: Image.Image, *, threshold: float = DEFAULT_THRESHOLD,
                 session=None, model_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """All boxes of one panel image, as panel fractions, best score first."""
    if session is None:
        session = get_session(model_path or default_model_path())
    rgb = img.convert("RGB")
    small = rgb.resize((INPUT_SIZE, INPUT_SIZE), Image.BILINEAR)
    arr = (np.asarray(small, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None]
    # NOTE: this export wants orig_target_sizes as [width, height].
    labels, boxes, scores = session.run(
        None, {"images": arr,
               "orig_target_sizes": np.array([[rgb.width, rgb.height]], dtype=np.int64)})
    out = []
    for lab, box, score in zip(labels[0], boxes[0], scores[0]):
        score = float(score)
        if score < threshold or int(lab) not in LABELS:
            continue
        x0, y0, x1, y1 = (float(v) for v in box)
        out.append({
            "label": LABELS[int(lab)],
            "score": round(score, 4),
            "x0": round(min(max(x0, 0.0), rgb.width) / rgb.width, 4),
            "y0": round(min(max(y0, 0.0), rgb.height) / rgb.height, 4),
            "x1": round(min(max(x1, 0.0), rgb.width) / rgb.width, 4),
            "y1": round(min(max(y1, 0.0), rgb.height) / rgb.height, 4),
        })
    out.sort(key=lambda b: -b["score"])
    return out


# ------------------------------------------------------------------ cache


def panel_key(img: Image.Image) -> str:
    rgb = img.convert("RGB")
    return hashlib.sha256(f"{rgb.width}x{rgb.height}:".encode()
                          + rgb.tobytes()).hexdigest()[:20]


def boxes_dir(root: Path, chapter: str) -> Path:
    return work_root(root) / "video" / f"ch{require_chapter(chapter)}" / "boxes"


def boxes_for_panel(root: Path, chapter: str, panel: Dict[str, Any], norm_width: int,
                    *, force: bool = False, threshold: float = DEFAULT_THRESHOLD,
                    model_path: Optional[Path] = None,
                    session=None) -> Dict[str, Any]:
    """The panel's sidecar record, computing and caching it when stale."""
    record, _ = _boxes_for_panel(root, chapter, panel, norm_width, force=force,
                                 threshold=threshold, model_path=model_path,
                                 session=session)
    return record


def _boxes_for_panel(root: Path, chapter: str, panel: Dict[str, Any], norm_width: int,
                     *, force: bool = False, threshold: float = DEFAULT_THRESHOLD,
                     model_path: Optional[Path] = None,
                     session=None) -> tuple[Dict[str, Any], bool]:
    from .panels import panel_image

    model_path = Path(model_path) if model_path else default_model_path()
    path = boxes_dir(root, chapter) / f"{panel['id']}.json"
    img = panel_image(root, chapter, panel, norm_width)
    key = panel_key(img)
    if not force and path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (cached.get("key") == key
                    and cached.get("model", {}).get("file") == model_path.name
                    and abs(float(cached.get("threshold", -1)) - threshold) < 1e-9):
                return cached, False
        except (OSError, ValueError):
            pass
    record = {
        "panel": panel["id"],
        "key": key,
        "model": {"id": MODEL_ID, "revision": MODEL_REVISION, "file": model_path.name},
        "threshold": threshold,
        "boxes": detect_boxes(img, threshold=threshold, session=session,
                              model_path=model_path),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(record, indent=1, ensure_ascii=False))
    return record, True


def load_boxes(root: Path, chapter: str, panel_id: str) -> Optional[Dict[str, Any]]:
    """A panel's cached record, or None. Never computes anything."""
    path = boxes_dir(root, chapter) / f"{panel_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and "boxes" in data else None
    except (OSError, ValueError):
        return None


def detect_chapter(root: Path, chapter: str, *, force: bool = False,
                   threshold: float = DEFAULT_THRESHOLD,
                   model_path: Optional[Path] = None) -> Dict[str, Any]:
    """Run (or reuse) detection for every panel of a chapter."""
    from .panels import load_panels

    chapter = require_chapter(chapter)
    data = load_panels(root, chapter)
    if not data:
        raise UserError(f"No panels.json for chapter {chapter}; run video panels first.")
    model_path = Path(model_path) if model_path else default_model_path()
    session = get_session(model_path)  # fail fast, before any panel work
    computed = cached = boxes = 0
    for p in data["panels"]:
        rec, did_compute = _boxes_for_panel(
            root, chapter, p, data["norm_width"], force=force,
            threshold=threshold, model_path=model_path, session=session)
        boxes += len(rec["boxes"])
        if did_compute:
            computed += 1
        else:
            cached += 1
    return {"chapter": chapter, "panels": len(data["panels"]),
            "computed": computed, "cached": cached, "boxes": boxes}
