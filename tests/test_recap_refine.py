"""Refined recap system: tag-integrity splitter fixes, honest tagging counts,
and the sentence-integrity guards (incident log 4 and 5, map Part 2)."""

from pathlib import Path

from manhwa_ocr.video import cues

PANELS = [
    {"id": "P001-01", "text_only": False},
    {"id": "P001-02", "text_only": False},
    {"id": "P002-01", "text_only": True},   # lettering: never shown
]
KNOWN = {"P001-01", "P001-02", "P002-01"}


def _parse(text):
    return cues.parse(text, KNOWN)


# ---------------------------------------------------------------- splitter


def test_interior_question_inside_quote_stays_one_sentence():
    """Incident 4: the splitter used to break 'What is this? Is this the
    end?' mid-quote, orphaning the second half from its panel tag."""
    p = _parse('{P001-01} He shouts "What is this? Is this the end?" and collapses.')
    assert len(p["sentences"]) == 1
    assert p["sentences"][0]["panels"] == ["P001-01"]
    assert p["tagged_ratio"] == 1.0


def test_quote_final_terminator_with_lowercase_continuation_stays_whole():
    p = _parse('{P001-01} She asks "is it over?" but nothing happens next.')
    assert len(p["sentences"]) == 1
    p = _parse('{P001-01} He mutters "Fine." and keeps walking down the road.')
    assert len(p["sentences"]) == 1


def test_quote_final_terminator_with_capital_still_splits():
    p = _parse('{P001-01} He yells "Stop!" Then he dies quietly.')
    assert len(p["sentences"]) == 2


def test_bracket_cue_never_splits_a_sentence():
    """Incident 5 (production shape, chronicles ch1-5): '[LOOP: ...?]' used
    to be decapitated at '?]' -- the closing bracket sits in SENTENCE_END's
    trailing class -- leaking literal '[LOOP:' text into cues.json and
    stealing the following sentence's tag."""
    p = _parse(
        "{P001-01} Mong-Ryeon listens quietly.\n"
        "{P001-02} [LOOP: How did a child bring down a monster?] When she asks how, the truth unfolds.")
    assert len(p["sentences"]) == 2
    leak = [s for s in p["sentences"] if "[" in s["text"]]
    assert not leak
    assert p["sentences"][1]["panels"] == ["P001-02"]
    assert p["sentences"][1]["loops"] == ["How did a child bring down a monster?"]
    assert p["tagged_ratio"] == 1.0


def test_inline_loop_marker_attaches_without_placeholder_junk():
    p = _parse("[LOOP: why is he strong?] {P001-01} Because he trained for years.")
    s = p["sentences"][0]
    assert s["loops"] == ["why is he strong?"]
    assert "\x03" not in s["text"] and s["text"] == "Because he trained for years."


def test_whole_line_loop_marker_attaches_to_next_sentence():
    p = _parse("[LOOP: why?]\n{P001-01} Because he trained. {same} Every day of it.")
    assert p["sentences"][0]["loops"] == ["why?"]
    assert p["sentences"][0]["panels"] == ["P001-01"]
    assert p["sentences"][1]["same"] is True


# ------------------------------------------------------- honest tag counts


def test_same_only_sentence_counts_as_tagged():
    """Incident 4: {same} sentences used to count as untagged, so the ratio
    understated a properly anchored script."""
    p = _parse("{P001-01} One whole sentence here. {same} Second sentence continues it.")
    assert p["tagged_ratio"] == 1.0
    assert p["same_sentences"] == 1
    assert p["sentences"][1]["same"] is True and p["sentences"][1]["panels"] == []


def test_untagged_sentence_still_lowers_ratio():
    p = _parse("{P001-01} One whole sentence here. Second sentence has no tag at all.")
    assert p["tagged_ratio"] == 0.5


# ----------------------------------------------------------- check guards


def test_check_refuses_literal_cue_leak():
    report = cues.check('{P001-01} Good sentence here. Then he says [LOOP: broken', PANELS)
    assert any("leaks a literal editing cue" in e for e in report["errors"])


def test_check_refuses_lowercase_fragment():
    report = cues.check('{P001-01} It was hard as steel. "and then it kept going. More text follows here.', PANELS)
    assert any("starts lowercase" in e for e in report["errors"])


def test_check_refuses_barely_tagged_script():
    narration = " ".join(f"{{P001-01}} Tagged sentence number {i} is here." if i == 1
                         else f"Untagged sentence number {i} just talks along."
                         for i in range(1, 6))
    report = cues.check(narration, PANELS)
    assert any("anchored to a panel" in e for e in report["errors"])


def test_check_clean_script_has_no_errors():
    narration = ("{P001-01} The first proper sentence talks about the panel. "
                 "{P001-02} The second proper sentence covers another panel. "
                 "{same} A third sentence stays on the same picture again.")
    report = cues.check(narration, PANELS)
    assert report["errors"] == []


def test_shipped_loop_leak_shape_is_now_an_error():
    """The exact authoring shape that shipped in chronicles ch1-5."""
    narration = ("{P001-01} Ise quietly asks why she is feeding him.\n"
                 "{P001-02} [LOOP: How did a child bring down an immortal monster?]\n"
                 "- When she asks how, the grim truth of the massacre unfolds.")
    report = cues.check(narration, PANELS)
    assert report["errors"] == []
