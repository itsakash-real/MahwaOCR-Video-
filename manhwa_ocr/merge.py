"""Flatten per-tile extraction into one text payload, dropping overlap repeats.

Consecutive tiles overlap by a strip of ~180px, so a speech bubble sitting in
that strip is transcribed twice. Only the *edge* blocks can be repeats: the
last few of one tile against the first few of the next. Comparing whole tiles
would also delete lines a character legitimately says twice ("No!" ... "No!").
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Dict, Iterable, List, Tuple

EDGE_BLOCKS = 4


def _norm(text: str) -> str:
    return re.sub(r"[^\w ]", "", (text or "").casefold()).strip()


def _blocks(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    blocks = entry.get("blocks") if isinstance(entry, dict) else None
    if not isinstance(blocks, list):
        return []
    out = []
    for b in blocks:
        if isinstance(b, str):
            b = {"type": "dialogue", "speaker": None, "text": b}
        if isinstance(b, dict) and str(b.get("text") or "").strip():
            out.append(b)
    return out


def flatten(tiles: Iterable[Tuple[str, Dict[str, Any] | None]], threshold: float = 0.86) -> str:
    """tiles: [(label, entry_or_None), ...] in reading order."""
    lines: List[str] = []
    prev_tail: List[str] = []

    for label, entry in tiles:
        lines.append(f"\n--- {label} ---")
        if not entry:
            lines.append("[missing from results]")
            prev_tail = []
            continue
        scene = entry.get("scene") if isinstance(entry, dict) else ""
        if scene:
            lines.append(f"[scene] {str(scene).strip()}")

        blocks = _blocks(entry)
        normed = [_norm(str(b.get("text"))) for b in blocks]
        for i, (block, n) in enumerate(zip(blocks, normed)):
            # Short tokens ("...", "!?") are too ambiguous to call duplicates.
            # Require a long, near-identical match: prefer a visible duplicate
            # over silently deleting a distinct line like "I will kill you".
            if (
                i < EDGE_BLOCKS
                and len(n) >= 8
                and any(difflib.SequenceMatcher(None, n, p).ratio() >= max(threshold, 0.92) for p in prev_tail)
            ):
                continue
            speaker = block.get("speaker") or "-"
            btype = block.get("type") or "dialogue"
            flag = " [truncated]" if block.get("truncated") else ""
            lines.append(f"({btype}) {speaker}: {str(block.get('text')).strip()}{flag}")
        prev_tail = [n for n in normed[-EDGE_BLOCKS:] if len(n) >= 8]

    return "\n".join(lines)
