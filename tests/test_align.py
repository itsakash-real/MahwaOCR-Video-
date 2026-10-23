"""Regression tests for the deterministic alignment engine (classify, align_sentences,
validate). These are the proof that the new architecture actually fixes the chapter 2
bug - a sentence about an unseen "creature chirping in his head" being shown as an
unrelated vampire-romance panel - rather than relying on another round of manual review.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.video.align_engine import align_sentences  # noqa: E402
from manhwa_ocr.video.classify import classify  # noqa: E402
from manhwa_ocr.video.validate import review_queue, validate  # noqa: E402


# ------------------------------------------------------------------ classify()


def test_internal_narration_is_classified_internal():
    assert classify("He thinks to himself that none of this matters.")[0] == "INTERNAL"
    assert classify("In his head, the creature chirps an inquisitive sound.")[0] == "INTERNAL"
    assert classify("She wonders if anyone noticed.")[0] == "INTERNAL"


def test_pure_sound_with_no_actor_is_pure_audio():
    assert classify("A terrifying roar echoes through the dungeon.")[0] == "PURE_AUDIO"
    assert classify("A strange chirp sounds from somewhere nearby.")[0] == "PURE_AUDIO"


def test_visible_action_is_visual_even_when_it_makes_a_sound():
    assert classify("The monster roars.")[0] == "VISUAL_ACTION"


def test_retrospective_thought_it_was_is_not_internal():
    # "she thought it was" is a common past-tense comparison about something
    # VISIBLE ("not the argument she thought it was"), not a description of
    # an unspoken thought -- misclassifying it held the previous panel ~15s.
    assert classify("Not the argument she thought it was.")[0] == "VISUAL_ACTION"
    assert classify("That wasn't the plan they thought it would be.")[0] == "VISUAL_ACTION"
    # a genuine internal thought still classifies correctly
    assert classify("He thought about the plan all night, alone in the dark.")[0] == "INTERNAL"
    assert classify("The monster roars and charges toward him.")[0] == "VISUAL_ACTION"
    assert classify("Across the room, the television blares breaking news.")[0] == "VISUAL_ACTION"


# ------------------------------------------------------------------ align_sentences()


def _panel(pid, *, text="", text_only=False):
    return {"id": pid, "text": text, "text_only": text_only}


def test_known_vampire_panel_bug_is_never_selected():
    """The actual chapter 2 failure: a sentence about a creature chirping *in his
    head* was tagged to P022-02, an unrelated vampire-romance panel. The new engine
    must hold the previous panel instead of ever showing P022-02 for it."""
    panels = [_panel("P021-01"), _panel("P022-01", text_only=True), _panel("P022-02"),
             _panel("P022-03"), _panel("P023-01")]
    sentences = [
        {"text": "Yo-han offers his condolences, but it really wasn't his fault.", "panels": ["P021-01"]},
        {"text": ("In his head, the creature chirps an inquisitive sound, forcing Yo-han to "
                  "mutter under his breath and tell it to stay silent."), "panels": ["P022-02"]},
        {"text": "To clear his head, he pulls out his phone and searches old archives.", "panels": ["P023-01"]},
    ]
    segments = align_sentences(sentences, panels)
    bad = segments[1]
    assert bad["narration_type"] == "INTERNAL"
    assert bad["selected_panel"] is None
    assert bad["display_action"] == "HOLD_PREVIOUS"
    assert "P022-02" != bad["selected_panel"]  # the vampire panel is never chosen


def test_internal_narration_holds_previous_panel():
    panels = [_panel("P001-01"), _panel("P001-02")]
    sentences = [{"text": "He picks up the bag.", "panels": ["P001-01"]},
                {"text": "He thinks to himself that this changes everything.", "panels": ["P001-02"]}]
    segments = align_sentences(sentences, panels)
    assert segments[1]["narration_type"] == "INTERNAL"
    assert segments[1]["selected_panel"] is None
    assert segments[1]["display_action"] == "HOLD_PREVIOUS"
    assert segments[1]["needs_review"] is False  # there was a previous panel to hold


def test_pure_audio_narration_holds_previous_panel():
    panels = [_panel("P001-01"), _panel("P001-02")]
    sentences = [{"text": "He picks up the bag.", "panels": ["P001-01"]},
                {"text": "A terrifying roar echoes through the dungeon.", "panels": ["P001-02"]}]
    segments = align_sentences(sentences, panels)
    assert segments[1]["narration_type"] == "PURE_AUDIO"
    assert segments[1]["selected_panel"] is None
    assert segments[1]["display_action"] == "HOLD_PREVIOUS"


def test_visual_sound_action_still_gets_matched():
    panels = [_panel("P001-01"), _panel("P002-01"), _panel("P002-02")]
    sentences = [{"text": "He waits in the corridor.", "panels": ["P001-01"]},
                {"text": "The monster roars and charges toward him.", "panels": ["P002-01"]}]
    segments = align_sentences(sentences, panels)
    assert segments[1]["narration_type"] == "VISUAL_ACTION"
    assert segments[1]["selected_panel"] == "P002-01"
    assert segments[1]["confidence"] == "tag_confirmed_in_window"
    assert segments[1]["needs_review"] is False


def test_the_recap_tag_wins_over_word_overlap_with_another_panel():
    """The writer tagged this sentence while looking at the panels. Another panel
    sharing a few words with the sentence must not replace it: that showed the wrong
    picture while the narrator spoke (the Chapter 8 mismatch)."""
    panels = [_panel("P010-01"), _panel("P010-02", text="SYSTEM Strength +20"), _panel("P010-03")]
    sentences = [{"text": "He stares at the ceiling, lost in doubt.", "panels": ["P010-01"]},
                {"text": "The system suddenly announces that his strength has increased by 20.",
                 "panels": ["P010-03"]}]
    seg = align_sentences(sentences, panels)[1]
    assert seg["selected_panel"] == "P010-03"
    assert seg["confidence"] == "tag_confirmed_in_window"
    assert seg["needs_review"] is False


def test_a_tag_on_a_text_only_panel_falls_back_to_the_next_real_panel():
    panels = [_panel("P010-01"), _panel("P010-02", text_only=True), _panel("P010-03", text="strength increased")]
    sentences = [{"text": "He stares at the ceiling, lost in doubt.", "panels": ["P010-01"]},
                {"text": "His strength has increased.", "panels": ["P010-02"]}]
    seg = align_sentences(sentences, panels)[1]
    assert seg["selected_panel"] != "P010-02"


def test_every_tag_on_a_multi_panel_sentence_is_shown_in_order():
    panels = [_panel(f"P001-0{i}") for i in range(1, 6)]
    sentences = [{"text": "Then the door opens and he walks in slowly.",
                  "panels": ["P001-01", "P001-02", "P001-03"]},
                {"text": "He sits down.", "panels": ["P001-04"]}]
    segs = align_sentences(sentences, panels)
    assert segs[0]["selected_panel"] == "P001-01"
    assert segs[0]["tagged_extras"] == ["P001-02", "P001-03"]


def test_a_shared_stopword_never_overrides_a_tag():
    """The actual chapter 2 cascade bug: panel text 'THE TRUTH IS...' shares only
    the common word "the" with an unrelated sentence - that must never count as
    evidence strong enough to override a correctly in-window tag, because one
    false override corrupts the cursor for every sentence after it."""
    panels = [_panel("P001-01"), _panel("P002-01", text="LIKEMANGA.INK / GRRR... / THE TRUTH IS..."),
             _panel("P002-02")]
    sentences = [{"text": "He waits by the door.", "panels": ["P001-01"]},
                {"text": ("The Seoul police are completely baffled because zero physical weapons or "
                          "forensic traces were recovered from the crime scene."), "panels": ["P002-02"]}]
    segments = align_sentences(sentences, panels)
    assert segments[1]["selected_panel"] == "P002-02"
    assert segments[1]["confidence"] == "tag_confirmed_in_window"


def test_forward_tag_beyond_the_window_is_trusted_but_flagged():
    """A tag far AHEAD of the current position usually just means the window was
    too small for a sparse stretch of story, not that the tag is wrong - it must
    still be used (never get the cursor stuck), just flagged for a quick glance."""
    panels = [_panel(f"P{i:03d}-01") for i in range(1, 21)]
    sentences = [{"text": "He walks down the hallway.", "panels": ["P001-01"]},
                {"text": "He opens the door.", "panels": ["P020-01"]}]
    segments = align_sentences(sentences, panels)
    seg = segments[1]
    assert seg["selected_panel"] == "P020-01"
    assert seg["needs_review"] is True
    assert seg["confidence"] == "tag_beyond_window_accepted"


def test_backward_tag_is_trusted_but_flagged():
    """A tag pointing well BEHIND the current position is a deliberate backward
    glance (the recap looks back at a face or a wound). Reviewing the real
    chapters showed the writer is right about these far more often than the
    forward window guess, so the tag stands and the glance is flagged for
    review instead of being silently overridden."""
    panels = [_panel(f"P{i:03d}-01") for i in range(1, 11)]
    sentences = [{"text": "He walks down the hallway.", "panels": ["P008-01"]},
                {"text": "He opens the door.", "panels": ["P001-01"]}]
    segments = align_sentences(sentences, panels)
    seg = segments[1]
    assert seg["selected_panel"] == "P001-01"
    assert seg["needs_review"] is True
    assert seg["confidence"] == "tag_behind_cursor_accepted"
    # The backward glance is legal: validation must not fail the timeline for it.
    result = validate(segments, _order(panels), duration=8.0)
    assert result["checks"]["no_backward_jumps"] == "PASS"


# ------------------------------------------------------------------ validate()


def _order(panels):
    return {p["id"]: i for i, p in enumerate(panels)}


def test_validate_flags_missing_panel():
    panels = [_panel("P001-01"), _panel("P001-02")]
    segments = [{"sentence": 1, "start": 0.0, "end": 2.0, "narration_type": "VISUAL_ACTION",
                "selected_panel": "P099-01", "candidate_window": ["P099-01"], "display_action": "SHOW",
                "confidence": "ocr_match"}]
    result = validate(segments, _order(panels), duration=2.0)
    assert result["status"] == "FAIL"
    assert result["checks"]["panels_exist"] == "FAIL"
    assert review_queue(segments, result) == [1]


def test_validate_flags_timestamp_gap_and_overlap():
    panels = [_panel("P001-01")]
    gap = [
        {"sentence": 1, "start": 0.0, "end": 2.0, "narration_type": "VISUAL_ACTION", "selected_panel": "P001-01",
         "candidate_window": ["P001-01"], "display_action": "SHOW", "confidence": "ocr_match"},
        {"sentence": 2, "start": 5.0, "end": 7.0, "narration_type": "VISUAL_ACTION", "selected_panel": "P001-01",
         "candidate_window": ["P001-01"], "display_action": "SHOW", "confidence": "ocr_match"},
    ]
    result = validate(gap, _order(panels), duration=7.0)
    assert result["status"] == "FAIL"
    assert result["checks"]["no_gaps_or_overlaps"] == "FAIL"

    overlap = [
        {"sentence": 1, "start": 0.0, "end": 4.0, "narration_type": "VISUAL_ACTION", "selected_panel": "P001-01",
         "candidate_window": ["P001-01"], "display_action": "SHOW", "confidence": "ocr_match"},
        {"sentence": 2, "start": 2.0, "end": 6.0, "narration_type": "VISUAL_ACTION", "selected_panel": "P001-01",
         "candidate_window": ["P001-01"], "display_action": "SHOW", "confidence": "ocr_match"},
    ]
    result = validate(overlap, _order(panels), duration=6.0)
    assert result["status"] == "FAIL"
    assert result["checks"]["no_gaps_or_overlaps"] == "FAIL"


def test_validate_passes_a_clean_timeline():
    panels = [_panel("P001-01"), _panel("P001-02")]
    segments = [
        {"sentence": 1, "start": 0.0, "end": 2.0, "narration_type": "VISUAL_ACTION", "selected_panel": "P001-01",
         "candidate_window": ["P001-01"], "display_action": "SHOW", "confidence": "tag_confirmed_in_window"},
        {"sentence": 2, "start": 2.0, "end": 4.0, "narration_type": "INTERNAL", "selected_panel": None,
         "candidate_window": [], "display_action": "HOLD_PREVIOUS", "confidence": "rule:internal_hold"},
    ]
    result = validate(segments, _order(panels), duration=4.0)
    assert result["status"] == "PASS"
    assert review_queue(segments, result) == []
