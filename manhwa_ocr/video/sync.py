"""Find when each script sentence is spoken in the recorded narration.

faster-whisper gives word-level timestamps for the audio. We already know the
exact script, so whisper only has to get the *timing* right, not every word:
both word sequences are lined up with difflib, and each sentence's start is
taken from its first matched word. Misheard names, ad-libs and skipped words
just leave gaps, which are filled by interpolating between neighbours.

Transcription is the slow part, so it is cached in
video/chapter_N.timeline.json keyed by the audio file's hash and the model.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List

from ..util import UserError, atomic_write_text, require_chapter
from .media import video_dir

SYNC_HINT = 'pip install -e ".[sync]"'
_SMALL = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
          "fifteen sixteen seventeen eighteen nineteen").split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()


def timeline_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.timeline.json"


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _number_words(n: int) -> List[str]:
    if n < 20:
        return [_SMALL[n]]
    if n < 100:
        tens, ones = divmod(n, 10)
        return [_TENS[tens - 2]] + ([_SMALL[ones]] if ones else [])
    return [str(n)]


def tokens(text: str) -> List[str]:
    """Comparable words: casefolded, punctuation gone, small numbers spelled out."""
    out: List[str] = []
    for raw in re.split(r"\s+", text or ""):
        w = "".join(ch for ch in unicodedata.normalize("NFKC", raw).casefold()
                    if unicodedata.category(ch)[0] in "LMN")
        if not w:
            continue
        if w.isdigit() and len(w) <= 2:
            out.extend(_number_words(int(w)))
        else:
            out.append(w)
    return out


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:24]


def transcribe(audio: Path, *, model: str = "small.en", language: str | None = None,
               initial_prompt: str | None = None, cache: Path | None = None,
               log=print) -> List[Dict[str, Any]]:
    """[{"w": word, "start": s, "end": s}, ...] for the whole recording."""
    key = {"audio": _file_hash(audio), "model": model, "language": language}
    if cache and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("key") == key and data.get("words"):
                return data["words"]
        except (OSError, ValueError):
            pass
    if not available():
        raise UserError(f"Syncing to the audio needs faster-whisper. Install it with:  {SYNC_HINT}")
    from faster_whisper import WhisperModel

    log(f"  transcribing {audio.name} with whisper '{model}' (first run downloads the model)...")
    try:
        wm = WhisperModel(model, device="cpu", compute_type="int8")
        segments, _info = wm.transcribe(str(audio), word_timestamps=True, vad_filter=True,
                                        language=language, initial_prompt=initial_prompt,
                                        condition_on_previous_text=False)
        words = [{"w": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)}
                 for seg in segments for w in (seg.words or []) if w.word.strip()]
    except Exception as exc:  # noqa: BLE001 - model download/decoding errors
        raise UserError(f"Whisper couldn't transcribe {audio.name}: {exc}") from None
    if not words:
        raise UserError(f"Whisper heard no speech in {audio.name}.")
    if cache:
        atomic_write_text(cache, json.dumps({"key": key, "words": words}, ensure_ascii=False))
    return words


def align(sentences: List[Dict[str, Any]], words: List[Dict[str, Any]], duration: float) -> Dict[str, Any]:
    """Give every sentence a start time, and every emphasised word a time.

    Returns {"sentences": [... with "start" and "emphasis" times ...],
             "matched": fraction of sentences anchored to real word timings,
             "unmatched": [texts that had to be interpolated]}.
    """
    script: List[str] = []
    owner: List[int] = []
    offsets: List[int] = []
    for i, s in enumerate(sentences):
        offsets.append(len(script))
        toks = tokens(s.get("text", ""))
        script.extend(toks)
        owner.extend([i] * len(toks))
    heard = []
    for w in words:
        for t in tokens(w["w"]):
            heard.append((t, w["start"], w["end"]))

    to_time: Dict[int, float] = {}
    if script and heard:
        sm = difflib.SequenceMatcher(None, script, [h[0] for h in heard], autojunk=False)
        for a, b, size in sm.get_matching_blocks():
            for k in range(size):
                to_time[a + k] = heard[b + k][1]

    starts: List[float | None] = []
    for i, s in enumerate(sentences):
        lo = offsets[i]
        hi = offsets[i + 1] if i + 1 < len(sentences) else len(script)
        anchor = next((j for j in range(lo, hi) if j in to_time), None)
        if anchor is None:
            starts.append(None)
            continue
        # The first word may have been misheard: back off from the first
        # matched word at a typical speaking rate (~0.33s per word).
        starts.append(max(0.0, to_time[anchor] - 0.33 * (anchor - lo)))

    # Fill gaps: a sentence whisper didn't anchor is placed between the nearest
    # anchored neighbours in proportion to how much text precedes it.
    known = [i for i, t in enumerate(starts) if t is not None]
    unmatched = [sentences[i].get("text", "") for i, t in enumerate(starts) if t is None]
    n = len(sentences)
    cum = [0]
    for s in sentences:
        cum.append(cum[-1] + (len(s.get("text", "")) or 1))
    anchors = [(i, float(starts[i])) for i in known]
    if not anchors or anchors[0][0] != 0:
        anchors.insert(0, (0, 0.0))          # the first sentence starts the audio
    anchors.append((n, duration))            # virtual anchor at the very end
    for (ia, ta), (ib, tb) in zip(anchors, anchors[1:]):
        span = cum[ib] - cum[ia] or 1
        for i in range(ia + 1, ib):
            if starts[i] is None:
                starts[i] = ta + (tb - ta) * (cum[i] - cum[ia]) / span
    if starts and starts[0] is None:
        starts[0] = 0.0

    # Starts must never go backwards, or shots would have negative length.
    out, last = [], 0.0
    for i, s in enumerate(sentences):
        t = max(last, float(starts[i] or 0.0))
        t = min(t, max(0.0, duration - 0.3))
        emphasis_times = []
        sent_tokens = script[offsets[i]:(offsets[i + 1] if i + 1 < len(sentences) else len(script))]
        for phrase in s.get("emphasis") or []:
            ptoks = tokens(phrase)
            if not ptoks:
                continue
            for k in range(len(sent_tokens) - len(ptoks) + 1):
                if sent_tokens[k:k + len(ptoks)] == ptoks and (offsets[i] + k) in to_time:
                    emphasis_times.append(to_time[offsets[i] + k])
                    break
        out.append({**s, "start": round(t, 3), "emphasis": sorted(emphasis_times)})
        last = t

    matched = len(known) / len(sentences) if sentences else 0.0
    return {"sentences": out, "matched": round(matched, 3), "unmatched": unmatched}


def character_prompt(memory) -> str | None:
    """Whisper hint: the series' character names, so it spells them right."""
    names = []
    for c in list(getattr(memory, "characters", {}).values())[:40]:
        names.append(c.get("name", ""))
        names.extend(c.get("aliases") or [])
    names = [n for n in dict.fromkeys(names) if n]
    return ("Characters: " + ", ".join(names) + ".") if names else None
