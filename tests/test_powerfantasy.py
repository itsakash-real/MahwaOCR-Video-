"""Power Fantasy recap style (recap.style: powerfantasy).

Covers: style selection (and the speedrun selector it shares the knob with),
the range gate (a range is recapped only once every transcript in it
exists), whole-range prompt assembly, the script lint gate, and the lint's
cross-check against the title-card detector's accepted cue forms.
"""

import json
from pathlib import Path

import pytest

from manhwa_ocr.config import load_config
from manhwa_ocr.script import (
    POWERFANTASY_SYSTEM,
    SCRIPT_SYSTEM,
    SPEEDRUN_SYSTEM,
    RecapBuilder,
    _range_span,
    _transcript_digest,
)
from manhwa_ocr.script_lint import lint_powerfantasy
from manhwa_ocr.util import UserError
from manhwa_ocr.video.titlecards import detect_title_cue

OPENER = (
    "The arena falls silent as the janitor steps onto the dueling platform. "
    "This guy is Jin, a mere cleaner at the Royal Sword Academy, but he just "
    "shattered the champion's blade with one casual swing. "
    "The professors are screaming while the poor guy stares at his broken sword. "
    "But what none of them know is, our guy carries the soul of the strongest "
    "swordsman who ever lived. "
    "By week one, he was already stronger than every senior in the school. "
    "Age fifteen. "
    "By age fifteen he had mastered the forbidden blade art. "
    "Now the academy elders are watching him, and the first real hunter has "
    "just taken the bait."
)

CONTINUATION = (
    "Our guy is already moving before the bell finishes ringing, and the whole "
    "arena can barely follow him with their eyes. "
    "He casually destroys the second challenger while the crowd loses its mind, "
    "then brutally shuts down the third one before his feet even touch the ground. "
    "The professors are whispering now, terrified, because nobody has ever seen "
    "speed like this before. "
    "The academy elder stands up, pale, because the next hunter is already at the gate."
)


def _workspace(tmp_path: Path, style="powerfantasy", labels=("1", "2", "3")):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    for c in labels:
        (ws / f"chapter_{c}.md").write_text(
            f"# Ch {c}\n\n## Scene 1\n\nSomething happens in chapter {c}.\n"
            f"Then it escalates in chapter {c}.\n\n## Scene 2\n\nA rival appears in chapter {c}.\n",
            encoding="utf-8",
        )
    cfg = load_config(series__workdir=str(ws), series__name="T", recap__style=style)
    return cfg, RecapBuilder(cfg, log=lambda *_: None)


def _codes(issues):
    return [i.code for i in issues]


# ---------------- style selection ----------------

def test_style_selection_resolves_all_three_voices(tmp_path):
    _, default = _workspace(tmp_path / "a", style="default")
    _, speedrun = _workspace(tmp_path / "b", style="speedrun")
    _, power = _workspace(tmp_path / "c", style="powerfantasy")
    assert default._system_prompt() is SCRIPT_SYSTEM
    assert speedrun._system_prompt() is SPEEDRUN_SYSTEM
    assert power._system_prompt() is POWERFANTASY_SYSTEM


def test_style_selection_is_case_insensitive_and_falls_back(tmp_path):
    _, power = _workspace(tmp_path / "a", style="PowerFantasy")
    _, unknown = _workspace(tmp_path / "b", style="not-a-style")
    assert power._system_prompt() is POWERFANTASY_SYSTEM
    assert unknown._system_prompt() is SCRIPT_SYSTEM


def test_system_template_keeps_the_users_core_verbatim():
    for phrase in (
        "DO NOT start with the main character's past, reincarnation, or childhood",
        "Start IN MEDIAS RES (in the middle of the action)",
        "This guy is [Name], a [low status role] at [prestigious place], but...",
        "But what none of them know is...",
        "Our boy",
        "the poor guy",
        "this absolute amateur",
        "casually destroys",
        "HYPE MAN",
        "Status window",
        "Plot armor",
        "Format the output entirely as a spoken voiceover script",
        "Do not include camera directions or visual notes",
    ):
        assert phrase in POWERFANTASY_SYSTEM
    # The pipeline adaptations are a separate, clearly marked tail.
    assert "Pipeline contract" in POWERFANTASY_SYSTEM


# ---------------- the range gate ----------------

def test_range_span_is_contiguous():
    assert _range_span(["2", "4"]) == ["2", "3", "4"]
    assert _range_span(["1"]) == ["1"]
    assert _range_span(["3", "1", "2"]) == ["1", "2", "3"]


def test_range_gate_refuses_until_the_whole_range_is_transcribed(tmp_path):
    _, b = _workspace(tmp_path, labels=("1", "2", "4"))
    with pytest.raises(UserError) as exc:
        b.prepare(["1", "2", "4"], 6)
    assert "Missing transcript(s) for chapter(s) 3" in str(exc.value)
    assert "range 1-4" in str(exc.value)
    with pytest.raises(UserError):
        b.generate(["1", "2", "4"], 6)


def test_range_gate_passes_with_a_complete_range(tmp_path):
    _, b = _workspace(tmp_path, labels=("1", "2", "3"))
    task_md = b.prepare(["1", "2", "3"], 6)
    assert Path(task_md).exists()
    assert sorted(p.name for p in (b.task_dir / "prompts").glob("ch*.md")) == [
        "ch1.md", "ch2.md", "ch3.md"]


def test_default_style_has_no_range_gate(tmp_path):
    _, b = _workspace(tmp_path, style="default", labels=("1", "3"))
    b.prepare(["1", "3"], 6)  # chapter 2 missing is fine for other styles
    assert sorted(p.name for p in (b.task_dir / "prompts").glob("ch*.md")) == [
        "ch1.md", "ch3.md"]


# ---------------- prompt assembly ----------------

def test_prompt_carries_range_context_full_transcript_and_framing(tmp_path):
    _, b = _workspace(tmp_path)
    prompt = b._prompt_for("2", 6, range_chapters=["1", "2", "3"])
    # Whole-range context: beat digests of the other chapters...
    assert "RANGE CONTEXT" in prompt and "chapters 1-3" in prompt
    assert "Something happens in chapter 1." in prompt   # ch1 digest
    assert "Something happens in chapter 3." in prompt   # ch3 digest
    # ...but digests only: ch1's second body line is not embedded whole.
    assert "Then it escalates in chapter 1." not in prompt
    # Chapter 2 itself rides as the full transcript.
    assert "Then it escalates in chapter 2." in prompt
    assert "A rival appears in chapter 2." in prompt
    # Framing: a later chapter opens mid-flow, never a lore re-drop.
    assert "CONTINUES the range (chapters 1-3)" in prompt
    assert "Open mid-flow" in prompt
    assert "TITLE-CARD CUES" in prompt
    assert "mid-escalation" in prompt


def test_prompt_for_the_opener_demands_the_full_structure(tmp_path):
    _, b = _workspace(tmp_path)
    prompt = b._prompt_for("1", 6, range_chapters=["1", "2", "3"])
    assert "OPENS the range (chapters 1-3)" in prompt
    assert "But what none of them know is" in prompt


def test_prompt_carries_the_previous_chapters_ending(tmp_path):
    _, b = _workspace(tmp_path)
    b._write_recap("1", OPENER, "hook one", 6, "Jin humiliates the champion.")
    prompt = b._prompt_for("2", 6, range_chapters=["1", "2", "3"])
    assert "THE END OF THE RECAP FOR CHAPTER 1" in prompt
    assert "first real hunter has just taken the bait" in prompt
    assert "Jin humiliates the champion." in prompt  # story-so-far summary


def test_prepare_task_carries_the_power_fantasy_system(tmp_path):
    _, b = _workspace(tmp_path)
    b.prepare(["1", "2", "3"], 6)
    task = (b.task_dir / "TASK.md").read_text(encoding="utf-8")
    assert "This guy is [Name]" in task
    prompt2 = (b.task_dir / "prompts" / "ch2.md").read_text(encoding="utf-8")
    assert "RANGE CONTEXT" in prompt2


def test_transcript_digest_keeps_headings_and_first_lines():
    digest = _transcript_digest(
        "# Ch 9\n\n## Arrival\n\nHe lands at dawn.\nMore detail here.\n\n## Duel\n\nBlades cross.\n")
    assert "Arrival — He lands at dawn." in digest
    assert "Duel — Blades cross." in digest
    assert "More detail here." not in digest


# ---------------- the script lint ----------------

def test_lint_passes_a_compliant_opener():
    assert lint_powerfantasy(OPENER, range_opener=True) == []


def test_lint_opener_requires_the_hook_formula():
    narration = OPENER.replace(
        "This guy is Jin, a mere cleaner at the Royal Sword Academy, but he just "
        "shattered the champion's blade with one casual swing.",
        "Jin steps up and wins the duel without breaking a sweat.")
    assert "hook-formula" in _codes(lint_powerfantasy(narration, range_opener=True))


def test_lint_opener_requires_the_lore_drop():
    narration = OPENER.replace(
        "But what none of them know is, our guy carries the soul of the strongest "
        "swordsman who ever lived.",
        "Our guy carries the soul of the strongest swordsman who ever lived.")
    assert "lore-drop" in _codes(lint_powerfantasy(narration, range_opener=True))


def test_lint_opener_must_not_lead_with_the_past_life():
    narration = "In his past life, Jin was the strongest swordsman alive. " + OPENER
    assert "past-life-opening" in _codes(lint_powerfantasy(narration, range_opener=True))


def test_lint_later_chapters_skip_hook_and_lore_but_keep_continuity():
    assert lint_powerfantasy(CONTINUATION, range_opener=False) == []
    codes = _codes(lint_powerfantasy(CONTINUATION, range_opener=True))
    assert "hook-formula" in codes and "lore-drop" in codes


def test_lint_bans_finale_endings_on_every_chapter():
    for ending in (" And so ends the tale of Jin.", " That's the end of this arc."):
        assert "finale-ending" in _codes(
            lint_powerfantasy(CONTINUATION + ending, range_opener=False))


def test_lint_bans_restart_openings_on_every_chapter():
    for opening in ("Welcome back, everyone. ", "Previously on this story. "):
        assert "restart-opening" in _codes(
            lint_powerfantasy(opening + CONTINUATION, range_opener=False))


def test_lint_bans_camera_directions():
    cued = CONTINUATION + "\n[Camera zooms in on his face]"
    assert "camera-cue" in _codes(lint_powerfantasy(cued, range_opener=False))
    spoken = "Cut to the arena floor. " + CONTINUATION
    assert "camera-direction" in _codes(lint_powerfantasy(spoken, range_opener=False))


def test_lint_allows_the_pipelines_own_cues():
    text = "[HOOK]\n" + CONTINUATION + "\n[LOOP: the crowd keeps chanting]"
    assert lint_powerfantasy(text, range_opener=False) == []


def test_lint_cue_forms_match_the_title_card_detector():
    for cue in ("Age four.", "Week six of possession.", "Age eight.",
                "Year four of possession.", "Three years later."):
        assert detect_title_cue(cue) is not None, cue
        narration = CONTINUATION + " " + cue
        assert "cue-form" not in _codes(lint_powerfantasy(narration, range_opener=False)), cue
    # A cue-shaped sentence the detector cannot parse is a lint failure.
    assert detect_title_cue("Age banana began the grind.") is None
    narration = CONTINUATION + " Age banana began the grind."
    assert "cue-form" in _codes(lint_powerfantasy(narration, range_opener=False))


def test_finalize_rejects_a_chapter_that_fails_the_lint(tmp_path):
    _, b = _workspace(tmp_path)
    b.prepare(["1", "2", "3"], 6)
    parts = {
        "1": {"narration": OPENER, "hook": "hook one", "summary": "s1"},
        "2": {"narration": CONTINUATION, "hook": "hook two", "summary": "s2"},
        "3": {"narration": CONTINUATION + " And so ends the tale of Jin.",
              "hook": "hook three", "summary": "s3"},
    }
    (b.task_dir / "parts.json").write_text(json.dumps(parts), encoding="utf-8")
    with pytest.raises(UserError) as exc:
        b.finalize()
    assert "finale-ending" in str(exc.value) or "finale language" in str(exc.value)
    assert set(b.recapped()) == {"1", "2"}  # the failing chapter was not saved


# ---------------- docs ----------------

def test_style_docs_exist():
    root = Path(__file__).resolve().parent.parent
    power = (root / "docs" / "RECAP_STYLE_POWERFANTASY.md").read_text(encoding="utf-8")
    for needle in ("recap.style: powerfantasy", "RANGE CONTEXT", "script_lint",
                   "finale-ending", "hook-formula"):
        assert needle in power
    speedrun = (root / "docs" / "RECAP_STYLE_SPEEDRUN.md").read_text(encoding="utf-8")
    assert "style: speedrun" in speedrun
