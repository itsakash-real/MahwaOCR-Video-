"""Build chapter_N.alignment.json: the one file that decides which panel shows
for every sentence, before anything is rendered.

The renderer must never decide this itself. Everything in this module is
deterministic - no CLIP, no per-panel LLM captioning, no judgment calls made
under time pressure across 40+ sentences in one sitting. The things that
actually decide a panel are, in order:

  1. classify.py - is this sentence even something a panel could show?
     INTERNAL/PURE_AUDIO sentences hold whatever panel is already up.
  2. the story's own order - a panel can only be chosen from a window ahead
     of wherever the story currently is (never a blind, unconstrained pick).
  3. the recap's original tag - trusted *if* it falls inside that window and
     nothing in the window has stronger lexical evidence against it.
  4. lexical overlap with each candidate panel's own OCR'd text - exact or
     near-exact word/number matches, not fuzzy semantic similarity (which
     testing showed is unreliable for this art style).

Whatever is left genuinely undetermined is flagged (`needs_review`), not
guessed at - see validate.py for the checks that turn those into the review
queue a human or a future, stronger per-chapter model can work through.

`align_sentences()` is the pure decision engine: given sentence texts/tags and
panel data only (no audio, no whisper, no files), it decides every panel.
`build_alignment()` is the I/O wrapper that gets real timestamps and writes
the file. Tests exercise `align_sentences()` directly, so they run instantly
and need no audio fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..util import UserError, atomic_write_text, require_chapter
from . import classify as classify_mod
from . import cues as cues_mod
from . import sync as sync_mod
from . import validate as validate_mod
from .media import audio_path, video_dir
from .panels import load_panels

WINDOW = 6          # how many panels ahead of the current story position are candidates
JUMP_TOLERANCE = 2  # a tag this far "behind" the running position isn't trusted blindly
MIN_WORD = 4        # a matched token shorter than this is too common to count as evidence

# Common words long enough to slip past MIN_WORD but still carry no distinguishing
# evidence ("the", "was" would already be excluded by length; these are the
# length>=4 stragglers). A real match should be a name, a number, a place, an
# item - something that wouldn't appear in most sentences.
STOPWORDS = {
    "this", "that", "these", "those", "then", "than", "when", "where", "what",
    "which", "while", "with", "from", "they", "their", "them", "were", "have",
    "has", "had", "about", "would", "could", "should", "will", "shall", "into",
    "onto", "over", "under", "after", "before", "because", "even", "just",
    "only", "also", "very", "more", "most", "some", "such", "each", "every",
    "both", "being", "been", "does", "did", "doing", "there", "here", "your",
    "his", "her", "him", "himself", "herself", "itself", "who", "whom", "whose",
    "again", "still", "once", "upon", "like", "much", "many", "though",
}


def alignment_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.alignment.json"


MIN_OVERRIDE_EVIDENCE = 10  # roughly two real content words, or one long distinctive one


def _lexical_overlap(text: str, panel_text: str) -> int:
    """How much real evidence the panel's own OCR'd text shares with the
    sentence: the summed character length of matched, meaningful tokens.

    Deliberately simple overlap on normalised tokens, not an embedding - this
    is meant to catch exact/near-exact evidence (a shared number, a shared
    name, a shared system-notification phrase, a near-quoted line), not
    "similar meaning". It's a sum, not a matched/total ratio: a panel with a
    lot of OCR'd dialogue must not score *lower* than a near-empty one just
    because one coincidental short word is a smaller fraction of its text -
    more real matched words (or longer, more distinctive ones) is always
    stronger evidence, never weaker.
    """
    if not panel_text:
        return 0
    p_tokens = {t for t in sync_mod.tokens(panel_text) if len(t) >= MIN_WORD and t not in STOPWORDS}
    if not p_tokens:
        return 0
    s_tokens = set(sync_mod.tokens(text))
    hit = p_tokens & s_tokens
    return sum(len(t) for t in hit)


def align_sentences(sentences: List[Dict[str, Any]], panels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The pure decision engine. `sentences`: [{"text": ..., "panels": [ids]}, ...]
    in story order. `panels`: the chapter's panel list, in story order, each
    with at least "id" and optionally "text_only"/"text". No timestamps, no
    I/O - every segment here still needs "start"/"end" added by the caller.
    """
    by_id = {p["id"]: p for p in panels}
    order = {p["id"]: i for i, p in enumerate(panels)}
    art_positions = [i for i, p in enumerate(panels) if not p.get("text_only")]

    segments: List[Dict[str, Any]] = []
    cursor = -1  # position of the last panel actually shown

    def window_from(pos: int) -> List[str]:
        # pos itself is included: a sentence may legitimately continue showing
        # the panel already on screen without an explicit {same} marker.
        ahead = [panels[i]["id"] for i in art_positions if pos <= i <= pos + WINDOW]
        if ahead:
            return ahead
        remaining = [panels[i]["id"] for i in art_positions if i >= pos]
        if remaining:
            return remaining[:WINDOW]
        return [p["id"] for p in panels if not p.get("text_only")][-1:]

    for n, s in enumerate(sentences, start=1):
        text = s.get("text", "")
        tagged = [p for p in (s.get("panels") or []) if p in by_id]
        ntype, reason = classify_mod.classify(text)
        seg: Dict[str, Any] = {
            "sentence": n, "text": text, "narration_type": ntype, "type_reason": reason,
            "tagged_panels": tagged,
        }

        if s.get("same") or ntype in {"INTERNAL", "PURE_AUDIO"}:
            if s.get("same"):
                ntype, reason = "SAME", "recap marked {same}: continues the previous panel"
                seg["narration_type"], seg["type_reason"] = ntype, reason
            seg.update(story_position=max(cursor, 0), candidate_window=[], candidates=[],
                      selected_panel=None, display_action="HOLD_PREVIOUS",
                      confidence=f"rule:{ntype.lower()}_hold", needs_review=cursor < 0)
            segments.append(seg)
            continue

        window = window_from(cursor)
        candidates = []
        for pid in window:
            ov = _lexical_overlap(text, by_id[pid].get("text", ""))
            dist = abs(order[pid] - cursor)
            candidates.append({"panel": pid, "ocr_overlap": ov,
                               "sequence_score": round(1.0 / (1.0 + dist), 3), "matches_tag": pid in tagged})
        candidates.sort(key=lambda c: (-c["ocr_overlap"], -c["sequence_score"]))
        best = candidates[0] if candidates else None
        best_ocr = best["ocr_overlap"] if best else 0

        tag = tagged[0] if tagged else None
        if tag and order[tag] < cursor - JUMP_TOLERANCE:
            # A real backward jump: don't trust it blindly, search the forward window instead.
            selected, confidence, needs_review = (best["panel"] if best else tag), "tag_behind_cursor", True
        elif tag and tag in window:
            tag_ov = next((c["ocr_overlap"] for c in candidates if c["panel"] == tag), 0)
            if best["panel"] == tag or best_ocr < MIN_OVERRIDE_EVIDENCE or best_ocr <= tag_ov:
                selected, confidence, needs_review = tag, "tag_confirmed_in_window", False
            else:
                selected, confidence, needs_review = best["panel"], "ocr_overrides_tag", True
        elif tag:
            # Forward, just beyond the normal lookahead window - the window was too
            # small for this stretch, not the tag wrong. Trust it, flag for a glance.
            selected, confidence, needs_review = tag, "tag_beyond_window_accepted", True
        elif best and best_ocr >= MIN_OVERRIDE_EVIDENCE:
            selected, confidence, needs_review = best["panel"], "ocr_match", False
        elif best:
            selected, confidence, needs_review = best["panel"], "no_evidence_default_sequence", True
        else:
            selected, confidence, needs_review = None, "no_candidates", True

        # candidate_window always reflects what was actually considered/chosen, so
        # "selected panel is in its own window" stays a true invariant even when a
        # tag beyond the normal lookahead (or a backward-jump fallback) was used.
        stored_window = window if selected is None or selected in window else [*window, selected]
        seg.update(story_position=order.get(selected, cursor), candidate_window=stored_window,
                  candidates=candidates, selected_panel=selected,
                  display_action="SHOW" if selected else "HOLD_PREVIOUS",
                  confidence=confidence, needs_review=needs_review)
        if selected:
            cursor = order[selected]
        segments.append(seg)

    return segments


def place_coverage(segments: List[Dict[str, Any]], panels: List[Dict[str, Any]], duration: float,
                   min_hold: float) -> Dict[str, Any]:
    """Every drawn panel must appear in the video. A panel no sentence picked is
    placed, in story order, into one of the sentences nearest to it (the one
    with the fewest extra panels so far). Its time is shared with that
    sentence's picks; plan.py does the timing. Text-only panels (captions,
    blank pages, credits) are never placed.

    Returns {"placed_by_order": [...], "uncovered": [...]}; `uncovered` is only
    non-empty when the narration is too short to give every panel min_hold.
    """
    order = {p["id"]: i for i, p in enumerate(panels)}
    shows = [k for k, s in enumerate(segments) if s.get("selected_panel")]
    for s in segments:
        s["extra_panels"] = []
    shown = {segments[k]["selected_panel"] for k in shows}
    missing = [p["id"] for p in panels if not p.get("text_only") and p["id"] not in shown]
    # Each shot holds at most two panels, and each needs min_hold of time.
    room = 2 * int(duration // min_hold) - len(shown)
    placed, uncovered = [], []
    for pid in sorted(missing, key=lambda x: order[x]):
        if len(placed) >= max(0, room) or not shows:
            uncovered.append(pid)
            continue
        i = order[pid]
        near = sorted(shows, key=lambda k: (abs(order[segments[k]["selected_panel"]] - i), k))[:3]
        k = min(near, key=lambda k: (len(segments[k]["extra_panels"]), abs(order[segments[k]["selected_panel"]] - i)))
        segments[k]["extra_panels"].append(pid)
        placed.append(pid)
    for seg in segments:
        if seg.get("selected_panel"):
            seg["selected_by_align"] = seg["selected_panel"]
            seq = sorted({seg["selected_panel"], *seg["extra_panels"]}, key=lambda p: order[p])
            seg["sequence"] = seq
        if not seg["extra_panels"]:
            del seg["extra_panels"]
    return {"placed_by_order": placed, "uncovered": uncovered}


def _timed_sentences(root: Path, chapter: str, sentences: List[Dict[str, Any]], vcfg: Dict[str, Any],
                     sync: str, log) -> tuple[List[Dict[str, Any]], str, float]:
    """Adds "start"/"end" to each sentence using whisper (cached if available),
    falling back to even spacing. This is the only I/O-dependent part.
    sync="off" skips whisper entirely (fast, deterministic estimated timing)."""
    audio = audio_path(root, chapter)
    if audio is None:
        raise UserError(f"No narration audio for chapter {chapter} yet.")
    from .build import END_TAIL
    from .ffmpeg import audio_timing

    timing = audio_timing(audio)
    duration = timing["duration"]
    if timing["speech_end"] + END_TAIL < duration - 0.5:
        duration = timing["speech_end"] + END_TAIL

    words: List[Dict[str, Any]] = []
    # This is only ever called when sentences (with tags) exist - the no-tags
    # "even" spread is a separate, earlier branch in build.py that never
    # reaches here - so no whisper words always means "estimated" (timing
    # interpolated from text length), never "even".
    mode = "estimated"
    if sync != "off" and sync_mod.available():
        try:
            from ..memory import SeriesMemory

            memory = SeriesMemory.load(root / "series_memory.json")
            words = sync_mod.transcribe(audio, model=vcfg.get("whisper_model", "small.en"),
                                        language=vcfg.get("whisper_language"),
                                        initial_prompt=sync_mod.character_prompt(memory),
                                        cache=sync_mod.timeline_path(root, chapter), log=log)
            mode = "synced"
        except UserError as exc:
            log(f"  ! {exc} Falling back to estimated timing.")
            mode = "estimated"
    elif sync != "off":
        log(f"  note: install whisper for exact sync:  {sync_mod.SYNC_HINT}")
    aligned = sync_mod.align(sentences, words, duration)
    timed = aligned["sentences"]
    for i, s in enumerate(timed):
        s["end"] = float(timed[i + 1]["start"]) if i + 1 < len(timed) else duration
    if mode == "synced" and not aligned.get("matched"):
        mode = "estimated"
    return timed, mode, duration


def build_alignment(root: Path, chapter: str, cfg, *, sync: str = "auto", log=print) -> Dict[str, Any]:
    root, chapter = Path(root), require_chapter(chapter)
    cue_data = cues_mod.load_cues(root, chapter)
    if not cue_data:
        raise UserError(f"Chapter {chapter}'s recap has no panel tags yet. Write the recap first.")
    panels_data = load_panels(root, chapter)
    if not panels_data:
        raise UserError(f"No panels for chapter {chapter} yet. Run:  video panels --chapter {chapter}")
    plist = panels_data["panels"]
    order = {p["id"]: i for i, p in enumerate(plist)}

    vcfg = dict(cfg.video) if hasattr(cfg, "video") else dict(cfg)
    timed, sync_mode, duration = _timed_sentences(root, chapter, cue_data["sentences"], vcfg, sync, log)

    segments = align_sentences(cue_data["sentences"], plist)
    for seg, t in zip(segments, timed):
        seg["start"], seg["end"] = round(float(t["start"]), 3), round(float(t["end"]), 3)

    min_hold = float(vcfg.get("min_hold", 3.0))
    coverage = place_coverage(segments, plist, duration, min_hold)
    coverage["art_panels"] = sum(1 for p in plist if not p.get("text_only"))
    coverage["text_only_excluded"] = [p["id"] for p in plist if p.get("text_only")]
    coverage["placed_by_order_count"] = len(coverage["placed_by_order"])
    validation = validate_mod.validate(segments, order, duration)
    queue = validate_mod.review_queue(segments, validation)

    result = {
        "version": 2, "chapter": chapter, "audio": {"duration": round(duration, 3)},
        "sync_mode": sync_mode, "segments": segments, "validation": validation, "review_queue": queue,
        "coverage": coverage,
    }
    atomic_write_text(alignment_path(root, chapter), json.dumps(result, indent=1, ensure_ascii=False))
    return result
