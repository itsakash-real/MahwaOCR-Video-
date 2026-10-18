"""Per-panel crop-analysis cache.

`_panel_maps` (crops.py) is the expensive step in every `video plan`: it runs
once per panel in verify, and again in the designer. The common workflow is
plan -> review -> tweak 1-2 entries in fixes.json -> re-plan, and every
re-plan re-analyses all ~130 panels even though 128 are unchanged.

This cache stores the *derived* result (pick_card + head_of + edge_rows) per
panel, keyed by everything that can change it: the panels fingerprint, the
panel's geometry, the candidate shapes, the crop options, and the panel's
face fix (a reviewer's head position changes both the head result and the
candidate ranking). Entries are one small JSON file per key, so a miss writes
exactly one file and a hit reads exactly one -- no whole-file rewrite, no
save hook, and no invalidation pass: a changed input simply hashes to a
different filename. Stale entries are tiny and harmless; a hit means the
caller never loads the panel image at all.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from ..util import atomic_write_text, require_chapter
from ..workspace import work_root


def crop_cache_dir(root: Path, chapter: str) -> Path:
    return work_root(root) / "video" / f"ch{require_chapter(chapter)}" / "crop_cache"


def crop_key(fingerprint: str, panel: Dict[str, Any], shapes: List[float],
             options: Dict[str, Any] | None, face_fix: Any) -> str:
    """Everything the crop analysis depends on, hashed to a cache filename."""
    geo = {k: panel.get(k) for k in ("id", "x0", "x1", "y0", "y1", "segments")}
    raw = json.dumps({"fp": fingerprint, "panel": geo, "shapes": list(shapes or []),
                      "options": options or {}, "face": face_fix},
                     sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def load_crop(root: Path, chapter: str, key: str) -> Dict[str, Any] | None:
    try:
        data = json.loads((crop_cache_dir(root, chapter) / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pick"), dict) \
            or not isinstance(data.get("head"), dict) or "edge_rows" not in data:
        return None
    return data


def store_crop(root: Path, chapter: str, key: str, entry: Dict[str, Any]) -> None:
    try:
        atomic_write_text(crop_cache_dir(root, chapter) / f"{key}.json",
                          json.dumps(entry, sort_keys=True, ensure_ascii=False))
    except OSError:
        pass
