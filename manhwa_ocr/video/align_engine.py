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

# --- Impactful-only panel selection (user rules, 2026-10-19) -----------------
# Time is scarce, panels are abundant: SELECT, don't cover. There is no
# "every drawn panel must appear" pass anymore (place_coverage was deleted).
# Unshown panels are not a problem: panels_shown/total is INFO ONLY.
MIN_SHOT = 3.0             # no shot is ever shorter than this (except the renderer's black tail)
LONG_SENTENCE = 8.0        # a sentence longer than this may split (never shorter ones)
LONG_SENTENCE_MAX = 15.0   # ... into at most 2 shots; above this, at most 3
TAGGED_COVERAGE_SHORT = 0.80  # tagged panel must cover >=80% of a <=8s sentence's window
TAGGED_COVERAGE_LONG = 0.60   # ... and >=60% of a longer sentence's window
SPLIT_TAGGED_SHARE = 0.65  # a 2-piece split gives the tagged panel ~65% of the window
SPLIT_TAGGED_SHARE_3 = 0.60  # a 3-piece split gives it ~60%
IMPACT_WINDOW = 6          # how far past the tagged panel a filler may come from


def max_shots_for_duration(duration: float) -> int:
    """Rule 1: at most floor(audio_seconds / 3) shots in the whole video."""
    return max(0, int(float(duration) // MIN_SHOT))


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
            # B1e: the sentence's motion override rides along (a string,
            # or a list parallel to the tagged panels); the planner
            # resolves it per shown panel.
            "motion": s.get("motion"),
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
        # Rule 6 — duplicate tag: this sentence EXPLICITLY tags the panel
        # already on screen (the previous sentence's pick). HOLD it; do NOT
        # trigger a filler, a re-show, or a new shot. The panel stays up
        # across both sentences, which is exactly what "same panel again"
        # means. An UNTAGGED sentence whose default pick lands on the same
        # panel is not a duplicate tag — it re-shows the cursor panel, as
        # before — so the rule keys on `tag`, never on `selected` alone:
        # without that, untagged sentences oscillate HOLD/SHOW.
        if (tag is not None and tag == selected and selected is not None and segments
                and segments[-1].get("selected_panel") == selected):
            seg.update(story_position=max(cursor, 0), candidate_window=[], candidates=[],
                       selected_panel=None, display_action="HOLD_PREVIOUS",
                       confidence="rule:duplicate_tag_hold", needs_review=False)
            segments.append(seg)
            continue
        seg.update(story_position=order.get(selected, cursor), candidate_window=stored_window,
                  candidates=candidates, selected_panel=selected,
                  display_action="SHOW" if selected else "HOLD_PREVIOUS",
                  confidence=confidence, needs_review=needs_review)
        if selected:
            cursor = order[selected]
            # More than one tag: only the FIRST shows (one tagged panel per
            # sentence). The rest stay on the segment as tagged_extras so
            # place_impactful() can use the writer's second tag as the
            # filler of a LONG sentence (>8s) — never as a second showing
            # of a short sentence, and never advancing the story cursor.
            if selected == tag:
                others = [p for p in usable[1:] if p != selected]
                if others:
                    seg["tagged_extras"] = others
        segments.append(seg)

    return segments


def _is_sfx_only(text: str) -> bool:
    """A panel whose only OCR text is a sound effect ("BOOM", "WHEW", "SLASH"):
    no words, no beat, never an impactful filler. Dialogue/narration has
    lowercase letters; SFX is short and all-caps."""
    t = (text or "").strip()
    return bool(t) and len(t) <= 16 and not any(c.islower() for c in t)


def filler_impact(panel: Dict[str, Any], *, sentence_text: str, order: Dict[str, int],
                  tagged_order: int, shown: set, future_tagged: set) -> tuple[bool, str]:
    """Rule 5 — the impact test for ONE candidate filler panel. ALL must hold:

    (a) named character + visible action/story beat: the panel carries a
        scene note from the transcript (the only action evidence the
        aligner has without an image). No scene note -> no verified beat.
    (b) visible face: the panel's measured figure share (panels.json
        "figure") is at least FIGURE_MIN — a text/SFX panel, a body
        fragment, or a back-of-head without a figure fails. Panels from
        before figure measurement carry no "figure" key: then a scene
        note alone is the evidence (fail closed only when both are absent).
    (c) capped by the caller (max 1 filler per sentence, 2 if >15s).
    (d) min 3s: enforced by the split shares in place_impactful, not here.
    (e) never repeat a panel already shown.
    (f) never backwards story order inside the sentence window, and never
        a spoiler: the panel must not be a LATER sentence's tagged panel.

    Returns (pass, reason). Failing closed is safe: if no panel passes,
    the tagged panel holds the whole window.
    """
    pid = panel.get("id", "")
    if panel.get("text_only") or panel.get("credits"):
        return False, "text-only/credits panel"
    pos = order.get(pid)
    if pos is None:
        return False, "unknown panel id"
    if pos < tagged_order:
        return False, "backwards story order (before the tagged panel)"
    if pid in shown:
        return False, "panel already shown (repeat)"
    if pid in future_tagged:
        return False, "spoiler: tagged by a later sentence"
    scene = (panel.get("scene") or "").strip()
    figure = panel.get("figure")
    if figure is not None:
        from .crops import FIGURE_MIN

        if float(figure) < FIGURE_MIN:
            return False, "no visible figure/face (figure share below FIGURE_MIN)"
    if not scene:
        return False, "no scene note: no verified character/action beat"
    text = (panel.get("text") or "").strip()
    if text and _is_sfx_only(text):
        return False, "SFX-only panel"
    return True, "impactful: figure + scene beat, in story order, not a repeat/spoiler"


def place_impactful(segments: List[Dict[str, Any]], panels: List[Dict[str, Any]], duration: float,
                    min_hold: float = MIN_SHOT) -> Dict[str, Any]:
    """Impactful-only panel placement — the replacement for place_coverage.

    The old pass ("every drawn panel must appear") is DELETED, not kept
    behind a flag: it placed panels no sentence was about into nearby
    sentences, which is exactly the wrong-picture time the sync gate now
    fails on. Selection here:

    - One tagged panel per sentence. A sentence <= LONG_SENTENCE (8s)
      shows ONLY its tagged panel for its whole window: no filler, no split.
    - A sentence > 8s splits into 2 shots (tagged ~65% + ONE impactful
      filler ~35%), each >= MIN_SHOT (3s). A sentence > 15s may split
      into 3 shots (tagged ~60% + up to TWO fillers) when two panels pass
      the impact test and each piece stays >= 3s. Shots per sentence are
      capped at ceil(duration / 3), preferring <= 2.
    - A filler comes from this sentence's own window only: story order
      after the tagged panel, before the next SHOW sentence's tagged
      panel (or IMPACT_WINDOW past the tag when there is no next tag),
      and must pass filler_impact(). The writer's second tag
      (tagged_extras[0]) is tried first for a long sentence: the writer
      chose it while looking at the panel, so it skips the figure/scene
      content checks but still must pass the structural ones
      (not a repeat, not backwards, not a spoiler, not text-only).
    - A duplicate-tag sentence (its tag is the previous sentence's panel)
      is converted to HOLD_PREVIOUS: the panel holds, no filler.
    - If no panel passes, the tagged panel holds the whole window.

    Every SHOW segment gets "sequence" (the panels it shows, in order),
    "sequence_shares" (each piece's share of the window — the planner
    splits on these, not evenly), "tagged_coverage" (the tagged panel's
    share of the window), "filler_panels" and "filler_impact" (the verdict
    per filler, read back by the sync gate).

    Returns the coverage/QA dict. "uncovered" is always [] — unshown
    panels are not a failure anymore. panels_shown/total is INFO ONLY:
    success is tagged coverage + min shot + impact pass.
    """
    order = {p["id"]: i for i, p in enumerate(panels)}
    by_id = {p["id"]: p for p in panels}
    art = [p for p in panels if not p.get("text_only")]
    art_total = len(art)

    # Suffix sets of tagged panels, for the spoiler check: future_tagged[i]
    # is every panel tagged by a SHOW segment AFTER segments[i].
    future_tagged: List[set] = [set() for _ in segments]
    acc: set = set()
    for i in range(len(segments) - 1, -1, -1):
        future_tagged[i] = set(acc)
        seg = segments[i]
        if seg.get("display_action") == "SHOW":
            acc |= {p for p in (seg.get("tagged_panels") or []) if p in by_id}

    shown: set = set()
    prev_selected: str | None = None
    placed: List[str] = []
    filler_seconds = 0.0
    tagged_cov: Dict[int, float] = {}

    for i, seg in enumerate(segments):
        if seg.get("display_action") != "SHOW" or not seg.get("selected_panel"):
            # HOLD ({same} / internal / pure-audio / duplicate-tag): nothing
            # is placed here, and nothing it hides is placed next door.
            # A duplicate-tag HOLD's panel IS its tag, so it covers 100%;
            # other HOLDs have no tag of their own and are not coverage-judged.
            if seg.get("display_action") == "HOLD_PREVIOUS":
                seg["sequence"] = []
                seg["sequence_shares"] = []
                seg["filler_panels"] = []
                seg["filler_impact"] = {}
                if seg.get("confidence") == "rule:duplicate_tag_hold":
                    seg["tagged_coverage"] = 1.0
                    tagged_cov[seg.get("sentence")] = 1.0
            continue
        sel = seg["selected_panel"]
        # Defensive duplicate-tag HOLD (align_sentences already converts
        # these; a hand-built segment list may still carry one).
        if prev_selected is not None and sel == prev_selected:
            seg.update(selected_panel=None, display_action="HOLD_PREVIOUS",
                       confidence="rule:duplicate_tag_hold", needs_review=False,
                       sequence=[], sequence_shares=[], tagged_coverage=1.0,
                       filler_panels=[], filler_impact={})
            tagged_cov[seg.get("sentence")] = 1.0
            continue
        dur = float(seg.get("end", 0.0)) - float(seg.get("start", 0.0))
        sel_order = order.get(sel, -1)
        # The filler's window: after the tagged panel, before the next
        # SHOW sentence's tagged panel (exclusive). No next tag: the next
        # IMPACT_WINDOW panels.
        next_tag_order: int | None = None
        for j in range(i + 1, len(segments)):
            nxt = segments[j].get("selected_panel")
            if segments[j].get("display_action") == "SHOW" and nxt in order:
                next_tag_order = order[nxt]
                break
        window_end = next_tag_order if next_tag_order is not None else sel_order + IMPACT_WINDOW + 1

        fillers: List[str] = []
        verdicts: Dict[str, Dict[str, Any]] = {}
        if dur > LONG_SENTENCE:
            max_fillers = 2 if dur > LONG_SENTENCE_MAX else 1
            # The writer's second tag goes first (structural checks only).
            tried: List[str] = []
            for cand in (seg.get("tagged_extras") or [])[:1]:
                if cand not in by_id:
                    continue
                tried.append(cand)
                p = by_id[cand]
                pos = order.get(cand, -1)
                if p.get("text_only") or p.get("credits"):
                    verdicts[cand] = {"pass": False, "reason": "text-only/credits panel"}
                elif pos < sel_order:
                    verdicts[cand] = {"pass": False, "reason": "backwards story order (before the tagged panel)"}
                elif cand in shown or cand == sel:
                    verdicts[cand] = {"pass": False, "reason": "panel already shown (repeat)"}
                elif cand in future_tagged[i]:
                    verdicts[cand] = {"pass": False, "reason": "spoiler: tagged by a later sentence"}
                else:
                    verdicts[cand] = {"pass": True, "reason": "writer second tag"}
                    fillers.append(cand)
            # Then the next story-order panels inside this sentence's window.
            for p in panels:
                if len(fillers) >= max_fillers:
                    break
                pid = p["id"]
                if pid in tried or pid == sel or pid in shown:
                    continue
                pos = order[pid]
                if pos <= sel_order or pos >= window_end:
                    continue
                if p.get("text_only"):
                    continue
                ok, reason = filler_impact(p, sentence_text=seg.get("text", ""), order=order,
                                           tagged_order=sel_order, shown=shown | {sel} | set(fillers),
                                           future_tagged=future_tagged[i])
                verdicts[pid] = {"pass": bool(ok), "reason": reason}
                if ok:
                    fillers.append(pid)

        # Split shares: tagged first, each piece >= MIN_SHOT, tagged share
        # >= its coverage threshold. Fewer pieces than the cap when the
        # fillers (or the time) are not there — the tagged panel holds.
        pieces = 1 + len(fillers)
        cap = 3 if dur > LONG_SENTENCE_MAX else 2
        cap = min(cap, max(1, int(-(-dur // MIN_SHOT)))) if dur > 0 else 1  # ceil(dur / 3)
        if pieces > cap:
            fillers = fillers[:cap - 1]
            pieces = 1 + len(fillers)
        if pieces == 3 and dur > LONG_SENTENCE_MAX:
            tagged_dur = min(max(SPLIT_TAGGED_SHARE_3 * dur, MIN_SHOT), dur - 2 * MIN_SHOT)
            rest = dur - tagged_dur
            shares = [tagged_dur / dur, (rest / 2) / dur, (rest / 2) / dur]
        elif pieces == 2:
            tagged_dur = min(max(SPLIT_TAGGED_SHARE * dur, MIN_SHOT), dur - MIN_SHOT)
            shares = [tagged_dur / dur, (dur - tagged_dur) / dur]
        else:
            fillers = []
            shares = [1.0]
        seq = [sel, *fillers]
        seg["sequence"] = seq
        seg["sequence_shares"] = shares
        seg["tagged_coverage"] = round(shares[0], 4)
        seg["filler_panels"] = list(fillers)
        seg["filler_impact"] = {pid: verdicts.get(pid, {"pass": False, "reason": "not evaluated"})
                                for pid in fillers}
        seg["split_pieces"] = len(seq)
        tagged_cov[seg.get("sentence")] = seg["tagged_coverage"]
        placed.extend(fillers)
        filler_seconds += dur * sum(shares[1:])
        shown.update(seq)
        prev_selected = sel
        if "tagged_extras" in seg:
            del seg["tagged_extras"]

    shown_panels = sorted(shown)
    return {
        "placed": placed,
        "placed_by_order": placed,
        "uncovered": [],
        "art_panels": art_total,
        "text_only_excluded": [p["id"] for p in panels if p.get("text_only")],
        "placed_by_order_count": len(placed),
        "panels_shown": shown_panels,
        "panels_total": art_total,
        "panels_shown_ratio": round(len(shown_panels) / art_total, 4) if art_total else 0.0,
        "tagged_coverage": tagged_cov,
        "filler_shots": len(placed),
        "filler_seconds": round(filler_seconds, 3),
        "max_shots": max_shots_for_duration(duration),
    }
def _timed_sentences(root: Path, chapter: str, sentences: List[Dict[str, Any]], vcfg: Dict[str, Any],
                     sync: str, log, settings: Dict[str, float],
                     warnings_out: List[Dict[str, str]] | None = None,
                     audio: Path | None = None) -> tuple[List[Dict[str, Any]], str, float]:
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
    would be pure interpolation, and interpolation is how drift starts.

    `audio` overrides the chapter audio file (hook / previously pass their
    explicit --audio file, e.g. audio/previously_7.mp3)."""
    audio = audio or audio_path(root, chapter)
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
    coverage = place_impactful(segments, plist, duration, min_hold)
    # place_impactful already reports art_panels / text_only_excluded /
    # placed_by_order_count; panels_shown/total there is INFO ONLY.
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
