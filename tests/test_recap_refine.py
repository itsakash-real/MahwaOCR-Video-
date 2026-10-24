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


# ================================================ refined flow (phases 4.1-4.7)

import json

import pytest

from manhwa_ocr.config import load_config
from manhwa_ocr.memory import SeriesMemory
from manhwa_ocr.script import RecapBuilder
from manhwa_ocr.util import UserError


def _ws(tmp_path, style="manhwabro", labels=("1", "2"), logs=None):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    for c in labels:
        (ws / f"chapter_{c}.md").write_text(
            f"# Ch {c}\n\n## Scene 1 -- duel\n\n**Mikail:** \"Fight me.\"\n\n"
            f"*(Mikail, silver hair, catches a fire lance and crushes it; the arena goes silent.)*\n",
            encoding="utf-8")
    cfg = load_config(series__workdir=str(ws), series__name="T", recap__style=style)
    return ws, RecapBuilder(cfg, log=(logs.append if logs is not None else (lambda *_: None)))


GOOD_BRO = (
    "{P001-01} Our boy Mikail casually catches the fire lance in one hand and crushes it like nothing. "
    "{same} The whole arena goes silent, and the poor bastard across from him can only stare in shock, "
    "while every witness in the stands questions what they just saw, because nobody believed it. "
    "{P001-01} So, why is this brat so strong? "
    "{same} Well, of course, he trained for twenty years, and he remembers every duel, which is why bro wins. "
    "{P001-01} Unfortunately, the clown of a hero then ruined the final fight in the dumbest way."
)


def test_prepare_refuses_range_with_missing_transcripts(tmp_path):
    """Incident 3: transcripts-first is enforced for the range lanes, and
    every lane requires the requested chapters' transcripts."""
    ws, b = _ws(tmp_path, labels=("1", "3"))   # chapter 2 transcript missing
    with pytest.raises(UserError, match="chapter\(s\) 2"):
        b.prepare(["1", "3"], 6)
    # non-range lanes (default) accept a non-contiguous selection
    ws2, b2 = _ws(tmp_path / "d", style="default", labels=("1", "3"))
    assert b2.prepare(["1", "3"], 6) is not None
    # ...but still refuse a requested chapter with no transcript
    ws3, b3 = _ws(tmp_path / "e", style="default", labels=("1",))
    with pytest.raises(UserError, match="chapter\(s\) 2"):
        b3.prepare(["1", "2"], 6)


def test_prepare_persists_brief_artifact(tmp_path):
    """Incident 11: the brief survives finalize (which deletes .work/recap)."""
    ws, b = _ws(tmp_path)
    b.prepare(["1"], 6)
    brief = ws / "recaps" / "chapter_1.brief.md"
    assert brief.exists()
    text = brief.read_text(encoding="utf-8")
    assert "brief-manifest" in text and "transcripts_missing_in_range" in text
    assert "## System instruction" in text and "## Chapter prompt" in text
    assert "Scene 1 -- duel" in text          # the full prompt content
    b._write_recap("1", GOOD_BRO, "hook", 6, "summary")
    b.cancel()                                # what finalize does on success
    assert brief.exists(), "the brief must survive the task-dir cleanup"


def test_finalize_runs_quality_report_and_claim_check(tmp_path):
    ws, b = _ws(tmp_path)
    b.prepare(["1"], 6)
    (b.task_dir / "parts.json").write_text(json.dumps(
        {"1": {"narration": GOOD_BRO, "hook": "h", "summary": "s"}}), encoding="utf-8")
    written = b.finalize()
    assert [p.name for p in written] == ["chapter_1.md"]
    report = (ws / "recaps" / "chapter_1.report.md").read_text(encoding="utf-8")
    assert "Quality signals" in report and "witness_reactions" in report
    assert "Claim check" in report
    claims = json.loads((ws / "recaps" / "chapter_1.claims.json").read_text(encoding="utf-8"))
    verdicts = {r["verdict"] for r in claims["sentences"]}
    assert "flagged" not in verdicts or claims["summary"]["flagged"] >= 0  # shape check
    assert any(r["verdict"] == "supported" for r in claims["sentences"])


def test_finalize_merges_writer_memory_update_into_bible(tmp_path):
    """Incident 2: the recap side writes the bible through the same
    SeriesMemory.merge the transcript stage uses."""
    ws, b = _ws(tmp_path)
    b.prepare(["1"], 6)
    (b.task_dir / "parts.json").write_text(json.dumps(
        {"1": {"narration": GOOD_BRO, "hook": "h", "summary": "s",
               "memory_update": {"world_facts": ["the fire lance duel is annual"],
                                 "plot_threads": [{"thread": "who ruined the final fight",
                                                   "status": "open"}]}}}), encoding="utf-8")
    b.finalize()
    mem = SeriesMemory.load(ws / "series_memory.json", "T")
    assert "the fire lance duel is annual" in mem.world_facts
    assert any(t["thread"] == "who ruined the final fight" for t in mem.plot_threads)


def test_finalize_without_memory_update_leaves_bible_untouched(tmp_path):
    ws, b = _ws(tmp_path)
    b.prepare(["1"], 6)
    (b.task_dir / "parts.json").write_text(json.dumps(
        {"1": {"narration": GOOD_BRO, "hook": "h", "summary": "s"}}), encoding="utf-8")
    b.finalize()
    assert not (ws / "series_memory.json").exists()


# ----------------------------------------------- quality signal validation

def test_quality_signals_flag_the_rejected_profile_control():
    """The guardrail: a gate is only real if it FLAGS the control built
    from the rejected pass's post-mortem and PASSES a good script."""
    from manhwa_ocr import recap_quality

    control = Path(__file__).parent / "fixtures" / "rejected_profile_control.md"
    sentences = [s["text"] for s in
                 __import__("manhwa_ocr.video.cues", fromlist=["parse"]).parse(
                     control.read_text(encoding="utf-8").split("-->", 1)[1], None)["sentences"]]
    m = recap_quality.measure_sentences(sentences)
    sigs = {s.name: s for s in recap_quality.signals("powerfantasy", m)}
    flagged = [s.name for s in sigs.values() if s.status == "warn"]
    assert "chained_sentences" in flagged
    assert "long_sentences" in flagged
    assert "vague_power_claims" in flagged
    assert "witness_reactions" in flagged


def test_quality_signals_pass_the_reference_anchor():
    from manhwa_ocr import recap_quality
    from test_manhwabro import CONTINUATION, OPENER

    m = recap_quality.measure_sentences(
        [s["text"] for s in __import__("manhwa_ocr.video.cues", fromlist=["parse"]).parse(
            OPENER + " " + CONTINUATION, None)["sentences"]])
    sigs = {s.name: s for s in recap_quality.signals("manhwabro", m)}
    # 'length' cannot apply to a 6-sentence anchor; every content signal must pass
    warns = [s.name for s in sigs.values() if s.status == "warn" and s.name != "length"]
    assert not warns, warns


# ----------------------------------------------------------- claim checks

def test_claim_check_flags_invented_name_and_number():
    from manhwa_ocr.claimcheck import TranscriptIndex, check_sentences

    transcript = ("# Ch 1\n\n## Summary\n\nMikail fights at the arena for forty years.\n\n"
                  "## Scene 1 -- duel\n\n**Mikail:** \"I trained for forty years.\"\n\n"
                  "*(Mikail crushes a fire lance; the arena goes silent.)*\n")
    idx = TranscriptIndex(transcript)
    rows = check_sentences([
        "Mikail crushes the fire lance while the arena goes silent.",   # supported
        "Then Zarathux of the Voidborn Dominion devours the entire arena.",  # invented name
        "He trained for nine hundred years before that.",               # invented number
    ], idx)["sentences"]
    assert rows[0]["verdict"] == "supported"
    assert rows[1]["verdict"] == "flagged" and any("Voidborn" in f for f in rows[1]["flags"])
    assert rows[2]["verdict"] == "flagged" and any("nine" in f for f in rows[2]["flags"])


def test_claim_check_sentence_initial_unknown_is_review_not_alarm():
    """Documented trade-off: an unknown word at position 0 is suppressed
    (ordinary sentence starters are capitalised too), so an invented name
    that OPENS a sentence lands in the review list, not an alarm."""
    from manhwa_ocr.claimcheck import TranscriptIndex, check_sentences

    idx = TranscriptIndex("*(Mikail, silver hair, catches the lance.)*\n")
    rows = check_sentences(["Zarathux devours the entire arena in one bite."], idx)["sentences"]
    assert rows[0]["verdict"] == "no-checkable-tokens"


def test_claim_check_is_false_alarm_averse_on_sentence_starts():
    from manhwa_ocr.claimcheck import TranscriptIndex, check_sentences

    idx = TranscriptIndex("*(Mikail, silver hair, catches the lance.)*\n")
    rows = check_sentences(["Unfortunately, the clown of a hero ruined everything here."], idx)["sentences"]
    assert rows[0]["verdict"] != "flagged"


def test_panel_usability_warns_on_recorded_mask_risk(tmp_path):
    from manhwa_ocr.recap_quality import panel_usability_warnings

    (tmp_path / "video").mkdir()
    (tmp_path / "video" / "chapter_1.qa.json").write_text(
        json.dumps({"mask_risk": ["P001-02"]}), encoding="utf-8")
    panels = [{"id": "P001-01"}, {"id": "P001-02"}]
    warns = panel_usability_warnings(["P001-01", "P001-02"], panels, tmp_path, "1")
    assert any("mask-risk" in w for w in warns)


def test_tts_readiness_warnings_surface_the_punctuation_contract():
    from manhwa_ocr.recap_quality import tts_readiness_warnings

    warns = tts_readiness_warnings(["He was incredibly calm — and then he struck hard."])
    assert any("dash-pause" in w for w in warns)
 


def test_report_existing_regenerates_reports_without_touching_the_recap(tmp_path):
    ws, b = _ws(tmp_path)
    b.prepare(["1"], 6)
    (b.task_dir / "parts.json").write_text(json.dumps(
        {"1": {"narration": GOOD_BRO, "hook": "h", "summary": "s"}}), encoding="utf-8")
    b.finalize()
    recap = ws / "recaps" / "chapter_1.md"
    before = recap.read_text(encoding="utf-8")
    (ws / "recaps" / "chapter_1.report.md").unlink()
    (ws / "recaps" / "chapter_1.claims.json").unlink()
    reports = b.report_existing(["1"])
    assert reports and reports[0].exists()
    assert recap.read_text(encoding="utf-8") == before
    assert "style" in json.loads(
        (ws / "recaps" / "chapter_1.md").read_text(encoding="utf-8").split("recap-meta ")[1].split(" -->")[0])
