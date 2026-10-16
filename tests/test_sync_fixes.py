"""The sync fixes: `video sync-audit`, effective settings, and the plan meta.

The audit is a pure function over plan + alignment + cached whisper words, so
every cause it can report is exercised here with a hand-built two-shot plan -
no audio, no whisper, no rendering.
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.util import UserError  # noqa: E402
from manhwa_ocr.video import align_engine, sync, syncaudit  # noqa: E402
from manhwa_ocr.video.plan import build_plan  # noqa: E402


# ------------------------------------------------------------------ helpers


def _words(text: str, start=0.5, per=0.35):
    out, t = [], start
    for w in text.split():
        out.append({"w": w, "start": round(t, 3), "end": round(t + per * 0.8, 3)})
        t += per
    return out


def _shot(i: int, start: float, end: float, cards):
    return {"id": f"S{i:03d}", "start": round(start, 3), "end": round(end, 3), "layout": "single",
            "background": "blur", "exit": "cut", "effects": [], "sentence": None,
            "cards": [{"panel": pid, "crop": [0, 0, 1, 1], "enter": "cut", "at": at, "motion": "hold"}
                      for pid, at in cards]}


def _seg(n: int, start: float, end: float, text: str, tags, *, action="SHOW",
         selected=None, sequence=None):
    sel = selected if selected is not None else (tags[0] if tags else None)
    return {"sentence": n, "text": text, "start": float(start), "end": float(end), "tagged_panels": list(tags),
            "display_action": action, "selected_panel": sel, "selected_by_align": sel,
            "candidates": [], "confidence": "test",
            "sequence": list(sequence) if sequence is not None else ([sel] if sel else [])}


def _plan(shots, duration):
    return {"chapter": "1", "duration": duration, "shots": shots}


def _align(segs):
    return {"segments": segs}


def _run_audit(shots, segs, words, duration, **kw):
    return syncaudit.audit(_plan(shots, duration), _align(segs), words, **kw)


# ------------------------------------------------------------------ causes


def test_audit_flags_a_late_cut_as_min_hold_shift():
    # Sentence 1 is tagged P2 but the plan holds P1 for 4s first: the classic
    # min_hold retiming drift.
    segs = [_seg(1, 0.0, 10.0, "Ron draws his sword fast", ["P2"])]
    shots = [_shot(1, 0.0, 4.0, [("P1", 0.0)]), _shot(2, 4.0, 10.0, [("P2", 0.0)])]
    rep = _run_audit(shots, segs, _words("Ron draws his sword fast"), 10.0)
    assert len(rep["windows"]) == 1
    w = rep["windows"][0]
    assert w["cause"] == "min_hold_shift" and w["cut_offset"] == 4.0
    assert w["start"] == 0.0 and w["end"] == 4.0
    assert w["shown"] == "P1" and w["expected"] == ["P2"]
    assert rep["gate"]["pass"] is False and any("cut(s)" in f for f in rep["gate"]["failures"])


def test_audit_flags_filler_before_the_tagged_panel_as_filler_panel():
    # P9 was placed by coverage and is shown the whole sentence; the tagged P2
    # never appears. No late cut exists, so the filler is the cause.
    segs = [_seg(1, 0.0, 6.0, "The gate opens now", ["P2"], sequence=["P2", "P9"])]
    shots = [_shot(1, 0.0, 6.0, [("P9", 0.0)])]
    rep = _run_audit(shots, segs, _words("The gate opens now"), 6.0)
    assert [w["cause"] for w in rep["windows"]] == ["filler_panel"]
    assert rep["windows"][0]["shown"] == "P9"


def test_audit_flags_a_replaced_tag_as_tag_override():
    # The alignment engine selected P5 while the writer tagged P2; the plan
    # shows P5 on time. No lateness, no filler - a real tag override.
    segs = [_seg(1, 0.0, 6.0, "His sword breaks", ["P2"], selected="P5", sequence=["P5"])]
    shots = [_shot(1, 0.0, 6.0, [("P5", 0.0)])]
    rep = _run_audit(shots, segs, _words("His sword breaks"), 6.0)
    assert [w["cause"] for w in rep["windows"]] == ["tag_override"]


def test_audit_flags_a_mismatch_on_an_interpolated_sentence():
    # Sentence 2 has no matched whisper word at all (interpolated); the stale
    # plan still shows P1 while the sentence was tagged P2.
    segs = [_seg(1, 0.0, 5.0, "The tower stands tall", ["P1"]),
            _seg(2, 5.0, 10.0, "and the gate opens wide", ["P2"])]
    shots = [_shot(1, 0.0, 10.0, [("P1", 0.0)])]
    rep = _run_audit(shots, segs, _words("The tower stands tall"), 10.0)
    assert [w["cause"] for w in rep["windows"]] == ["interpolated_timing"]
    assert rep["interpolated"]["count"] == 1


def test_audit_flags_a_picture_held_past_max_hold_as_long_hold():
    # One 9.5s shot of P1 spans two sentences; sentence 2 (tagged P2) never
    # gets its own picture. Nothing is late; the hold itself is the cause.
    segs = [_seg(1, 0.0, 3.0, "One two three", ["P1"]),
            _seg(2, 3.0, 9.5, "Four five six", ["P2"])]
    words = _words("One two three four five six")
    shots = [_shot(1, 0.0, 9.5, [("P1", 0.0)])]
    rep = _run_audit(shots, segs, words, 9.5)
    assert [w["cause"] for w in rep["windows"]] == ["long_hold"]
    assert rep["windows"][0]["sentence"] == 2


def test_audit_falls_back_to_unknown_when_nothing_upstream_explains_it():
    segs = [_seg(1, 0.0, 6.0, "She looks away", ["P2"])]
    shots = [_shot(1, 0.0, 3.0, [("P7", 0.0)]), _shot(2, 3.0, 6.0, [("P7", 0.0)])]
    rep = _run_audit(shots, segs, _words("She looks away"), 6.0)
    assert [w["cause"] for w in rep["windows"]] == ["unknown"]


def test_audit_holds_are_judged_against_what_the_shot_legitimately_shows():
    # {same} sentence: no tags of its own. The previous sentence's sequence
    # included the coverage extra P9, so showing it during the hold is fine.
    segs = [_seg(1, 0.0, 4.0, "The gate opens now", ["P2"], sequence=["P2", "P9"]),
            _seg(2, 4.0, 9.0, "It creaks loudly", [], action="HOLD_PREVIOUS", selected=None, sequence=[])]
    shots = [_shot(1, 0.0, 4.0, [("P2", 0.0), ("P9", 2.0)]), _shot(2, 4.0, 9.0, [("P9", 0.0)])]
    rep = _run_audit(shots, segs, _words("The gate opens now it creaks loudly"), 9.0)
    assert rep["windows"] == []
    assert rep["strict_match"] == 1.0
    assert rep["wrong_picture"]["ratio"] == 0.0


def test_audit_ignores_text_only_tags():
    # A sentence tagged only with a caption panel can't be judged: the video
    # legitimately shows the nearest art instead.
    segs = [_seg(1, 0.0, 6.0, "Three days later", ["P0"])]
    shots = [_shot(1, 0.0, 6.0, [("P2", 0.0)])]
    rep = _run_audit(shots, segs, _words("Three days later"), 6.0, text_only=["P0"])
    assert rep["windows"] == [] and rep["judged_samples"] == 0


# ------------------------------------------------------------------ validity extras


def test_audit_reports_interpolated_runs_longer_than_five_seconds():
    segs = [_seg(1, 0.0, 2.0, "The road begins here", ["P1"])]
    for i in range(2, 8):     # 6 sentences x 2s = 12s with no matched words
        segs.append(_seg(i, (i - 1) * 2.0, i * 2.0, f"and it goes on number {i}", ["P1"]))
    shots = [_shot(1, 0.0, 14.0, [("P1", 0.0)])]
    rep = _run_audit(shots, segs, _words("The road begins here"), 14.0)
    assert rep["interpolated"]["count"] == 6
    assert len(rep["interpolated"]["long_runs"]) == 1
    run = rep["interpolated"]["long_runs"][0]
    assert run["first_sentence"] == 2 and run["last_sentence"] == 7 and run["seconds"] == 12.0


def test_audit_reports_leading_trailing_offsets_and_anchor_residuals():
    segs = [_seg(1, 0.0, 10.0, "Ron draws his sword fast", ["P2"])]
    words = _words("Ron draws his sword fast", start=2.0)
    shots = [_shot(1, 0.0, 10.0, [("P2", 0.0)])]
    rep = _run_audit(shots, segs, words, 11.0)
    assert rep["leading_offset"] == 2.0          # first word 2s after the first sentence starts
    assert rep["trailing_offset"] == round(float(shots[-1]["end"]) - words[-1]["end"], 3)
    res = rep["anchor_residuals"]
    assert res["count"] == 1
    assert res["worst"]["matched_word"] == 2.0
    assert res["worst"]["residual"] == -2.0      # sent to time from the anchor's back-off


def test_audit_compares_plan_length_with_audio_length():
    segs = [_seg(1, 0.0, 6.0, "Ron draws his sword fast", ["P2"])]
    shots = [_shot(1, 0.0, 6.0, [("P2", 0.0)])]
    long_plan = _run_audit(shots, segs, _words("Ron draws his sword fast"), 12.0,
                           audio={"duration": 6.0, "speech_end": 5.5})
    assert long_plan["audio_length_ok"] is False
    assert any("longer than the audio" in n for n in long_plan["length_notes"])
    short_plan = _run_audit(shots, segs, _words("Ron draws his sword fast", start=8.0,
                                                per=0.35), 8.0,
                            audio={"duration": 12.0, "speech_end": 11.5})
    assert any("before the last word" in n for n in short_plan["length_notes"])


def test_audit_marks_a_stale_timeline_in_its_summary():
    segs = [_seg(1, 0.0, 6.0, "Ron draws his sword fast", ["P2"])]
    shots = [_shot(1, 0.0, 6.0, [("P2", 0.0)])]
    stale = _run_audit(shots, segs, [], 6.0, timeline={
        "present": True, "fresh": False, "cached_audio": "aaaa", "current_audio": "bbbb",
        "model": "base.en", "configured_model": "small.en"})
    assert stale["settings"]["max_late"] == 0.3
    text = "\n".join(syncaudit.summary_lines(stale))
    assert "timeline: STALE" in text
    assert "'base.en'" in text and "'small.en'" in text


def test_audit_gate_reports_five_second_windows_over_the_wrong_picture_threshold():
    # One good sentence, then a broken stretch where sentence 1's panel keeps
    # playing under sentence 2: the gate must fail on the wrong-picture ratio
    # and on at least one five-second window.
    segs = [_seg(1, 0.0, 6.0, "Ron draws his sword fast", ["P2"]),
            _seg(2, 6.0, 12.0, "The demon king laughs loud", ["P3"])]
    shots = [_shot(1, 0.0, 12.0, [("P2", 0.0)])]
    rep = _run_audit(shots, segs, _words("Ron draws his sword fast the demon king laughs loud"), 12.0)
    assert rep["gate"]["pass"] is False
    assert rep["wrong_picture"]["ratio"] == 0.5
    assert rep["five_second"]["worst"] is not None
    assert rep["five_second"]["worst"]["wrong"] == 1.0
    assert rep["five_second"]["worst"]["start"] == 6.0


def test_audit_wrong_picture_excludes_legitimate_fillers():
    # The tagged P2 never appears; a coverage extra (owned by no sentence) is
    # shown instead. Strict match is 0% but no OTHER sentence's panel is on
    # screen, so wrong-picture time is 0 and the gate is not what fails.
    segs = [_seg(1, 0.0, 6.0, "The gate opens now", ["P2"], sequence=["P2", "P9"])]
    shots = [_shot(1, 0.0, 6.0, [("P9", 0.0)])]
    rep = _run_audit(shots, segs, _words("The gate opens now"), 6.0)
    assert rep["strict_match"] == 0.0
    assert rep["wrong_picture"] == {"samples": 0, "seconds": 0.0, "ratio": 0.0}
    assert rep["gate"]["pass"] is True
    assert [w["cause"] for w in rep["windows"]] == ["filler_panel"]


def test_audit_minute_curve_splits_match_and_wrong_by_minute():
    # Minute 0 is perfectly voiced; in minute 1 sentence 1's panel keeps
    # playing under sentence 2 - the cliff shows in minute 1 only.
    segs = [_seg(1, 0.0, 60.0, "one two three four", ["P1"]),
            _seg(2, 60.0, 120.0, "five six seven eight", ["P2"])]
    shots = [_shot(1, 0.0, 60.0, [("P1", 0.0)]), _shot(2, 60.0, 120.0, [("P1", 0.0)])]
    rep = _run_audit(shots, segs, _words("one two three four five six seven eight"), 120.0)
    mc = rep["minute_curve"]
    assert [(m["minute"], m["match"], m["wrong"]) for m in mc] == [
        (0, 1.0, 0.0), (1, 0.0, 1.0)]


def test_audit_lists_the_worst_stretches_by_length():
    # Mismatches of 3s, 6s and 4s: worst_windows is ordered by duration, the
    # full windows list stays in time order.
    segs = [_seg(1, 0.0, 8.0, "a", ["P9"]), _seg(2, 8.0, 16.0, "b", ["P9"])]
    shots = [_shot(1, 0.0, 3.0, [("P1", 0.0)]), _shot(2, 3.0, 4.0, [("P9", 0.0)]),
             _shot(3, 4.0, 10.0, [("P1", 0.0)]), _shot(4, 10.0, 11.0, [("P9", 0.0)]),
             _shot(5, 11.0, 15.0, [("P1", 0.0)]), _shot(6, 15.0, 16.0, [("P9", 0.0)])]
    rep = _run_audit(shots, segs, _words("a b"), 16.0)
    assert [w["duration"] for w in rep["windows"]] == [3.0, 6.0, 4.0]
    assert [w["duration"] for w in rep["worst_windows"]] == [6.0, 4.0, 3.0]
    assert [w["start"] for w in rep["worst_windows"]] == [4.0, 11.0, 0.0]


# ---------------------------------------------------- anchor timing provenance


def test_align_marks_matched_and_interpolated_sentences_and_their_runs():
    sentences = [{"text": "The road begins here"},
                 {"text": "and it goes on forever"},
                 {"text": "past the broken gate"},
                 {"text": "until the tower falls"}]
    words = _words("The road begins here", start=0.5) + _words("until the tower falls", start=9.0)
    aligned = sync.align(sentences, words, duration=12.0)
    sources = [s["timing_source"] for s in aligned["sentences"]]
    assert sources == ["matched", "interpolated", "interpolated", "matched"]
    assert aligned["unmatched"] == ["and it goes on forever", "past the broken gate"]
    assert len(aligned["unmatched_runs"]) == 1
    run = aligned["unmatched_runs"][0]
    assert run["first"] == 2 and run["last"] == 3
    assert run["end"] == 9.0                       # ends where sentence 4's matched word starts
    assert 0.0 < run["start"] < run["end"] and run["seconds"] == round(run["end"] - run["start"], 3)


def test_align_marks_everything_estimated_when_there_are_no_words():
    sentences = [{"text": "One two three"}, {"text": "four five six"}]
    aligned = sync.align(sentences, [], duration=6.0)
    assert [s["timing_source"] for s in aligned["sentences"]] == ["estimated", "estimated"]
    assert aligned["unmatched_runs"] == []
    assert sync.long_unmatched_run(aligned, 0.0) is None


def test_long_unmatched_run_finds_only_what_is_over_the_limit():
    aligned = {"unmatched_runs": [{"first": 2, "last": 3, "seconds": 2.0},
                                  {"first": 7, "last": 9, "seconds": 7.5}]}
    assert sync.long_unmatched_run(aligned, 6.0)["first"] == 7
    assert sync.long_unmatched_run(aligned, 8.0) is None
    assert sync.long_unmatched_run({}, 6.0) is None


def test_timed_sentences_refuses_a_run_longer_than_max_unmatched(tmp_path, monkeypatch):
    from manhwa_ocr.video import align_engine, ffmpeg

    # Preload build.py first: it binds ffmpeg.audio_timing at import time, and
    # without this, importing it while the patch below is active would freeze
    # the fake into build's namespace for every later test.
    from manhwa_ocr.video import build as _build  # noqa: F401

    root = tmp_path / "ws"
    (root / "audio").mkdir(parents=True)
    (root / "audio" / "chapter_1.wav").write_bytes(b"not real audio")
    monkeypatch.setattr(sync, "available", lambda: True)
    monkeypatch.setattr(sync, "transcribe", lambda *a, **k: _words("The road begins here", start=0.0))
    monkeypatch.setattr(ffmpeg, "audio_timing", lambda *a, **k: {"duration": 20.0, "speech_end": 20.0})

    sentences = [{"text": "The road begins here"}] + [
        {"text": f"and it goes on number {i}"} for i in range(2, 7)]

    with pytest.raises(UserError, match="sentences 2-6"):
        align_engine._timed_sentences(root, "1", sentences, {}, "auto", lambda *_: None,
                                      dict(sync.SYNC_DEFAULTS, max_unmatched=6.0))

    loosened = dict(sync.SYNC_DEFAULTS, max_unmatched=30.0)
    timed, mode, duration = align_engine._timed_sentences(root, "1", sentences, {}, "auto",
                                                          lambda *_: None, loosened)
    assert mode == "synced" and duration == 20.0
    assert [t["timing_source"] for t in timed] == ["matched"] + ["interpolated"] * 5


# ------------------------------------------------------------------ summary output


def test_summary_lines_name_everything_the_report_found():
    segs = [_seg(1, 0.0, 10.0, "Ron draws his sword fast", ["P2"])]
    shots = [_shot(1, 0.0, 4.0, [("P1", 0.0)]), _shot(2, 4.0, 10.0, [("P2", 0.0)])]
    rep = _run_audit(shots, segs, _words("Ron draws his sword fast"), 10.0)
    text = "\n".join(syncaudit.summary_lines(rep))
    assert "Chapter 1 sync audit" in text
    assert "strict match" in text and "wrong-picture time" in text
    assert "min_hold_shift" in text and "sentence 1" in text
    assert "worst stretches by length" in text
    assert "gate: FAIL" in text


# ------------------------------------------------------------------ settings


def test_effective_settings_reads_its_own_keys_and_warns_loudly_about_missing_ones():
    logs = []
    st = sync.effective_settings({"sync": {"max_late": 1.0}}, log=logs.append)
    assert st["max_late"] == 1.0
    assert st["min_hold"] == sync.SYNC_DEFAULTS["min_hold"]
    assert st["gate_window"] == sync.SYNC_DEFAULTS["gate_window"]
    text = "\n".join(logs)
    assert "video.min_hold" in text and "video.sync.max_late" not in text
    assert "video.sync.min_hold_short" in text


def test_effective_settings_is_silent_when_the_config_is_complete():
    logs = []
    st = sync.effective_settings(dict(load_config().video), log=logs.append)
    assert logs == []
    assert st["min_hold"] == 3.0 and st["max_late"] == 0.3 and st["lead"] == 0.2


def test_effective_settings_rejects_a_non_number():
    with pytest.raises(UserError):
        sync.effective_settings({"min_hold": "soon", "sync": {}}, log=lambda *_: None)


# ------------------------------------------------------------------ the plan meta


def test_build_plan_stores_the_effective_settings_in_its_meta():
    vcfg = dict(load_config().video)
    panels = [{"id": f"P001-0{i}", "width": 300, "height": 700 if i % 2 else 300, "text": ""}
              for i in range(1, 4)]

    def img_for(p):
        img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
        ImageDraw.Draw(img).line((0, 0, img.width, img.height), fill=(20, 20, 20))
        return img

    units = [{"start": 0.0, "end": 8.0, "panels": ["P001-01"], "cues": [], "emphasis": [],
              "sentence": "x", "sentence_starts": [0.0]}]
    plan = build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "x"}, duration=8.0,
                      audio=None, vcfg=vcfg, image_for=img_for, alignment_units=units)
    assert plan["sync_settings"]["min_hold"] == 3.0
    assert plan["sync_settings"]["max_late"] == 0.3
    assert plan["sync_settings"]["gate_wrong"] == sync.SYNC_DEFAULTS["gate_wrong"]


# ------------------------------------------------------------------ make_report plumbing


def test_make_report_writes_the_json_and_survives_missing_audio(tmp_path):
    root = tmp_path / "ws"
    (root / "video").mkdir(parents=True)
    segs = [_seg(1, 0.0, 6.0, "Ron draws his sword fast", ["P2"])]
    (root / "video" / "chapter_1.alignment.json").write_text(json.dumps({
        "segments": segs, "sync_mode": "synced", "coverage": {}, "validation": {"status": "PASS"},
        "review_queue": []}), encoding="utf-8")
    (root / "video" / "chapter_1.plan.json").write_text(json.dumps(
        _plan([_shot(1, 0.0, 6.0, [("P2", 0.0)])], 6.0)), encoding="utf-8")
    (root / "video" / "chapter_1.timeline.json").write_text(json.dumps({
        "key": {"audio": "abc123", "model": "base.en"}, "words": _words("Ron draws his sword fast")}),
        encoding="utf-8")

    rep = syncaudit.make_report(root, "1", load_config(), log=lambda *_: None)
    saved = json.loads((root / "video" / "chapter_1.syncaudit.json").read_text(encoding="utf-8"))
    assert saved["chapter"] == "1" and saved["gate"]["pass"] is True
    assert saved["timeline"]["fresh"] is False          # audio file is missing
    assert "not found" in saved["timeline"]["note"]
    assert saved["audio"] is None
    assert any("no narration audio found" in n for n in saved["length_notes"])


def test_make_report_checks_a_real_audio_file_for_staleness_and_length(tmp_path):
    root = tmp_path / "ws"
    audio_dir = root / "audio"
    video = root / "video"
    audio_dir.mkdir(parents=True)
    video.mkdir(parents=True)
    wav = audio_dir / "chapter_1.wav"
    with wave.open(str(wav), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(16000)
        fh.writeframes(b"\x00\x00" * 16000)             # 1 second of silence
    segs = [_seg(1, 0.0, 30.0, "Ron draws his sword fast", ["P2"])]
    (video / "chapter_1.alignment.json").write_text(json.dumps({
        "segments": segs, "sync_mode": "synced", "coverage": {}, "validation": {"status": "PASS"},
        "review_queue": []}), encoding="utf-8")
    (video / "chapter_1.plan.json").write_text(json.dumps(
        _plan([_shot(1, 0.0, 30.0, [("P2", 0.0)])], 30.0)), encoding="utf-8")
    (video / "chapter_1.timeline.json").write_text(json.dumps({
        "key": {"audio": sync.file_hash(wav), "model": "base.en"},
        "words": _words("Ron draws his sword fast")}), encoding="utf-8")

    rep = syncaudit.make_report(root, "1", load_config(), log=lambda *_: None)
    assert rep["timeline"]["fresh"] is True
    assert rep["audio"]["duration"] == 1.0
    assert rep["audio_length_ok"] is False
    assert any("longer than the audio" in n for n in rep["length_notes"])


# ------------------------------------------------------------------ FIX 4: tag trust


def _panel(pid: str, text: str = "") -> dict:
    return {"id": pid, "text": text, "text_only": False}


def test_strong_ocr_evidence_overrides_an_in_window_tag():
    """video.sync.override_evidence is the one bar another panel must clear to
    replace the writer's tag: overlap strictly above it means the panel's own
    OCR'd text really is what the narrator is reading out."""
    panels = [_panel("P001-01"), _panel("P001-02"),
              _panel("P001-03", text="Zarithium darkphantom overload cracked the seal")]
    sentences = [{"text": "He steps through the gate.", "panels": ["P001-01"]},
                 {"text": "The zarithium darkphantom overload triggered everywhere.",
                  "panels": ["P001-02"]}]
    seg = align_engine.align_sentences(sentences, panels)[1]
    assert seg["selected_panel"] == "P001-03"
    assert seg["confidence"] == "ocr_override_tag"
    assert seg["needs_review"] is True


def test_a_tag_survives_evidence_below_override_evidence():
    """The same overlap, with the knob raised above it: the tag stands. The bar
    is a setting, not a hidden constant."""
    panels = [_panel("P001-01"), _panel("P001-02"),
              _panel("P001-03", text="Zarithium darkphantom overload cracked the seal")]
    sentences = [{"text": "He steps through the gate.", "panels": ["P001-01"]},
                 {"text": "The zarithium darkphantom overload triggered everywhere.",
                  "panels": ["P001-02"]}]
    seg = align_engine.align_sentences(sentences, panels, override_evidence=30.0)[1]
    assert seg["selected_panel"] == "P001-02"
    assert seg["confidence"] == "tag_confirmed_in_window"
    assert seg["needs_review"] is False


# ------------------------------------------------------------------ FIX 4: coverage placement


def _coverage_panels(n: int):
    return [{"id": f"P001-0{i}", "text_only": False} for i in range(1, n + 1)]


def _show(sentence: int, selected: str, start: float, end: float) -> dict:
    return {"sentence": sentence, "selected_panel": selected, "display_action": "SHOW",
            "start": start, "end": end}


def test_coverage_fillers_follow_the_sentence_s_own_panel():
    """The sentence's own pick leads its time (that is what the narrator is
    talking about); coverage fillers follow in story order, even when they come
    before it in the story."""
    segs = [_show(1, "P001-04", 0.0, 12.0)]
    align_engine.place_coverage(segs, _coverage_panels(6), 12.0, 3.0)
    assert segs[0]["sequence"] == ["P001-04", "P001-01", "P001-02", "P001-03", "P001-05", "P001-06"]


def test_fillers_prefer_a_sentence_long_enough_to_share_min_hold():
    """filler_full: a filler goes to a sentence at least that long when one is
    near, so the sentence's own picture still holds a full min_hold beside it.
    P001-03 sits nearer to sentence 1, but only sentence 2 (9s) can be filled
    end to end, so that is where it goes."""
    segs = [_show(1, "P001-02", 0.0, 3.0), _show(2, "P001-05", 3.0, 12.0)]
    align_engine.place_coverage(segs, _coverage_panels(6), 12.0, 3.0)
    assert "P001-03" in segs[1]["sequence"]
    assert "P001-03" not in segs[0]["sequence"]


def test_a_filler_is_never_dropped_when_every_sentence_is_short():
    """When every nearby sentence is under filler_full, the nearest ones take
    the filler anyway - a panel the artist drew never disappears just because
    the narration was choppy."""
    segs = [_show(1, "P001-02", 0.0, 3.0), _show(2, "P001-03", 3.0, 6.0)]
    cov = align_engine.place_coverage(segs, _coverage_panels(3), 6.0, 3.0)
    assert cov["placed_by_order"] == ["P001-01"]
    assert "P001-01" in segs[0]["sequence"]
