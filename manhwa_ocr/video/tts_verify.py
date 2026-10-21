"""Catch a bad Deepgram synthesis automatically, without a human listening.

``video tts`` can produce audio that *sounds* fine to no one checking it: a
mispronounced invented character name, a truncated sentence from a dropped
API response, a rushed read. This module re-transcribes the synthesized
narration with the same whisper.cpp engine ``video align`` already uses, then
diffs what whisper actually heard against each sentence's expected words in
its expected time window (from ``chapter_N.tts.json``). A sentence whisper
heard very differently from what was asked for is exactly the sentence a
human would have caught by listening -- this catches most of that class
without needing one.

This is a heuristic safety net, not a gate as strict as ``fixes.json``
validation: whisper itself mishears real speech sometimes, so a flagged
sentence is a "go listen to this one" signal, not automatic proof of a
broken synthesis. Report every flag; never silently drop one.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Callable

from ..util import UserError, atomic_write_text, read_agent_json
from . import sync as sync_mod
from .media import audio_dir
from .tts import load_sentences

_WORD = re.compile(r"[a-z0-9']+")

DEFAULT_WHISPER_MODEL = "small.en"
# A sentence's heard-vs-expected word overlap below this ratio is flagged.
DEFAULT_MATCH_THRESHOLD = 0.55
# Seconds-per-word outside this range flags a duration anomaly (truncation
# reads as suspiciously fast overall; a corrupt/looped clip reads as slow).
MIN_SECONDS_PER_WORD = 0.12
MAX_SECONDS_PER_WORD = 0.60
WINDOW_PAD = 0.20


def _normalize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _words_in_window(words: list[dict], start: float, end: float) -> list[str]:
    return [w["w"] for w in words if start <= (w["start"] + w["end"]) / 2 <= end]


def _manifest(root: Path, chapter: str) -> dict:
    path = audio_dir(root) / f"chapter_{chapter}.tts.json"
    if not path.exists():
        raise UserError(f"No {path.name}. Run `video tts --chapter {chapter}` first.")
    return read_agent_json(path, f"Re-run `video tts --chapter {chapter}` to regenerate it.")


def verify_chapter_audio(root: Path, chapter: str, *, log: Callable[[str], None] = print,
                         whisper_model: str = DEFAULT_WHISPER_MODEL,
                         match_threshold: float = DEFAULT_MATCH_THRESHOLD,
                         transcribe: Callable[..., list[dict]] | None = None) -> dict[str, Any]:
    """Re-transcribe the synthesized audio and flag sentences that don't match. Writes a report."""
    manifest = _manifest(root, chapter)
    sentences = load_sentences(root, chapter)
    audio_path = audio_dir(root) / f"chapter_{chapter}.m4a"
    if not audio_path.exists():
        raise UserError(f"No {audio_path.name}. Run `video tts --chapter {chapter}` first.")

    transcribe = transcribe or sync_mod.transcribe
    if transcribe is sync_mod.transcribe and not sync_mod.available():
        raise UserError(f"Audio verification needs pywhispercpp. Install it with:  {sync_mod.SYNC_HINT}")

    cache_dir = Path(root) / ".work" / "tts" / f"ch{chapter}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    log(f"  verifying tts audio: transcribing {audio_path.name} with whisper '{whisper_model}'...")
    heard_words = transcribe(audio_path, model=whisper_model,
                             cache=cache_dir / "verify_timeline.json", log=log)

    flags: list[dict[str, Any]] = []
    for entry, text in zip(manifest.get("sentences", []), sentences):
        n = entry["n"]
        start, seconds = float(entry["start"]), float(entry["seconds"])
        expected = _normalize(text)
        heard = _normalize(" ".join(_words_in_window(heard_words, start - WINDOW_PAD,
                                                       start + seconds + WINDOW_PAD)))
        ratio = difflib.SequenceMatcher(a=expected, b=heard).ratio() if expected else 1.0
        issues = []
        if ratio < match_threshold:
            issues.append(f"heard words only {ratio:.0%} match the expected sentence")
        word_count = max(1, len(expected))
        spw = seconds / word_count
        if spw < MIN_SECONDS_PER_WORD:
            issues.append(f"{spw:.2f}s/word is unusually fast -- possible truncation")
        elif spw > MAX_SECONDS_PER_WORD:
            issues.append(f"{spw:.2f}s/word is unusually slow -- possible corrupt/looped audio")
        if issues:
            flags.append({"n": n, "expected": text, "heard": " ".join(heard), "ratio": round(ratio, 3),
                          "seconds_per_word": round(spw, 3), "issues": issues})

    result = {
        "chapter": str(chapter), "whisper_model": whisper_model,
        "sentences_checked": len(sentences), "flagged": flags, "ok": not flags,
    }
    atomic_write_text(audio_dir(root) / f"chapter_{chapter}.tts_verify.json",
                      json.dumps(result, indent=1, ensure_ascii=False))
    if flags:
        log(f"  tts-verify: {len(flags)}/{len(sentences)} sentence(s) flagged for a listen -- "
            f"see chapter_{chapter}.tts_verify.json")
        for f in flags:
            log(f"    sentence {f['n']}: {'; '.join(f['issues'])}")
    else:
        log(f"  tts-verify: all {len(sentences)} sentences passed.")
    return result
