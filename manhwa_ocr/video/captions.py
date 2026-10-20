"""Auto-captions: CapCut-style burned-in subtitles for chapter videos.

One caption at a time, 3-5 words, key word yellow, black outline -- the
same look the intro video's burned-in subtitles used, generalised to
chapter videos. Captions are FULL text (every word of the narration),
unlike callouts, which stay keyword-only: the two never double up,
because a callout is one keyword on an impact/reveal peak and a caption
is the sentence itself.

Timing: captions are built from the ALIGNMENT segments (one sentence =
one [start, end] window from whisper / the alignment engine), never
from per-shot estimates. A shot is not a sentence: coverage fillers and
pair splits cut shots inside a sentence's spoken window, so captions
built per shot stopped during filler shots (3-5s gaps with no caption
while the narration continued) and compressed a sentence's tail into
the shot before the filler (a caption up to 5s BEFORE its words were
spoken). Sentence-aligned captions have neither gap: every chunk falls
inside its own sentence's aligned window, and the chunks cover the
whole narration span with no gap over 1s.

Layout: captions render in a DEDICATED bottom bar OUTSIDE the card art
(frames.py draws the black bar; cards are capped to the top 92% of the
frame and centred there). The .ass MarginV puts the text inside that
bar, so a caption can never cover a panel's own bottom text.

Flow: plan.py builds plan["captions"] from the alignment sentences
(captions on by default for NEW builds, video.captions: true).
render.py writes video/chapter_N.captions.ass from them and burns it in
with ffmpeg's subtitles filter in a final pass -- captions never touch
the per-shot segment cache, so rewording captions never re-renders a
shot. Old plans have no "captions" key and render without captions
unless the user runs `video captions --chapter N`, which adds them to
the plan + writes the .ass. The .ass is also a standalone artefact: any
player that loads external subtitles shows the same captions.
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
CAPTION_MARGIN_V = 16      # text sits inside the dedicated bottom bar (px at 1080p, scaled by frame height)
CAPTION_FONT_SIZE = 46     # px at 1080p (scaled by frame height)
KEY_COLOUR = "&H00FFFF&"   # ASS BGR: yellow
TEXT_COLOUR = "&HFFFFFF&"  # ASS BGR: white

# The dedicated caption bar: the bottom 8% of the frame is a black bar
# drawn by frames.py (ShotRenderer) under every captioned shot. Cards are
# capped to the top 92% (see frames.card_box / slot_centres), so no caption
# ever overlaps panel art.
CAPTION_BAR_FRACTION = 0.08
CAPTION_BAR_TOP_FRACTION = 1.0 - CAPTION_BAR_FRACTION  # 0.92: cards end at or above this
CAPTION_EARLY_TOLERANCE = 0.5   # a caption may not appear this far before its words
CAPTION_MAX_GAP = 1.0           # no gap over this long without a caption during narration

_WORD = re.compile(r"[A-Za-z0-9']+")


def caption_bar_geometry(fmt) -> Dict[str, float]:
    """The dedicated caption bar for a frame format: {top, height} in px.

    Cards end at or above `top`; caption text sits inside [top, height].
    """
    height = float(getattr(fmt, "height", 1080) or 1080)
    top = height * CAPTION_BAR_TOP_FRACTION
    return {"top": top, "height": height - top}


def caption_text_bbox(fmt) -> tuple:
    """Approximate (x0, y0, x1, y1) px box of one caption line in `fmt`.

    The .ass style is bottom-centre (Alignment 2) with MarginV from the
    frame's bottom edge; one line is ~1.25x the font size tall. Used by
    tests / QA to prove the caption sits inside the bar, below the cards.
    """
    height = float(getattr(fmt, "height", 1080) or 1080)
    width = float(getattr(fmt, "width", 1920) or 1920)
    scale = height / 1080.0
    margin_v = max(16, int(round(CAPTION_MARGIN_V * scale)))
    line_h = CAPTION_FONT_SIZE * scale * 1.25
    return (0.0, height - margin_v - line_h, width, height - margin_v)


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


def build_captions_from_sentences(sentences: List[Dict[str, Any]],
                                  duration: float | None = None) -> List[Dict[str, Any]]:
    """Captions aligned to sentence windows: {text, key, start, end, shot, sentence}.

    `sentences`: alignment segments (or cues sentences) with "text",
    "start" and "end". A sentence with a missing "end" ends where the
    next sentence starts (alignment segments are contiguous: each
    sentence's end is the next one's start); a sentence with no usable
    start/end is skipped. Chunks spread evenly across the sentence's
    own window, so no chunk appears before its words are spoken and no
    filler shot inside the window leaves a caption gap. `shot` carries
    the sentence index ("sent12") so a chunk is traceable to its
    sentence; `sentence` carries the 1-based sentence number.
    """
    out: List[Dict[str, Any]] = []
    usable: List[Dict[str, Any]] = []
    for i, s in enumerate(sentences or []):
        if not isinstance(s, dict):
            continue
        text = (s.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(s["start"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            end = float(s["end"])
        except (KeyError, TypeError, ValueError):
            end = None
        usable.append({"text": text, "start": start, "end": end, "index": i})
    for k, s in enumerate(usable):
        end = s["end"]
        if end is None:
            end = usable[k + 1]["start"] if k + 1 < len(usable) else duration
        if end is None or not end > s["start"]:
            continue
        chunks = split_sentence_to_chunks(s["text"])
        if not chunks:
            continue
        step = (end - s["start"]) / len(chunks)
        for j, chunk in enumerate(chunks):
            out.append({"text": chunk, "key": _key_word(chunk),
                        "start": round(s["start"] + j * step, 3),
                        "end": round(s["start"] + (j + 1) * step, 3),
                        "shot": f"sent{s['index'] + 1}",
                        "sentence": s["index"] + 1})
    return out


def build_captions(shots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Captions for a plan's shots: {text, key, start, end, shot}.

    LEGACY fallback for old plans / callers without sentence timings
    (`video captions` on a plan with no alignment file). New builds use
    build_captions_from_sentences() instead: a shot is not a sentence,
    and per-shot captions leave gaps during filler shots and show a
    sentence's tail before its words are spoken.
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


def validate_caption_timing(captions: List[Dict[str, Any]], sentences: List[Dict[str, Any]],
                            duration: float | None = None) -> List[str]:
    """Timing problems: a chunk outside its sentence's aligned window, or a
    narration gap over CAPTION_MAX_GAP with no caption.

    `sentences`: alignment segments with "text"/"start"/"end". Chunks are
    matched to sentences by their "sentence" number (set by
    build_captions_from_sentences); a chunk without one is matched by
    overlap: the sentence whose window contains most of the chunk. A
    chunk may start at most CAPTION_EARLY_TOLERANCE before its sentence
    starts and end at most that far after it ends.
    """
    errs: List[str] = []
    windows: List[tuple] = []
    for i, s in enumerate(sentences or []):
        if not isinstance(s, dict):
            continue
        try:
            windows.append((i + 1, float(s["start"]), float(s["end"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not windows:
        return errs
    by_num = {n: (a, b) for n, a, b in windows}
    for c in captions or []:
        try:
            cs, ce = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            errs.append(f"caption '{c.get('text')}' needs numeric start/end")
            continue
        num = c.get("sentence")
        if num not in by_num:
            # Match by WORD overlap first (a chunk's words belong to its
            # sentence even when the chunk's time window overlaps a
            # neighbour's -- a per-shot chunk can sit mostly inside the
            # previous sentence's window while carrying the next
            # sentence's words), then by time overlap as a tie-break.
            chunk_words = {w.strip(".,!?;:'\"").lower() for w in (c.get("text") or "").split()}
            best, best_key = None, None
            for n, (a, b) in by_num.items():
                sent_words = {w.strip(".,!?;:'\"").lower()
                              for w in (sentences[n - 1].get("text") or "").split()}
                word_ov = len(chunk_words & sent_words)
                time_ov = max(0.0, min(ce, b) - max(cs, a))
                key = (word_ov, time_ov)
                if best_key is None or key > best_key:
                    best, best_key = n, key
            num = best
        if num is None:
            continue
        a, b = by_num[num]
        if cs < a - CAPTION_EARLY_TOLERANCE:
            errs.append(f"caption '{c.get('text')}' starts at {cs:.2f}s, "
                        f"{a - cs:.2f}s BEFORE its sentence's words (sentence {num} starts {a:.2f}s)")
        if ce > b + CAPTION_EARLY_TOLERANCE:
            errs.append(f"caption '{c.get('text')}' ends at {ce:.2f}s, "
                        f"{ce - b:.2f}s AFTER its sentence's words (sentence {num} ends {b:.2f}s)")
    ordered = sorted((float(c["start"]), float(c["end"])) for c in captions or []
                     if isinstance(c, dict) and c.get("start") is not None and c.get("end") is not None)
    span_start = min(a for _, a, _ in windows)
    span_end = max(b for _, _, b in windows)
    prev_end = span_start
    for cs, ce in ordered:
        if span_start <= cs and cs - prev_end > CAPTION_MAX_GAP + 1e-9:
            errs.append(f"no caption for {cs - prev_end:.2f}s during narration ({prev_end:.2f}-{cs:.2f}s)")
        prev_end = max(prev_end, ce)
    if span_end - prev_end > CAPTION_MAX_GAP + 1e-9:
        errs.append(f"no caption for {span_end - prev_end:.2f}s during narration "
                    f"({prev_end:.2f}-{span_end:.2f}s)")
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


def write_ass(captions: List[Dict[str, Any]], path: Path, fmt=None,
              margin_v: int | None = None) -> Path:
    """Write captions as an .ass subtitle file. Returns the path.

    Alignment 2 (bottom centre) with MarginV inside the dedicated bottom
    bar (see CAPTION_BAR_FRACTION): the text sits on the black bar below
    the cards, never over panel art. `margin_v` overrides that (px at
    1080p) -- wide mode (no bar) needs the captions higher off the edge.
    """
    height = int(getattr(fmt, "height", 1080) or 1080)
    width = int(getattr(fmt, "width", 1920) or 1920)
    scale = height / 1080.0
    font_size = max(12, int(round(CAPTION_FONT_SIZE * scale)))
    if margin_v is None:
        margin_v = CAPTION_MARGIN_V
    margin_v = max(16, int(round(margin_v * scale)))
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
