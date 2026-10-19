"""Auto-captions: CapCut-style burned-in subtitles for chapter videos.

One caption at a time, 3-5 words, bottom centre, key word yellow, black
outline -- the same look the intro video's burned-in subtitles used,
generalised to chapter videos. Captions are FULL text (every word of the
narration), unlike callouts, which stay keyword-only: the two never double
up, because a callout is one keyword on an impact/reveal peak and a caption
is the sentence itself.

Flow: plan.py builds plan["captions"] from the shots (captions on by
default for NEW builds, video.captions: true). render.py writes
video/chapter_N.captions.ass from them and burns it in with ffmpeg's
subtitles filter in a final pass -- captions never touch the per-shot
segment cache, so rewording captions never re-renders a shot. Old plans
have no "captions" key and render without captions unless the user runs
`video captions --chapter N`, which adds them to the plan + writes the
.ass. The .ass is also a standalone artefact: any player that loads
external subtitles shows the same captions.

Style: bottom band (Alignment 2, MarginV 80 at 1080p), never over faces
(faces sit in the upper part of a card; captions sit in the bottom band
where cards show torso / background). Key word = the longest word in the
chunk, yellow; the rest white.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from ..util import require_chapter
from .media import video_dir

CAPTION_MAX_WORDS = 5
CAPTION_MIN_WORDS = 3
CAPTION_MARGIN_V = 80      # bottom band, px at 1080p (scaled by frame height)
CAPTION_FONT_SIZE = 46     # px at 1080p (scaled by frame height)
KEY_COLOUR = "&H00FFFF&"   # ASS BGR: yellow
TEXT_COLOUR = "&HFFFFFF&"  # ASS BGR: white

_WORD = re.compile(r"[A-Za-z0-9']+")


def _key_word(chunk: str) -> str:
    """The chunk's key word: longest word, ties go to the first."""
    words = chunk.split()
    if not words:
        return ""
    return max(words, key=lambda w: len(_WORD.search(w).group(0)) if _WORD.search(w) else 0)


def split_sentence_to_chunks(text: str) -> List[str]:
    """Split one sentence into 3-5 word chunks (evenly balanced).

    A sentence of <=5 words is one chunk. Longer sentences split into
    ceil(n/5) chunks as evenly as possible, so no chunk is under 3 words
    (6 words -> 3+3, 11 -> 4+4+3) and none is over 5.
    """
    words = (text or "").split()
    if not words:
        return []
    if len(words) <= CAPTION_MAX_WORDS:
        return [" ".join(words)]
    n = -(-len(words) // CAPTION_MAX_WORDS)  # ceil
    base, rem = divmod(len(words), n)
    chunks, at = [], 0
    for k in range(n):
        take = base + (1 if k < rem else 0)
        chunks.append(" ".join(words[at:at + take]))
        at += take
    return chunks


def build_captions(shots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Captions for a plan's shots: {text, key, start, end, shot}.

    Each shot's sentence is chunked 3-5 words and the chunks spread evenly
    across the shot, so every caption lands on the words being spoken.
    """
    out: List[Dict[str, Any]] = []
    for s in shots:
        sentence = (s.get("sentence") or "").strip()
        if not sentence:
            continue
        try:
            start, end = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        chunks = split_sentence_to_chunks(sentence)
        if not chunks:
            continue
        step = (end - start) / len(chunks)
        for k, chunk in enumerate(chunks):
            out.append({"text": chunk, "key": _key_word(chunk),
                        "start": round(start + k * step, 3),
                        "end": round(start + (k + 1) * step, 3),
                        "shot": s.get("id")})
    return out


def validate_captions(captions: List[Dict[str, Any]], duration: float) -> List[str]:
    """Problems with a captions list, in plain words."""
    errs: List[str] = []
    by_shot: Dict[Any, List[Dict[str, Any]]] = {}
    for c in captions or []:
        by_shot.setdefault(c.get("shot"), []).append(c)
        words = (c.get("text") or "").split()
        if len(words) > CAPTION_MAX_WORDS:
            errs.append(f"caption '{c.get('text')}' has {len(words)} words (max {CAPTION_MAX_WORDS})")
        try:
            if not 0 <= float(c.get("start", -1)) <= float(c.get("end", -1)) <= float(duration):
                errs.append(f"caption '{c.get('text')}' is outside the video")
        except (TypeError, ValueError):
            errs.append(f"caption '{c.get('text')}' needs numeric start/end")
        key = c.get("key") or ""
        if key and key.lower() not in (c.get("text") or "").lower():
            errs.append(f"caption '{c.get('text')}': key word '{key}' is not in its text")
    for shot, group in by_shot.items():
        if len(group) > 1:
            for c in group:
                if len((c.get("text") or "").split()) < CAPTION_MIN_WORDS:
                    errs.append(f"caption '{c.get('text')}' (shot {shot}) has under "
                                f"{CAPTION_MIN_WORDS} words in a multi-chunk sentence")
    return errs


def ass_timestamp(seconds: float) -> str:
    """H:MM:SS.cc for an .ass Dialogue line."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_text(caption: Dict[str, Any]) -> str:
    """Caption text with the key word wrapped in a yellow colour override."""
    text = caption.get("text") or ""
    key = caption.get("key") or ""
    if not key:
        return text
    idx = text.lower().find(key.lower())
    if idx < 0:
        return text
    word = text[idx:idx + len(key)]
    return text[:idx] + "{\\c" + KEY_COLOUR + "}" + word + "{\\c" + TEXT_COLOUR + "}" + text[idx + len(key):]


def write_ass(captions: List[Dict[str, Any]], path: Path, fmt=None) -> Path:
    """Write captions as an .ass subtitle file. Returns the path."""
    height = int(getattr(fmt, "height", 1080) or 1080)
    width = int(getattr(fmt, "width", 1920) or 1920)
    scale = height / 1080.0
    font_size = max(12, int(round(CAPTION_FONT_SIZE * scale)))
    margin_v = max(16, int(round(CAPTION_MARGIN_V * scale)))
    lines = [
        "[Script Info]",
        "Title: Manhwa recap auto-captions",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,DejaVu Sans,{font_size},{TEXT_COLOUR},&H000000FF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,2,1,2,{int(60 * scale)},{int(60 * scale)},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for c in captions or []:
        lines.append(f"Dialogue: 0,{ass_timestamp(c['start'])},{ass_timestamp(c['end'])},Default,,0,0,0,,{_ass_text(c)}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(path)


def captions_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.captions.ass"


def write_captions_file(root: Path, chapter: str, plan: Dict[str, Any] | None = None) -> Path:
    """Write video/chapter_N.captions.ass from a plan (loaded if not given).

    Uses the plan's stored captions when present, else builds them from its
    shots, so `video captions` works on old plans too.
    """
    from .plan import plan_path

    if plan is None:
        plan = json.loads(plan_path(root, chapter).read_text(encoding="utf-8"))
    captions = plan.get("captions") or build_captions(plan.get("shots") or [])
    return write_ass(captions, captions_path(root, chapter))
