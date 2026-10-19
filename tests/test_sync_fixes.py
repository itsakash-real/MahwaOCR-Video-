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
from manhwa_ocr.video import build as vb  # noqa: E402
from manhwa_ocr.video import align_engine, sync, syncaudit  # noqa: E402
from manhwa_ocr.video.plan import build_plan, validate  # noqa: E402


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


def test_gate_windows_judge_settled_time_only():
    # Six 6s sentences, every shot cut 0.25s late - inside each sentence's
    # max_late budget, by design. Five wrong-picture samples land on the
    # boundaries; the old gate counted them and failed on share alone (40% at
    # t=3). The gate now judges settled time only, so none of these on-budget
    # cuts fail a window - the honest wrong-picture count is still reported.
    # (Shots are 6s: a 1.5s-shot fixture would itself fail the 3.0s min-shot gate.)
    segs, shots = [], []
    for k in range(1, 7):
        a, b = 6.0 * (k - 1), 6.0 * k
        segs.append(_seg(k, a, b, f"sentence number {k}", [f"P{k:02d}"]))
        shots.append(_shot(k, a + 0.25, b + 0.25, [(f"P{k:02d}", 0.0)]))
    words = _words(" ".join(f"sentence number {k}" for k in range(1, 7)))
    rep = _run_audit(shots, segs, words, 36.25)
    assert rep["wrong_picture"]["samples"] == 5         # the honest number never hides
    assert rep["five_second"]["over_gate"] == []
    assert rep["windows"] == []
    assert not any("five-second window" in f for f in rep["gate"]["failures"])
    # The gate still fails — on the honest overall wrong-picture share
    # (5/72 = 6.9% > 5%), not on any window: the on-budget cuts are not drift.
    assert rep["gate"]["failures"] == [
        "wrong-picture time 2.5s (6.9% of judged) is over 5%"]


def test_gate_window_needs_a_full_second_of_wrong_picture():
    # A stray flash of sentence 2's panel sits in settled time (t=23.0): with a
    # lowered share bar that is 11% of the window - over the bar - but 0.5s is
    # under the full second of wrong picture a window needs to fail. A short
    # straddle must not fail the gate just because the window is sparse.
    # Under the 3.0s min-shot rule the 0.5s flash shot itself is illegal, so
    # the gate now fails on the short shot — but the WINDOW gate still does
    # not trip on 0.5s of wrong picture (over_gate stays []); the failure is
    # the short shot, not the window. (A 1.0s settled-wrong stretch tripping
    # the window gate with legal shots is covered in test_impactful_selection.)
    segs = [_seg(1, 0.0, 20.0, "Ron draws his sword fast", ["P2"]),
            _seg(2, 20.0, 22.0, "He raises the blade", ["P1"]),
            _seg(3, 22.0, 30.0, "The demon king laughs loud", ["P3"])]
    shots = [_shot(1, 0.0, 20.0, [("P2", 0.0)]), _shot(2, 20.0, 22.4, [("P1", 0.0)]),
             _shot(3, 22.4, 23.0, [("P3", 0.0)]), _shot(4, 23.0, 23.5, [("P1", 0.0)]),
             _shot(5, 23.5, 30.0, [("P3", 0.0)])]
    rep = _run_audit(shots, segs, _words("Ron draws his sword fast he raises the blade "
                                         "the demon king laughs loud"), 30.0,
                     settings={"gate_window": 0.05})
    assert rep["wrong_picture"]["samples"] == 2
    assert rep["five_second"]["over_gate"] == []
    assert rep["windows"] == []
    assert not any("five-second window" in f for f in rep["gate"]["failures"])
    assert rep["gate"]["pass"] is False
    assert any("minimum shot" in f for f in rep["gate"]["failures"])


def test_audit_wrong_picture_excludes_impact_passing_filler():
    # The tagged P2 never appears; an impact-PASSING filler (P9, verdict
    # recorded by place_impactful) is shown instead. Strict match is 0%
    # but the filler earned its seconds, so wrong-picture time is 0.
    # The gate still FAILS — on tagged coverage (0% < 80%), not on
    # wrong-picture. A filler that FAILS the impact test IS wrong-picture.
    segs = [_seg(1, 0.0, 6.0, "The gate opens now", ["P2"], sequence=["P2", "P9"])]
    segs[0]["filler_impact"] = {"P9": {"pass": True, "reason": "impactful"}}
    shots = [_shot(1, 0.0, 6.0, [("P9", 0.0)])]
    rep = _run_audit(shots, segs, _words("The gate opens now"), 6.0)
    assert rep["strict_match"] == 0.0
    assert rep["wrong_picture"] == {"samples": 0, "seconds": 0.0, "ratio": 0.0}
    assert rep["gate"]["pass"] is False
    assert any("tagged-coverage" in f for f in rep["gate"]["failures"])
    assert [w["cause"] for w in rep["windows"]] == ["filler_panel"]
    # Same plan, filler failing the impact test -> wrong-picture.
    segs[0]["filler_impact"] = {"P9": {"pass": False, "reason": "no scene note"}}
    rep_fail = _run_audit(shots, segs, _words("The gate opens now"), 6.0)
    assert rep_fail["wrong_picture"]["samples"] == 12
    assert rep_fail["gate"]["pass"] is False
    assert any("wrong-picture" in f for f in rep_fail["gate"]["failures"])


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
    # The deleted keys (min_hold_short, filler_full) are never required.
    assert "video.sync.min_hold_short" not in text and "video.sync.filler_full" not in text
    assert "min_hold_short" not in sync.SYNC_DEFAULTS and "filler_full" not in sync.SYNC_DEFAULTS


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


def test_a_sub_second_pair_keeps_its_second_card_visible():
    # Real case (ch1 "Flesh."): a one-word sentence carrying two tags. The pair
    # cannot reach the floor without drifting off the voice, so it stays honest
    # and short - but both cards must remain on screen and the plan must be
    # valid (the second card used to be scheduled at a negative time).
    vcfg = dict(load_config().video)
    panels = [{"id": f"P001-0{i}", "width": 300, "height": 300, "text": ""} for i in (1, 2)]

    def img_for(p):
        img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
        ImageDraw.Draw(img).line((0, 0, img.width, img.height), fill=(20, 20, 20))
        return img

    units = [{"start": 0.0, "end": 0.25, "panels": ["P001-01"], "cues": [], "emphasis": [],
              "sentence": "Flesh.", "sentence_starts": [0.0]},
             {"start": 0.25, "end": 0.5, "panels": ["P001-02"], "cues": [], "emphasis": [],
              "sentence": "Jack says it plain.", "sentence_starts": [0.25]}]
    plan = build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "x"}, duration=0.5,
                      audio=None, vcfg=vcfg, image_for=img_for, alignment_units=units)
    assert validate(plan, {"P001-01", "P001-02"}) == []
    shot = plan["shots"][0]
    assert len(shot["cards"]) == 2
    at = shot["cards"][1]["at"]
    assert 0.0 <= at <= (shot["end"] - shot["start"]) - 0.2 + 1e-9
    assert plan["timing"]["short_shots"] == 1


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


def test_strong_ocr_evidence_flags_the_disagreement_but_the_tag_still_shows():
    """The writer tagged this sentence while looking at the panels, and the
    field chapters settled it: overlap above video.sync.override_evidence is
    real evidence against the tag (usually a nearby bubble the vision model
    filed under the wrong panel), but replacing the tag with it showed the
    wrong picture every time - the recording matches the tag. The tag shows,
    the disagreement is flagged and recorded, never silently shown over it."""
    panels = [_panel("P001-01"), _panel("P001-02"),
              _panel("P001-03", text="Zarithium darkphantom overload cracked the seal")]
    sentences = [{"text": "He steps through the gate.", "panels": ["P001-01"]},
                 {"text": "The zarithium darkphantom overload triggered everywhere.",
                  "panels": ["P001-02"]}]
    seg = align_engine.align_sentences(sentences, panels)[1]
    assert seg["selected_panel"] == "P001-02"
    assert seg["confidence"] == "tag_confirmed_in_window"
    assert seg["needs_review"] is True
    assert seg["candidates"][0]["panel"] == "P001-03"


def test_a_tag_survives_evidence_below_override_evidence():
    """The same overlap, with the knob raised above it: the tag stands and
    nothing is flagged. The flag bar is a setting, not a hidden constant."""
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


def test_impactful_filler_keeps_tagged_panel_majority():
    """A 12s sentence splits into tagged (~65%) + ONE impactful filler
    (~35%) — the tagged panel keeps the majority; fillers never fill a
    sentence end to end anymore."""
    segs = [_show(1, "P001-04", 0.0, 12.0)]
    panels = [{"id": f"P001-{i:02d}", "text_only": False, "figure": 0.5,
               "scene": f"beat {i}"} for i in range(1, 7)]
    align_engine.place_impactful(segs, panels, 12.0, 3.0)
    assert segs[0]["sequence"] == ["P001-04", "P001-05"]
    assert segs[0]["tagged_coverage"] == pytest.approx(0.65)


def test_fillers_go_only_to_sentences_long_enough_to_hold_them():
    """A filler goes only to a sentence >8s with room for it; a 3s sentence
    holds its own panel alone, and a panel no sentence picked is simply not
    shown (no coverage pass rescues it)."""
    segs = [_show(1, "P001-02", 0.0, 3.0), _show(2, "P001-05", 3.0, 12.0)]
    panels = _coverage_panels(6)
    panels[5]["figure"] = 0.5                      # P001-06: face + beat
    panels[5]["scene"] = "A gate opens behind them"
    align_engine.place_impactful(segs, panels, 12.0, 3.0)
    assert segs[0]["sequence"] == ["P001-02"]
    assert segs[1]["sequence"] == ["P001-05", "P001-06"]


def test_an_unpicked_panel_is_never_shown_when_every_sentence_is_short():
    """When every sentence is short, nothing is placed anywhere: a panel the
    artist drew but no sentence picked simply never appears."""
    segs = [_show(1, "P001-02", 0.0, 3.0), _show(2, "P001-03", 3.0, 6.0)]
    cov = align_engine.place_impactful(segs, _coverage_panels(3), 6.0, 3.0)
    assert cov["placed_by_order"] == []
    assert segs[0]["sequence"] == ["P001-02"]
    assert segs[1]["sequence"] == ["P001-03"]


# ------------------------------------------------------------------ FIX 5: the render sync gate


class _Sess:
    def __init__(self, path):
        self.path, self.slug = path, "ws"


def _gate_workspace(tmp_path, *, shown: str):
    """Two sentences (P001-01 then P001-02); the first shot shows `shown`.
    `shown` = P001-01 is a clean plan; P001-02 shows the other sentence's
    picture over sentence 1, which is exactly what the gate is about."""
    root = tmp_path / "ws"
    (root / "video").mkdir(parents=True)
    (root / "media" / "ch1").mkdir(parents=True)
    (root / "audio").mkdir()
    wav = root / "audio" / "chapter_1.wav"
    with wave.open(str(wav), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(16000)
        fh.writeframes(b"\x00\x00" * (16000 * 8))
    (root / "media" / "ch1" / "panels.json").write_text(json.dumps({
        "panels": [{"id": p, "width": 300, "height": 300, "text": "", "text_only": False}
                   for p in ("P001-01", "P001-02")],
        "norm_width": 900, "fingerprint": "x"}), encoding="utf-8")
    (root / "video" / "chapter_1.alignment.json").write_text(json.dumps({
        "segments": [_seg(1, 0.0, 4.0, "Ron draws his sword fast", ["P001-01"]),
                     _seg(2, 4.0, 8.0, "He blocks the blow", ["P001-02"])],
        "sync_mode": "synced", "coverage": {}, "validation": {"status": "PASS"},
        "review_queue": []}), encoding="utf-8")
    (root / "video" / "chapter_1.plan.json").write_text(json.dumps({
        "version": 2, "chapter": "1", "duration": 8.0, "sync": "synced",
        "format": "16:9", "fps": 30,
        "shots": [_shot(1, 0.0, 4.0, [(shown, 0.0)]), _shot(2, 4.0, 8.0, [("P001-02", 0.0)])]}),
        encoding="utf-8")
    (root / "video" / "chapter_1.timeline.json").write_text(json.dumps({
        "key": {"audio": sync.file_hash(wav), "model": "base.en"},
        "words": _words("Ron draws his sword fast he blocks the blow")}), encoding="utf-8")
    return root, _Sess(root), load_config()


def _stub_render(monkeypatch):
    calls = []
    monkeypatch.setattr(vb, "render",
                        lambda *a, **k: calls.append(a[2]) or (a[0] / "video" / "chapter_x.mp4"))
    return calls


def test_render_sync_gate_passes_a_clean_plan(monkeypatch, tmp_path):
    root, sess, cfg = _gate_workspace(tmp_path, shown="P001-01")
    calls = _stub_render(monkeypatch)
    logs = []
    vb.render_chapter(sess, "1", cfg, log=logs.append)
    assert len(calls) == 1
    assert any("sync gate: PASS" in line for line in logs)
    assert json.loads((root / "video" / "chapter_1.syncaudit.json").read_text(encoding="utf-8"))["gate"]["pass"]


def test_render_sync_gate_stops_a_wrong_picture_plan(monkeypatch, tmp_path):
    root, sess, cfg = _gate_workspace(tmp_path, shown="P001-02")
    calls = _stub_render(monkeypatch)
    with pytest.raises(UserError, match="sync gate"):
        vb.render_chapter(sess, "1", cfg, log=lambda *_: None)
    assert calls == []                      # nothing was rendered
    saved = json.loads((root / "video" / "chapter_1.syncaudit.json").read_text(encoding="utf-8"))
    assert saved["gate"]["pass"] is False   # the report is there to inspect


def test_render_sync_gate_can_be_forced(monkeypatch, tmp_path):
    root, sess, cfg = _gate_workspace(tmp_path, shown="P001-02")
    calls = _stub_render(monkeypatch)
    logs = []
    vb.render_chapter(sess, "1", cfg, allow_sync_fail=True, log=logs.append)
    assert len(calls) == 1
    assert any("sync gate failed" in line for line in logs)


def test_render_sync_gate_does_not_apply_to_estimated_plans(monkeypatch, tmp_path):
    root, sess, cfg = _gate_workspace(tmp_path, shown="P001-02")
    plan_file = root / "video" / "chapter_1.plan.json"
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    plan["sync"] = "estimated"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    calls = _stub_render(monkeypatch)
    vb.render_chapter(sess, "1", cfg, allow_estimated=True, log=lambda *_: None)
    assert len(calls) == 1                  # rendered despite the wrong picture: no gate without real timing
    assert not (root / "video" / "chapter_1.syncaudit.json").exists()


def test_chapter_status_reports_the_saved_sync_gate(tmp_path):
    root, sess, cfg = _gate_workspace(tmp_path, shown="P001-02")
    assert vb.chapter_status(root, "1")["sync_audit"] is None
    syncaudit.make_report(root, "1", cfg, log=lambda *_: None)
    st = vb.chapter_status(root, "1")
    assert st["sync_audit"]["pass"] is False and st["sync_audit"]["fresh"] is True
    plan_file = root / "video" / "chapter_1.plan.json"
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    plan["generated_hash"] = "rebuilt"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    assert vb.chapter_status(root, "1")["sync_audit"]["fresh"] is False
