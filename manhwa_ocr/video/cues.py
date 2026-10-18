"""Panel tags in recap narration.

The recap writer starts each sentence with the panel it describes:

    {P003-02} He finally draws the sword. {same} *Nobody* expected that.
    {P004-01,P004-02} {impact} The blast tears through the hall.

From that, three things are produced:

    recaps/chapter_N.md        what you read: tags removed, editing cues kept
    recaps/chapter_N.tts.txt   what you paste into a TTS tool: plain spoken text
    recaps/chapter_N.cues.json sentence -> panels, cues, emphasised words

The spoken sentences in cues.json are exactly the text in tts.txt, which is
what makes aligning them to the recorded audio reliable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Set

from ..util import atomic_write_text, require_chapter

TAG = re.compile(r"\{([^{}\n]{1,120})\}")
PANEL_ID = re.compile(r"^P\d{3}-\d{2}$")
CUE_WORDS = {"beat", "impact", "reveal"}
CUE_LINE = re.compile(r"^\s*\[[^\]]*\]\s*$")
INLINE_CUE = re.compile(r"\[[^\]\n]*\]")
SENTENCE_END = re.compile(r"(?<=[.!?…])[\"”’)\]]*\s+")


def strip_tags(text: str) -> str:
    out = TAG.sub("", text or "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r" +([.,!?;:])", r"\1", out).strip()


def _spoken(text: str) -> str:
    """Narration as it is read aloud: no tags, cues, or markdown."""
    t = TAG.sub("", text)
    t = INLINE_CUE.sub("", t)
    t = re.sub(r"[*_`#>]+", "", t)
    t = re.sub(r"\s+", " ", t)
    return re.sub(r" +([.,!?;:])", r"\1", t).strip()


def tts_text(narration: str) -> str:
    paragraphs = []
    for block in re.split(r"\n\s*\n", narration or ""):
        lines = [ln for ln in block.splitlines() if ln.strip() and not CUE_LINE.match(ln)]
        spoken = _spoken(" ".join(lines))
        if spoken:
            paragraphs.append(spoken)
    return "\n\n".join(paragraphs) + "\n"


def parse(narration: str, known_panels: Set[str] | None = None) -> Dict[str, Any]:
    """Split tagged narration into spoken sentences with their panels and cues."""
    body = "\n".join(ln for ln in (narration or "").splitlines() if not CUE_LINE.match(ln))
    body = re.sub(r"\s+", " ", body).strip()
    sentences: List[Dict[str, Any]] = []
    unknown: List[str] = []
    tagged = 0
    for chunk in SENTENCE_END.split(body):
        chunk = chunk.strip()
        if not chunk:
            continue
        panels, cues = [], []
        for raw in TAG.findall(chunk):
            for tok in re.split(r"[,\s]+", raw.strip()):
                if not tok:
                    continue
                low = tok.lower()
                if low == "same":
                    panels.append("same")
                elif low in CUE_WORDS:
                    cues.append(low)
                elif PANEL_ID.match(tok.upper()):
                    pid = tok.upper()
                    if known_panels is None or pid in known_panels:
                        panels.append(pid)
                    else:
                        unknown.append(pid)
        emphasis = [_spoken(m) for m in re.findall(r"\*{1,2}([^*\n]{1,60})\*{1,2}", chunk)]
        text = _spoken(chunk)
        if not text:
            # A tag-only fragment: carry its tags onto the next sentence.
            if sentences is not None and (panels or cues):
                sentences.append({"text": "", "panels": panels, "cues": cues, "emphasis": []})
            continue
        if sentences and not sentences[-1]["text"]:
            carry = sentences.pop()
            panels = carry["panels"] + panels
            cues = carry["cues"] + cues
        if any(p != "same" for p in panels):
            tagged += 1
        sentences.append({"text": text, "panels": [p for p in panels if p != "same"],
                          "same": "same" in panels and not any(p != "same" for p in panels),
                          "cues": sorted(set(cues)), "emphasis": [e for e in emphasis if e]})
    sentences = [s for s in sentences if s["text"]]
    return {
        "sentences": sentences,
        "tagged_ratio": round(tagged / len(sentences), 3) if sentences else 0.0,
        "unknown_panels": sorted(set(unknown)),
    }


def has_tags(narration: str) -> bool:
    return any(PANEL_ID.match(t.split(",")[0].strip().upper()) for t in TAG.findall(narration or ""))


def cues_path(root: Path, chapter: str) -> Path:
    return Path(root) / "recaps" / f"chapter_{require_chapter(chapter)}.cues.json"


def tts_path(root: Path, chapter: str) -> Path:
    return Path(root) / "recaps" / f"chapter_{require_chapter(chapter)}.tts.txt"


def write_outputs(root: Path, chapter: str, narration: str, known_panels: Set[str] | None) -> Dict[str, Any] | None:
    """Write tts.txt always, and cues.json when the narration carries panel tags."""
    atomic_write_text(tts_path(root, chapter), tts_text(narration))
    cp = cues_path(root, chapter)
    if not has_tags(narration):
        if cp.exists():
            cp.unlink()  # stale cues from an earlier tagged version
        return None
    data = {"version": 1, "chapter": chapter, **parse(narration, known_panels)}
    atomic_write_text(cp, json.dumps(data, indent=1, ensure_ascii=False))
    return data


def load_cues(root: Path, chapter: str) -> Dict[str, Any] | None:
    try:
        data = json.loads(cues_path(root, chapter).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("sentences") else None
