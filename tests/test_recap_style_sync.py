"""Spec/code sync for the recap style lanes (incident 8).

The style systems live in manhwa_ocr/script.py; their specs live in
docs/RECAP_STYLE_*.md; they have drifted before. This test fails when a
doc and the code disagree on the CHECKABLE claims: gate codes the doc says
the lint enforces, banned-phrase examples the doc quotes, required marker
phrases, and the pinned length band. Taste-level doc claims ("tone and
humor are the prompt's job") are deliberately not checked -- a doc
paragraph cannot be a regex.
"""

import re
from pathlib import Path

from manhwa_ocr import recap_quality, script, script_lint
from manhwa_ocr.config import DEFAULT_CONFIG

DOCS = Path(__file__).resolve().parent.parent / "docs"


def _doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- gate codes

def _lint_codes(lint, opener_text, continuation_text):
    """Every code the lint can emit, probed with one bad input per rule."""
    codes = set()
    codes |= {i.code for i in lint(opener_text, range_opener=True)}
    codes |= {i.code for i in lint(continuation_text, range_opener=False)}
    return codes


PF_OPENER_BAD = (
    "He was reborn in a past life and then he trained for a very long time indeed, "
    "and eventually he arrived at the school where he was incredibly strong. "
    "He beat a student there. Everyone was surprised by his strength. "
    "And so ends the chapter. See you next time.\n"
    "Camera pans left across the silent stands for a moment.\n"
    "[Camera: slow pan across the arena stands]\n"
    "Week banana of training happened before all of that."
)
PF_CONT_BAD = "Welcome back to the story. He was reborn after his death. And so ends it all, thanks for watching."

MB_OPENER_BAD = (
    "The fighter was reborn into a past life and lived through a long childhood before the duel began. "
    "He was incredibly strong and won the fight without anyone reacting at all."
)
MB_CONT_BAD = "The mage walks in and casts a spell while the enemy falls down badly."


def test_powerfantasy_doc_gate_codes_match_the_lint():
    doc = _doc("RECAP_STYLE_POWERFANTASY.md")
    codes = _lint_codes(script_lint.lint_powerfantasy, PF_OPENER_BAD, PF_CONT_BAD)
    assert codes == {"hook-formula", "lore-drop", "past-life-opening", "finale-ending",
                     "restart-opening", "camera-cue", "camera-direction", "cue-form"}
    for code in codes:
        assert code in doc, \
            f"docs/RECAP_STYLE_POWERFANTASY.md does not mention gate `{code}` the lint enforces"


def test_manhwabro_doc_gate_codes_match_the_lint():
    doc = _doc("RECAP_STYLE_MANHWABRO.md")
    codes = _lint_codes(script_lint.lint_manhwabro, MB_OPENER_BAD, MB_CONT_BAD)
    assert "bro-pivot" in codes and "bro-epithet" in codes
    for code in ("bro-pivot", "bro-epithet", "past-life-opening",
                 "finale-ending", "restart-opening", "camera-cue", "cue-form"):
        assert code in doc, f"docs/RECAP_STYLE_MANHWABRO.md does not mention gate `{code}`"


def test_doc_phrase_examples_exist_in_the_lint_constants():
    """Banned-phrase examples the docs quote must be the ones the code bans."""
    pf = _doc("RECAP_STYLE_POWERFANTASY.md")
    for phrase in ("until next time", "welcome back", "previously on"):
        assert phrase in pf
        assert any(phrase in p for p in script_lint.FINALE_PHRASES + script_lint.RESTART_PHRASES), \
            f"doc example {phrase!r} is not in the code's banned lists"
    assert script_lint.LORE_PHRASE in pf, "the lore-drop phrase the code requires is not in the doc"


def test_style_docs_name_real_code_symbols():
    for doc_name, module, symbols in (
        ("RECAP_STYLE_POWERFANTASY.md", script, ("POWERFANTASY_SYSTEM", "POWERFANTASY_PROMPT")),
        ("RECAP_STYLE_POWERFANTASY.md", script_lint, ("lint_powerfantasy",)),
        ("RECAP_STYLE_MANHWABRO.md", script, ("MANHWABRO_SYSTEM", "MANHWABRO_PROMPT")),
        ("RECAP_STYLE_MANHWABRO.md", script_lint, ("lint_manhwabro",)),
        ("RECAP_STYLE_SPEEDRUN.md", script, ("SPEEDRUN_SYSTEM",)),
    ):
        doc = _doc(doc_name)
        for sym in symbols:
            assert sym in doc, f"{doc_name} does not name {sym}"
            assert hasattr(module, sym), f"{doc_name} names {sym} but it does not exist"
    # the manhwabro doc says the lane is registered in config.py (the recap
    # section's style option documents every lane name)
    config_src = Path(script_lint.__file__).parent.joinpath("config.py").read_text(encoding="utf-8")
    assert "manhwabro" in config_src


def test_length_band_is_pinned_in_one_place():
    """The narration length contract (6-8 finished minutes at the default
    1.3x speed = 1170-1560 words) must stay identical in the quality
    module's constants and the config defaults."""
    assert recap_quality.MIN_WORDS == DEFAULT_CONFIG["recap"]["min_words"] == 1170
    assert recap_quality.MAX_WORDS == DEFAULT_CONFIG["recap"]["max_words"] == 1560


def test_speedrun_doc_claims_the_machinery_it_relies_on():
    doc = _doc("RECAP_STYLE_SPEEDRUN.md")
    assert "[music]" in doc
    assert re.search(r"music", script_lint._ALLOWED_CUE_RE.pattern, re.IGNORECASE), \
        "the doc promises [music] cues are allowed by the lint's cue whitelist"
