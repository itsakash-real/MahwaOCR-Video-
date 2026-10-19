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
  3. the recap's original tag - the writer's own choice, so it always stands:
     even when it glances backwards in the story, and even when another
     panel's own OCR'd text has a strong word overlap with the sentence
     (overlap above video.sync.override_evidence only flags the disagreement
     for review - it never replaces the tag).
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
JUMP_TOLERANCE = 2  # a tag this far "behind" the running position is a deliberate backward
                    # glance: still trusted, but flagged for review instead of silently taken
MIN_WORD = 3        # a matched token shorter than this is too common to count as evidence

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
    "the", "and", "are", "was", "you", "she", "not", "but", "for", "all",
    "any", "can", "one", "two", "six", "ten", "our",
}


def alignment_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.alignment.json"


MIN_OVERRIDE_EVIDENCE = 10  # roughly two real content words, or one long distinctive one


def _lexical_overlap_sets(s_tokens: set, p_tokens: set) -> int:
    """_lexical_overlap on pre-tokenized sets (see _lexical_overlap)."""
    return sum(len(t) for t in (p_tokens & s_tokens))


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
    return _lexical_overlap_sets(set(sync_mod.tokens(text)), p_tokens)


def align_sentences(sentences: List[Dict[str, Any]], panels: List[Dict[str, Any]], *,
                    override_evidence: float | None = None) -> List[Dict[str, Any]]:
    """The pure decision engine. `sentences`: [{"text": ..., "panels": [ids]}, ...]
    in story order. `panels`: the chapter's panel list, in story order, each
    with at least "id" and optionally "text_only"/"text". No timestamps, no
    I/O - every segment here still needs "start"/"end" added by the caller.

    `override_evidence` is the OCR-overlap score (video.sync.override_evidence)
    above which another panel's word overlap with the sentence flags a recap
    tag for review (the tag still shows); defaults to the shipped value.
    """
    if override_evidence is None:
        override_evidence = float(sync_mod.SYNC_DEFAULTS["override_evidence"])
    by_id = {p["id"]: p for p in panels}
    order = {p["id"]: i for i, p in enumerate(panels)}
    art_positions = [i for i, p in enumerate(panels) if not p.get("text_only")]
    # Tokenize every panel's OCR text once: the old code re-tokenized the
    # sentence AND each candidate panel on every candidate check.
    panel_tokens = {
        pid: {t for t in sync_mod.tokens(p.get("text", "")) if len(t) >= MIN_WORD and t not in STOPWORDS}
        for pid, p in by_id.items()
    }

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

        # Tags the writer gave this sentence, in order. A text-only panel
        # can't be shown, unless it carries scene/text notes (pale-art guard
        # against a bad text_only flag); the rest are the sentence's pictures.
        def _usable(pid: str) -> bool:
            p = by_id[pid]
            if not p.get("text_only"):
                return True
            return bool((p.get("scene") or "").strip() or (p.get("text") or "").strip())

        usable = [p for p in tagged if _usable(p)]
        tag = usable[0] if usable else None

        window = window_from(cursor)
        candidates = []
        s_tokens = set(sync_mod.tokens(text))
        for pid in window:
            ov = _lexical_overlap_sets(s_tokens, panel_tokens[pid])
            dist = abs(order[pid] - cursor)
            candidates.append({"panel": pid, "ocr_overlap": ov,
                               "sequence_score": round(1.0 / (1.0 + dist), 3), "matches_tag": pid in tagged})
        candidates.sort(key=lambda c: (-c["ocr_overlap"], -c["sequence_score"]))
        best = candidates[0] if candidates else None
        best_ocr = best["ocr_overlap"] if best else 0
        if tag and order[tag] < cursor - JUMP_TOLERANCE:
            # A tag pointing behind the story position. Reviewing the real
            # chapters showed the writer is right about these far more often
            # than the forward window guess is (the recap glances back at a
            # face or a wound on purpose); the old code replaced the tag and
            # showed the wrong picture. Trust the tag, flag the glance.
            selected, confidence, needs_review = tag, "tag_behind_cursor_accepted", True
        elif tag and tag in window:
            # The writer chose this panel while looking at it, and the real
            # chapters settled the rest: overriding the tag because another
            # panel's own OCR'd text overlapped the sentence replaced the tag
            # with the wrong picture every time it fired (chapter 3: overlap
            # 56 - the vision model had filed a nearby bubble's line under
            # that panel - and the recording still matched the tag). The tag
            # always stands; overlap above video.sync.override_evidence only
            # flags the disagreement so video pair surfaces it for review.
            disagree = bool(best and best["panel"] != tag and best_ocr > override_evidence)
            selected, confidence = tag, "tag_confirmed_in_window"
            needs_review = disagree
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
        # tag beyond the normal lookahead (or a backward tag) was used.
        stored_window = window if selected is None or selected in window else [*window, selected]
        seg.update(story_position=order.get(selected, cursor), candidate_window=stored_window,
                  candidates=candidates, selected_panel=selected,
                  display_action="SHOW" if selected else "HOLD_PREVIOUS",
                  confidence=confidence, needs_review=needs_review)
        if selected:
            cursor = order[selected]
            # More than one tag: the sentence shows all of them (plan.py splits its time).
            if selected == tag:
                others = [p for p in usable[1:] if p != selected and order[p] >= order[selected]]
                if others:
                    seg["tagged_extras"] = others
                    cursor = max(cursor, *(order[p] for p in others))
        segments.append(seg)

    return segments


def place_coverage(segments: List[Dict[str, Any]], panels: List[Dict[str, Any]], duration: float,
                   min_hold: float, filler_full: float | None = None) -> Dict[str, Any]:
    """Every drawn panel must appear in the video. A panel no sentence picked is
    placed, in story order, into one of the sentences nearest to it (the one
    with the fewest extra panels so far). Its time is shared with that
    sentence's picks; plan.py does the timing. Text-only panels (captions,
    blank pages, credits) are never placed.

    `filler_full` (video.sync.filler_full): prefer sentences at least that long
    as hosts, so a filler always shares the sentence with a full min_hold's
    worth of its own picture instead of squeezing it (a sentence filled end to
    end). When every nearby sentence is shorter than that, the nearest ones are
    used anyway - a filler is never dropped because its neighbours were short.

    A sentence's own pick leads its sequence (it is what the narrator is
    talking about); fillers follow it in story order.

    Returns {"placed_by_order": [...], "uncovered": [...]}; `uncovered` is only
    non-empty when the narration is too short to give every panel min_hold.
    """
    if filler_full is None:
        filler_full = float(sync_mod.SYNC_DEFAULTS["filler_full"])
    order = {p["id"]: i for i, p in enumerate(panels)}
    shows = [k for k, s in enumerate(segments) if s.get("selected_panel")]
    for s in segments:
        s["extra_panels"] = list(s.get("tagged_extras") or [])
    shown = {segments[k]["selected_panel"] for k in shows} | {p for s in segments for p in s["extra_panels"]}
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
        lit = [k for k in near
               if float(segments[k]["end"]) - float(segments[k]["start"]) >= filler_full]
        pool = lit or near
        k = min(pool, key=lambda k: (len(segments[k]["extra_panels"]), abs(order[segments[k]["selected_panel"]] - i)))
        segments[k]["extra_panels"].append(pid)
        placed.append(pid)
    for seg in segments:
        if seg.get("selected_panel"):
            seg["selected_by_align"] = seg["selected_panel"]
            seq = [seg["selected_panel"]] + sorted(
                (p for p in set(seg["extra_panels"]) if p != seg["selected_panel"]), key=lambda p: order[p])
            seg["sequence"] = seq
        if not seg["extra_panels"]:
            del seg["extra_panels"]
    return {"placed_by_order": placed, "uncovered": uncovered}


def _timed_sentences(root: Path, chapter: str, sentences: List[Dict[str, Any]], vcfg: Dict[str, Any],
                     sync: str, log, settings: Dict[str, float],
                     warnings_out: List[Dict[str, str]] | None = None) -> tuple[List[Dict[str, Any]], str, float]:
    """Adds "start"/"end" to each sentence using whisper (cached if available),
    falling back to even spacing. This is the only I/O-dependent part.
    sync="off" skips whisper entirely (fast, deterministic estimated timing).

    P0 safety lock: a fallback to estimated timing is never silent. When
    `warnings_out` is given, every fallback appends a machine-readable
    warning {code, message, fix} there; the caller stores it in the
    alignment (timing_warnings) and `video status` surfaces it. The
    return value is unchanged (timed, mode, duration) so existing callers
    keep working. Render still refuses an "estimated" plan without --force.

    With real timing, a run of consecutive sentences whisper never matched,
    longer than video.sync.max_unmatched, stops the build: timing that stretch
    would be pure interpolation, and interpolation is how drift starts."""
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
    def _warn(code: str, message: str, fix: str) -> None:
        if warnings_out is not None:
            warnings_out.append({"code": code, "message": message, "fix": fix})
        log(f"  ! {message} ({fix})")

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
            _warn("whisper_transcribe_failed",
                  f"Whisper transcription failed ({exc}); using estimated timing, "
                  f"so the pictures will drift from the voice.",
                  f"Fix the transcription, then re-run: video align --chapter {chapter}")
            mode = "estimated"
    elif sync != "off":
        _warn("whisper_unavailable",
              "Whisper is not installed, so timing is estimated from text length, "
              "not synced to the real narration.",
              f"Install whisper for exact sync ({sync_mod.SYNC_HINT}); render refuses "
              f"estimated timing without --force. Command: video align --chapter {chapter}")
    else:
        _warn("sync_off",
              "sync=off was passed, so timing is estimated from text length.",
              f"Re-run without --off for real sync: video align --chapter {chapter}")
    aligned = sync_mod.align(sentences, words, duration,
                             min_segment=float(settings.get("min_segment", sync_mod.MIN_SEGMENT)))
    timed = aligned["sentences"]
    for i, s in enumerate(timed):
        s["end"] = float(timed[i + 1]["start"]) if i + 1 < len(timed) else duration
    if mode == "synced" and not aligned.get("matched"):
        _warn("whisper_no_matches",
              "Whisper ran but matched no words, so timing fell back to estimated.",
              f"Check the narration audio, then re-run: video align --chapter {chapter}")
        mode = "estimated"
    if mode == "synced":
        over = sync_mod.long_unmatched_run(aligned, settings["max_unmatched"])
        if over is not None:
            from .build import _mmss
            where = (f"sentences {over['first']}-{over['last']}" if over["first"] != over["last"]
                     else f"sentence {over['first']}")
            raise UserError(
                f"Chapter {chapter}: {where} ({_mmss(over['start'])}-{_mmss(over['end'])}, "
                f"{over['seconds']:.1f}s) have no matched speech in the timeline - the pictures there "
                f"would be timed by guesswork.\nBump video.sync.max_unmatched in manhwa-ocr.yaml if that "
                f"stretch is really fine (currently {settings['max_unmatched']:g}s), or check the narration "
                f"and re-run transcription.")
    return timed, mode, duration


def build_alignment(root: Path, chapter: str, cfg, *, sync: str = "auto", log=print) -> Dict[str, Any]:
    root, chapter = Path(root), require_chapter(chapter)
    cue_data = cues_mod.load_cues_strict(root, chapter)
    if not cue_data:
        raise UserError(f"Chapter {chapter}'s recap has no panel tags yet. Write the recap first.")
    panels_data = load_panels(root, chapter)
    if not panels_data:
        raise UserError(f"No panels for chapter {chapter} yet. Run:  video panels --chapter {chapter}")
    plist = panels_data["panels"]
    order = {p["id"]: i for i, p in enumerate(plist)}

    vcfg = dict(cfg.video) if hasattr(cfg, "video") else dict(cfg)
    settings = sync_mod.effective_settings(vcfg, log=log)
    timing_warnings: List[Dict[str, str]] = []
    timed, sync_mode, duration = _timed_sentences(root, chapter, cue_data["sentences"], vcfg, sync, log,
                                                  settings, warnings_out=timing_warnings)

    segments = align_sentences(cue_data["sentences"], plist,
                               override_evidence=settings["override_evidence"])
    for seg, t in zip(segments, timed):
        seg["start"], seg["end"] = round(float(t["start"]), 3), round(float(t["end"]), 3)
        seg["timing_source"] = t.get("timing_source", "estimated")

    min_hold = settings["min_hold"]
    coverage = place_coverage(segments, plist, duration, min_hold, settings["filler_full"])
    coverage["art_panels"] = sum(1 for p in plist if not p.get("text_only"))
    coverage["text_only_excluded"] = [p["id"] for p in plist if p.get("text_only")]
    coverage["placed_by_order_count"] = len(coverage["placed_by_order"])
    validation = validate_mod.validate(segments, order, duration)
    queue = validate_mod.review_queue(segments, validation)

    result = {
        "version": 2, "chapter": chapter, "audio": {"duration": round(duration, 3)},
        "sync_mode": sync_mode, "timing_warnings": timing_warnings,
        "segments": segments, "validation": validation, "review_queue": queue,
        "coverage": coverage,
    }
    atomic_write_text(alignment_path(root, chapter), json.dumps(result, indent=1, ensure_ascii=False))
    return result
