"""Find when each script sentence is spoken in the recorded narration.

whisper.cpp (via pywhispercpp) gives word-level timestamps for the audio. We
already know the exact script, so whisper only has to get the *timing* right,
not every word: both word sequences are lined up with difflib, and each
sentence's start is taken from its first matched word. Misheard names, ad-libs
and skipped words just leave gaps, which are filled by interpolating between
neighbours.

whisper.cpp (ggml) rather than faster-whisper (ctranslate2/Intel MKL) is used
deliberately: MKL's allocator can fail with "mkl_malloc: failed to allocate
memory" under the memory pressure of a normal multi-app desktop, even though
the model itself only needs ~150-300MB. ggml doesn't have that failure mode.

Transcription is the slow part, so it is cached in
video/chapter_N.timeline.json keyed by the audio file's hash and the model.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Dict, List

from ..util import UserError, atomic_write_text, require_chapter
from .ffmpeg import ffmpeg_exe
from .media import video_dir

SYNC_HINT = 'pip install -e ".[sync]"'
_SMALL = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
          "fifteen sixteen seventeen eighteen nineteen").split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()


def timeline_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.timeline.json"


def available() -> bool:
    try:
        import pywhispercpp.model  # noqa: F401
        return True
    except ImportError:
        return False


_SCALES = ((10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand"))
_ORDINAL = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
            "nine": "ninth", "twelve": "twelfth"}


def _number_words(n: int) -> List[str]:
    """Any non-negative integer as spoken words: 99 -> ninety nine, 100 -> one hundred."""
    if n < 20:
        return [_SMALL[n]]
    if n < 100:
        tens, ones = divmod(n, 10)
        return [_TENS[tens - 2]] + ([_SMALL[ones]] if ones else [])
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        out = [_SMALL[hundreds], "hundred"]
        if rest:
            out.extend(_number_words(rest))
        return out
    for size, name in _SCALES:
        if n >= size:
            head, rest = divmod(n, size)
            out = _number_words(head) + [name]
            if rest:
                out.extend(_number_words(rest))
            return out
    return [str(n)]  # pragma: no cover - unreachable


def spell_number(n: int) -> List[str]:
    """Like _number_words, but four-digit years (1100-1999) read as a narrator says
    them: 1999 -> nineteen ninety nine."""
    if 1100 <= n <= 1999 and n % 100:
        return _number_words(n // 100) + _number_words(n % 100)
    return _number_words(n)


def _ordinal_words(n: int) -> List[str]:
    words = spell_number(n)
    last = words[-1]
    if last in _ORDINAL:
        words[-1] = _ORDINAL[last]
    elif last.endswith("y"):
        words[-1] = last[:-1] + "ieth"
    else:
        words[-1] = last + "th"
    return words


_ORD_RE = re.compile(r"^(\d+)(?:st|nd|rd|th)$")


def tokens(text: str) -> List[str]:
    """Comparable words: casefolded, punctuation gone, every integer spelled out.

    Hyphenated spoken numbers ("ninety-nine") split into separate words so the
    script's "99" and whisper's "ninety-nine" or "99" all compare equal.
    """
    out: List[str] = []
    for chunk in re.split(r"\s+", text or ""):
        for raw in re.split(r"[-‐-―]", chunk):
            w = "".join(ch for ch in unicodedata.normalize("NFKC", raw).casefold()
                        if unicodedata.category(ch)[0] in "LMN")
            if not w:
                continue
            m = _ORD_RE.match(w)
            if w.isdigit() and len(w) <= 15:
                out.extend(spell_number(int(w)))
            elif m and len(m.group(1)) <= 15:
                out.extend(_ordinal_words(int(m.group(1))))
            else:
                out.append(w)
    return out


def file_hash(path: Path) -> str:
    """The audio-file hash the timeline cache is keyed on (first 24 hex chars)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:24]


def _mono16k(path: Path) -> "Any":
    """Decode to mono 16kHz float32 PCM ourselves, so whisper.cpp never needs
    ffmpeg on PATH (we already carry one via imageio-ffmpeg)."""
    import numpy as np

    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path),
          "-f", "f32le", "-ac", "1", "-ar", "16000", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise UserError(f"Couldn't decode {path.name} for whisper:\n"
                        f"{(proc.stderr or b'').decode('utf-8', 'replace')[-800:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def transcribe(audio: Path, *, model: str = "small.en", language: str | None = None,
               initial_prompt: str | None = None, cache: Path | None = None,
               log=print) -> List[Dict[str, Any]]:
    """[{"w": word, "start": s, "end": s}, ...] for the whole recording."""
    key = {"audio": file_hash(audio), "model": model, "language": language}
    if cache and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("key") == key and data.get("words"):
                return data["words"]
        except (OSError, ValueError):
            pass
    if not available():
        raise UserError(f"Syncing to the audio needs pywhispercpp. Install it with:  {SYNC_HINT}")
    from pywhispercpp.model import Model

    log(f"  transcribing {audio.name} with whisper '{model}' (first run downloads the model)...")
    try:
        pcm = _mono16k(audio)
        wm = Model(model, print_progress=False, print_realtime=False, redirect_whispercpp_logs_to=None)
        # English-only (".en") models can't produce other languages; skip the
        # detection pass instead of leaving it to guess (it can pick nonsense
        # like Chinese at near-zero confidence and is simply wasted work).
        lang = language or ("en" if model.endswith(".en") else "")
        segments = wm.transcribe(pcm, token_timestamps=True, split_on_word=True, max_len=1,
                                 language=lang, initial_prompt=initial_prompt or "")
        words = [{"w": seg.text.strip(), "start": round(seg.t0 / 100, 3), "end": round(seg.t1 / 100, 3)}
                 for seg in segments if seg.text.strip()]
    except UserError:
        raise
    except Exception as exc:  # noqa: BLE001 - model download/decoding errors
        raise UserError(f"Whisper couldn't transcribe {audio.name}: {exc}") from None
    if not words:
        raise UserError(f"Whisper heard no speech in {audio.name}.")
    if cache:
        atomic_write_text(cache, json.dumps({"key": key, "words": words}, ensure_ascii=False))
    return words


MIN_SEGMENT = 0.25   # seconds; video.sync.min_segment overrides it

# Every sync/hold knob this package reads, where it lives and what it means.
# The single place defaults are written down; config.py's DEFAULT_CONFIG ships
# the same values under `video:` / `video.sync:`. A key missing from the
# config is warned about loudly (effective_settings) - never silently replaced.
SYNC_DEFAULTS: Dict[str, float] = {
    "min_hold": 3.0,          # video.min_hold: no picture stays up for less
    "min_segment": 0.25,      # video.sync.min_segment: shortest sentence segment
    "max_late": 0.3,          # video.sync.max_late: a cut may start at most this much after its sentence
    "lead": 0.2,              # video.sync.lead: hard early bound - a cut may lead its sentence by at most this
    "min_hold_short": 2.0,    # video.sync.min_hold_short: shortest hold allowed when merging is impossible
    "max_hold": 8.0,          # video.sync.max_hold: a picture held longer than this gets split
    "max_unmatched": 6.0,     # video.sync.max_unmatched: a run of unmatched (interpolated) speech longer than this stops the build
    "override_evidence": 20,  # video.sync.override_evidence: OCR-overlap score above which a tag/OCR disagreement is flagged (tag still shows)
    "filler_full": 6.0,       # video.sync.filler_full: only sentences this long may be filled end to end
    "gate_wrong": 0.05,       # video.sync.gate_wrong: most wrong-picture time allowed overall (provisional until calibration)
    "gate_window": 0.25,      # video.sync.gate_window: most wrong-picture time allowed in any 5s window (provisional)
    "gate_grace": 0.2,        # video.sync.gate_grace: extra lateness tolerated before the gate fails
}
_SETTING_PATHS = {k: (f"video.{k}" if k == "min_hold" else f"video.sync.{k}") for k in SYNC_DEFAULTS}


def effective_settings(vcfg: Dict[str, Any], log=print) -> Dict[str, float]:
    """The sync/hold settings actually in force, read only from their documented
    spots (`video.min_hold`, `video.sync.*`). Missing keys use the default but
    are warned about by name, so a half-set config never changes timing quietly."""
    sync_cfg = vcfg.get("sync") if isinstance(vcfg.get("sync"), dict) else {}
    out: Dict[str, float] = {}
    missing: List[str] = []
    for key, default in SYNC_DEFAULTS.items():
        if key == "min_hold":
            value = vcfg.get("min_hold")
        else:
            value = sync_cfg.get(key)
        if value is None:
            missing.append(key)
            out[key] = float(default)
        else:
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                raise UserError(f"{_SETTING_PATHS[key]} must be a number, not {value!r}.") from None
    if missing:
        log("  ! manhwa-ocr.yaml is missing sync/hold settings; using defaults: "
            + ", ".join(f"{_SETTING_PATHS[k]}={out[k]:g}" for k in missing))
        log("    Add them under `video:` (see manhwa-ocr.yaml) so plans match what you expect.")
    return out


def _spread_collisions(raw: List[float], weights: List[int], duration: float) -> tuple[List[float], int]:
    """Sentences whose raw start is not later than an earlier sentence's start
    form a cluster with that earlier one. Give each cluster member a share of
    the window [cluster start, next cluster start] in proportion to its words."""
    n = len(raw)
    out = list(raw)
    leaders: List[int] = []
    run = float("-inf")
    for i in range(n):
        if i == 0 or raw[i] > run + 1e-3:
            leaders.append(i)
            run = raw[i]
    spread = 0
    for k, lead in enumerate(leaders):
        stop = leaders[k + 1] if k + 1 < len(leaders) else n
        if stop - lead < 2:
            continue
        begin = raw[lead]
        end = raw[stop] if stop < n else duration
        end = max(end, begin)
        total = float(sum(weights[lead:stop]))
        acc = 0.0
        for i in range(lead, stop):
            out[i] = begin + (end - begin) * acc / total
            acc += weights[i]
        spread += stop - lead
    return out, spread


def _enforce_min_segment(starts: List[float], duration: float, min_segment: float) -> List[float]:
    """Make every gap between consecutive starts, and the last sentence's tail,
    at least `min_segment` without breaking order or passing the audio end.

    A backward pass first (pull starts earlier: a panel showing a moment early is
    harmless, showing late is not), then a forward pass for what is left at the
    front. Both together are always feasible when n * min_segment <= duration."""
    n = len(starts)
    if n == 0:
        return []
    duration = max(0.0, float(duration))
    m = max(0.0, float(min_segment))
    if duration > 0:
        m = min(m, 0.999 * duration / n)
    t = [max(0.0, x) for x in starts]
    for i in range(1, n):                 # never backwards
        t[i] = max(t[i], t[i - 1])
    t[-1] = min(t[-1], max(0.0, duration - m))
    for i in range(n - 2, -1, -1):        # backward: leave room for the next one
        t[i] = min(t[i], t[i + 1] - m)
    t[0] = max(t[0], 0.0)
    for i in range(1, n):                 # forward: repair anything pushed below zero
        t[i] = max(t[i], t[i - 1] + m)
    return t


def _script_align(sentences: List[Dict[str, Any]], words: List[Dict[str, Any]]):
    """Both sequences tokenised and lined up: (script tokens, per-sentence
    offsets into them, heard words, {script index -> whisper start time})."""
    script: List[str] = []
    offsets: List[int] = []
    for s in sentences:
        offsets.append(len(script))
        script.extend(tokens(s.get("text", "")))
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
    return script, offsets, heard, to_time


def _anchors(script: List[str], offsets: List[int], to_time: Dict[int, float],
             sentences: List[Dict[str, Any]]) -> List[Dict[str, float] | None]:
    out: List[Dict[str, float] | None] = []
    for i in range(len(sentences)):
        lo = offsets[i]
        hi = offsets[i + 1] if i + 1 < len(sentences) else len(script)
        anchor = next((j for j in range(lo, hi) if j in to_time), None)
        if anchor is None:
            out.append(None)
        else:
            # The first word may have been misheard: back off from the first
            # matched word at a typical speaking rate (~0.33s per word).
            out.append({"time": float(to_time[anchor]),
                        "raw_start": max(0.0, to_time[anchor] - 0.33 * (anchor - lo))})
    return out


def sentence_anchors(sentences: List[Dict[str, Any]], words: List[Dict[str, Any]]) -> List[Dict[str, float] | None]:
    """First matched whisper word per sentence: [{"time", "raw_start"} | None].

    None means the sentence had no matched word at all (it gets interpolated).
    `raw_start` is align()'s own start for it: the matched word's time with the
    speaking-rate back-off already applied. `video sync-audit` reads these back
    to tell real anchors from interpolated guesses and to measure residuals.
    """
    script, offsets, _heard, to_time = _script_align(sentences, words)
    return _anchors(script, offsets, to_time, sentences)


def align(sentences: List[Dict[str, Any]], words: List[Dict[str, Any]], duration: float,
          min_segment: float = MIN_SEGMENT) -> Dict[str, Any]:
    """Give every sentence a start time, and every emphasised word a time.

    Returns {"sentences": [... with "start", "emphasis" and "timing_source" ...
                           "timing_source" is "matched" (anchored to a heard
                           word), "interpolated" (filled between anchors) or
                           "estimated" (no words at all) ...],
             "matched": fraction of sentences anchored to real word timings,
             "unmatched": [texts that had to be interpolated],
             "unmatched_runs": consecutive interpolated sentences with their
                               span, so a long guess is visible (and can stop
                               the build), never silent,
             "spread": sentences whose start collided with a neighbour and were
                       redistributed by word count}.

    Every sentence ends up at least `min_segment` seconds long (as far as the
    audio length allows), so no segment is ever zero-length.
    """
    script, offsets, heard, to_time = _script_align(sentences, words)
    starts: List[float | None] = [None if a is None else float(a["raw_start"])
                                  for a in _anchors(script, offsets, to_time, sentences)]

    # Fill gaps: a sentence whisper didn't anchor is placed between the nearest
    # anchored neighbours in proportion to how much text precedes it.
    known = [i for i, t in enumerate(starts) if t is not None]
    known_set = set(known)
    unmatched = [sentences[i].get("text", "") for i, t in enumerate(starts) if t is None]
    n = len(sentences)
    cum = [0]
    for s in sentences:
        cum.append(cum[-1] + (len(s.get("text", "")) or 1))
    anchors = [(i, float(starts[i])) for i in known]
    speech_start = float(heard[0][1]) if heard else 0.0
    if not anchors or anchors[0][0] != 0:
        # Video shot 1 still starts at 0 in plan.py, but speech timing must
        # keep the first heard word's time so leading silence doesn't pull
        # every interpolated sentence early.
        anchors.insert(0, (0, speech_start))
    anchors.append((n, duration))            # virtual anchor at the very end
    for (ia, ta), (ib, tb) in zip(anchors, anchors[1:]):
        span = cum[ib] - cum[ia] or 1
        for i in range(ia + 1, ib):
            if starts[i] is None:
                starts[i] = ta + (tb - ta) * (cum[i] - cum[ia]) / span
    if starts and starts[0] is None:
        starts[0] = speech_start

    # Several short sentences can land on one anchor ("Number 99." then a run of
    # one-word names). Spread each such cluster across its window by word count
    # instead of collapsing them all onto the same instant.
    weights = [max(1, len(tokens(s.get("text", "")))) for s in sentences]
    times, spread = _spread_collisions([float(x or 0.0) for x in starts], weights, duration)
    times = _enforce_min_segment(times, duration, min_segment)

    # Consecutive interpolated sentences, with their span in the final timing:
    # a long one means that stretch of pictures sits on guesswork.
    runs: List[Dict[str, Any]] = []
    if heard and sentences:
        run_first = None
        for i in range(len(sentences) + 1):
            if i < len(sentences) and i not in known_set:
                if run_first is None:
                    run_first = i
            elif run_first is not None:
                last = i - 1
                end = times[i] if i < len(sentences) else duration
                runs.append({"first": run_first + 1, "last": last + 1,
                             "start": round(times[run_first], 3), "end": round(end, 3),
                             "seconds": round(end - times[run_first], 3)})
                run_first = None

    out = []
    for i, s in enumerate(sentences):
        t = times[i]
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
        source = "estimated" if not heard else ("matched" if i in known_set else "interpolated")
        out.append({**s, "start": round(t, 3), "emphasis": sorted(emphasis_times),
                    "timing_source": source})

    matched = len(known) / len(sentences) if sentences else 0.0
    return {"sentences": out, "matched": round(matched, 3), "unmatched": unmatched,
            "unmatched_runs": runs, "spread": spread}


def long_unmatched_run(aligned: Dict[str, Any], max_unmatched: float) -> Dict[str, Any] | None:
    """The longest run of consecutive interpolated sentences, if it is longer
    than `max_unmatched` seconds - the build stops on this, so a stretch of
    guessed timing can never pass silently. None when nothing is over."""
    runs = aligned.get("unmatched_runs") or []
    worst = max(runs, key=lambda r: r["seconds"], default=None)
    if worst and float(worst["seconds"]) > float(max_unmatched) + 1e-9:
        return worst
    return None


def character_prompt(memory) -> str | None:
    """Whisper hint: the series' character names, so it spells them right."""
    names = []
    for c in list(getattr(memory, "characters", {}).values())[:40]:
        names.append(c.get("name", ""))
        names.extend(c.get("aliases") or [])
    names = [n for n in dict.fromkeys(names) if n]
    return ("Characters: " + ", ".join(names) + ".") if names else None
