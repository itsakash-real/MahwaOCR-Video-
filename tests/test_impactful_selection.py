"""Impactful-only panel selection (Phase 1, 2026-10-19): the pacing rules
the user approved — max shots, min 3s shot, filler cap, duplicate-tag
HOLD, coverage gate, impact test, pair-arrival fix, QA info-only."""

import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.video import align_engine, syncaudit
from manhwa_ocr.video.build import quality_report
from manhwa_ocr.video.plan import build_plan, hold_groups
from manhwa_ocr.video import sync as sync_mod


# ------------------------------------------------------------------ helpers

def _panel(pid, idx, *, figure=0.5, scene="Ron draws his sword", text=""):
    return {"id": pid, "index": idx, "text_only": False, "figure": figure,
            "scene": scene, "text": text, "width": 300, "height": 300}


def _seg(n, start, end, tags, *, selected=None, sequence=None):
    sel = selected if selected is not None else (tags[0] if tags else None)
    return {"sentence": n, "text": f"sentence {n}", "start": float(start), "end": float(end),
            "tagged_panels": list(tags), "display_action": "SHOW",
            "selected_panel": sel, "selected_by_align": sel,
            "sequence": list(sequence) if sequence is not None else ([sel] if sel else [])}


def _shot(i, start, end, cards):
    return {"id": f"S{i:03d}", "start": start, "end": end, "layout": "single",
            "background": "blur", "exit": "cut", "effects": [], "sentence": None,
            "cards": [{"panel": pid, "crop": [0, 0, 1, 1], "enter": "cut", "at": at,
                       "motion": "hold"} for pid, at in cards]}


def _words(text, start=0.5, per=0.35):
    out, t = [], start
    for w in text.split():
        out.append({"w": w, "start": round(t, 3), "end": round(t + per * 0.8, 3)})
        t += per
    return out


def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 9):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


def _settings():
    return sync_mod.effective_settings({}, log=lambda _m: None)


# ------------------------------------------------------------------ Rule 1: max shots

def test_max_shots_for_duration():
    # Max shots = floor(audio_seconds / 3).
    assert align_engine.max_shots_for_duration(228.0) == 76
    assert align_engine.max_shots_for_duration(228.10) == 76
    assert align_engine.max_shots_for_duration(60.0) == 20
    assert align_engine.max_shots_for_duration(9.0) == 3
    assert align_engine.max_shots_for_duration(2.9) == 0
    assert align_engine.max_shots_for_duration(0.0) == 0


# ------------------------------------------------------------------ Rule 2: min shot 3.0s

def test_hold_groups_counts_shots_under_three_seconds_instead_of_hiding_them():
    # Dense narration: two 1s units pair into a 2.3s shot (nudged to the cut
    # budget). There is no shorter floor anymore, so it is counted — and the
    # sync gate fails on it — never silently stretched to look legal.
    units = [{"start": 0.0, "end": 1.0, "panels": ["A"], "cues": [], "emphasis": [],
              "sentence": "a", "sentence_starts": [0.0]},
             {"start": 1.0, "end": 2.0, "panels": ["B"], "cues": [], "emphasis": [],
              "sentence": "b", "sentence_starts": [1.0]},
             {"start": 2.0, "end": 9.0, "panels": ["C"], "cues": [], "emphasis": [],
              "sentence": "c", "sentence_starts": [2.0]}]
    shots, stats = hold_groups(units, 3.0, lead=0.2, max_late=0.3)
    assert shots[0]["end"] - shots[0]["start"] == pytest.approx(2.3)
    assert stats["short_shots"] == 1
    assert stats["short_floor"] == 3.0


def test_syncaudit_fails_any_shot_under_three_seconds():
    segs = [_seg(1, 0.0, 6.0, ["P1"])]
    shots = [_shot(1, 0.0, 2.5, [("P1", 0.0)]), _shot(2, 2.5, 6.0, [("P1", 0.0)])]
    rep = syncaudit.audit({"chapter": "1", "duration": 6.0, "shots": shots},
                          {"segments": segs}, _words("sentence one"))
    assert rep["short_shots"] == [{"shot": "S001", "duration": 2.5}]
    assert rep["gate"]["pass"] is False
    assert any("minimum shot" in f for f in rep["gate"]["failures"])


# ------------------------------------------------------------------ Rules 3-4: one tagged panel, split rules, filler cap

def test_short_sentence_shows_only_its_tagged_panel():
    panels = [_panel("P001-01", 0), _panel("P001-02", 1), _panel("P001-03", 2)]
    segs = [{"index": 0, "start": 0.0, "end": 6.0, "text": "sentence one",
             "tagged_panels": ["P001-01"], "sentence": 1, "display_action": "SHOW",
             "selected_panel": "P001-01", "panels": ["P001-01"]}]
    align_engine.place_impactful(segs, panels, 6.0, 3.0)
    assert segs[0]["sequence"] == ["P001-01"]
    assert segs[0]["sequence_shares"] == [1.0]
    assert segs[0]["tagged_coverage"] == 1.0


def test_long_sentence_split_is_tagged_plus_one_filler():
    panels = [_panel("P001-01", 0), _panel("P001-02", 1), _panel("P001-03", 2)]
    segs = [{"index": 0, "start": 0.0, "end": 12.0, "text": "sentence one",
             "tagged_panels": ["P001-01"], "sentence": 1, "display_action": "SHOW",
             "selected_panel": "P001-01", "panels": ["P001-01"]}]
    cov = align_engine.place_impactful(segs, panels, 12.0, 3.0)
    assert segs[0]["sequence"] == ["P001-01", "P001-02"]
    assert segs[0]["sequence_shares"] == pytest.approx([0.65, 0.35])
    assert segs[0]["split_pieces"] == 2
    assert cov["filler_shots"] == 1


def test_very_long_sentence_caps_at_three_shots_and_two_fillers():
    panels = [_panel(f"P001-0{i}", i - 1) for i in range(1, 6)]
    segs = [{"index": 0, "start": 0.0, "end": 21.0, "text": "sentence one",
             "tagged_panels": ["P001-01"], "sentence": 1, "display_action": "SHOW",
             "selected_panel": "P001-01", "panels": ["P001-01"]}]
    cov = align_engine.place_impactful(segs, panels, 21.0, 3.0)
    assert segs[0]["sequence"] == ["P001-01", "P001-02", "P001-03"]
    assert segs[0]["split_pieces"] == 3
    assert segs[0]["tagged_coverage"] == pytest.approx(0.60)
    assert cov["filler_shots"] == 2
    # Every piece is >= 3s: tagged 12.6s, fillers 4.2s each.
    assert all(s * 21.0 >= 3.0 for s in segs[0]["sequence_shares"])


def test_no_passing_filler_means_tagged_holds():
    # The only candidate after the tag has no scene note and no figure:
    # it fails the impact test, so the tagged panel holds the whole window.
    panels = [_panel("P001-01", 0),
              {"id": "P001-02", "index": 1, "text_only": False, "figure": 0.0,
               "scene": "", "text": "", "width": 300, "height": 300}]
    segs = [{"index": 0, "start": 0.0, "end": 12.0, "text": "sentence one",
             "tagged_panels": ["P001-01"], "sentence": 1, "display_action": "SHOW",
             "selected_panel": "P001-01", "panels": ["P001-01"]}]
    align_engine.place_impactful(segs, panels, 12.0, 3.0)
    assert segs[0]["sequence"] == ["P001-01"]
    assert segs[0]["tagged_coverage"] == 1.0


# ------------------------------------------------------------------ Rule 5: the impact test

def test_filler_impact_rejects_text_only_sfx_backwards_repeat_spoiler():
    tagged = _panel("P001-03", 2)
    panels = [
        _panel("P001-01", 0),                                   # before tag: backwards
        {"id": "P001-02", "index": 1, "text_only": True, "figure": 0.5,
         "scene": "A gate opens", "text": "", "width": 300, "height": 300},  # text-only
        tagged,
        {"id": "P001-04", "index": 3, "text_only": False, "figure": 0.5,
         "scene": "", "text": "BOOM!!", "width": 300, "height": 300},     # SFX-only (no beat)
        _panel("P001-05", 4),                                   # good
        _panel("P001-06", 5, scene="The demon king laughs"),    # next sentence's tag: spoiler
    ]
    order = {p["id"]: p["index"] for p in panels}
    kw = dict(sentence_text="sentence one", order=order, tagged_order=2,
              shown={tagged["id"]}, future_tagged={"P001-06"})
    assert align_engine.filler_impact(panels[0], **kw)[0] is False
    assert align_engine.filler_impact(panels[1], **kw)[0] is False
    assert align_engine.filler_impact(panels[3], **kw)[0] is False
    assert align_engine.filler_impact(panels[5], **kw)[0] is False
    assert align_engine.filler_impact(panels[4], **kw)[0] is True
    # A repeat (the tagged panel itself) is never a filler.
    assert align_engine.filler_impact(tagged, **kw)[0] is False


# ------------------------------------------------------------------ Rule 6: duplicate-tag HOLD

def test_duplicate_tag_sentence_holds_instead_of_triggering_filler():
    panels = [_panel("P001-01", 0), _panel("P001-02", 1), _panel("P001-03", 2)]
    sentences = [{"text": "Ron draws his sword.", "panels": ["P001-01"]},
                 {"text": "He raises it high.", "panels": ["P001-01"]},
                 {"text": "The demon king laughs.", "panels": ["P001-02"]}]
    segs = align_engine.align_sentences(sentences, panels)
    assert segs[0]["display_action"] == "SHOW"
    assert segs[1]["display_action"] == "HOLD_PREVIOUS"
    assert segs[1]["confidence"] == "rule:duplicate_tag_hold"
    assert segs[1]["needs_review"] is False
    # The hold's own tag is NOT consumed as a filler for anyone.
    assert segs[2]["display_action"] == "SHOW"
    assert segs[2]["selected_panel"] == "P001-02"


def test_untagged_sentences_do_not_trigger_duplicate_tag_hold():
    # Regression: the duplicate-tag HOLD fires ONLY on an explicit same-panel
    # tag. Untagged sentences re-show the cursor panel (no HOLD/SHOW
    # oscillation), and extras never advance the cursor.
    panels = [_panel(f"P001-{i:02d}", i - 1) for i in range(1, 6)]
    sentences = [{"text": "Sentence one.", "panels": ["P001-01"]},
                 {"text": "Sentence two.", "panels": ["P001-02", "P001-03"]},
                 {"text": "Sentence three.", "panels": []},
                 {"text": "Sentence four.", "panels": []}]
    segs = align_engine.align_sentences(sentences, panels)
    assert segs[1]["selected_panel"] == "P001-02"
    assert segs[1]["tagged_extras"] == ["P001-03"]
    assert all(s["display_action"] == "SHOW" for s in segs[2:])
    assert all(s["selected_panel"] == "P001-02" for s in segs[2:])
    assert all(s["confidence"] == "no_evidence_default_sequence" for s in segs[2:])


# ------------------------------------------------------------------ Rule 7: tagged coverage gate

def test_coverage_gate_fails_a_sentence_under_its_threshold():
    # 8s sentence, tagged panel on screen only half the window (50% < 80%).
    segs = [_seg(1, 0.0, 8.0, ["P1"], sequence=["P1", "P9"])]
    segs[0]["filler_impact"] = {"P9": {"pass": True, "reason": "impactful"}}
    shots = [_shot(1, 0.0, 4.0, [("P1", 0.0)]), _shot(2, 4.0, 8.0, [("P9", 0.0)])]
    rep = syncaudit.audit({"chapter": "1", "duration": 8.0, "shots": shots},
                          {"segments": segs}, _words("sentence one"))
    assert rep["tagged_coverage"] == [{"sentence": 1, "coverage": 0.5, "threshold": 0.8,
                                        "duration": 8.0, "pass": False}]
    assert rep["gate"]["pass"] is False
    assert any("tagged-coverage" in f for f in rep["gate"]["failures"])


def test_coverage_gate_passes_a_long_sentence_at_sixty_percent():
    # 10s sentence, tagged panel 6s of 10s (60% >= 60% for >8s).
    segs = [_seg(1, 0.0, 10.0, ["P1"], sequence=["P1", "P9"])]
    segs[0]["filler_impact"] = {"P9": {"pass": True, "reason": "impactful"}}
    shots = [_shot(1, 0.0, 6.0, [("P1", 0.0)]), _shot(2, 6.0, 10.0, [("P9", 0.0)])]
    rep = syncaudit.audit({"chapter": "1", "duration": 10.0, "shots": shots},
                          {"segments": segs}, _words("sentence one"))
    assert rep["tagged_coverage"][0]["pass"] is True
    assert rep["gate"]["pass"] is True


def test_gate_window_trips_on_a_full_second_of_settled_wrong_picture():
    # Legal shots throughout (all >= 3s, cuts on time). Sentence 2's panel
    # sits 1.5s into sentence 3's window — 1.0s of it in settled time, so
    # the window gate trips.
    segs = [_seg(1, 0.0, 20.0, ["P2"]),
            _seg(2, 20.0, 26.0, ["P1"]),
            _seg(3, 26.0, 34.0, ["P3"])]
    shots = [_shot(1, 0.0, 20.0, [("P2", 0.0)]),
             _shot(2, 20.0, 27.5, [("P1", 0.0)]),
             _shot(3, 27.5, 34.0, [("P3", 0.0)])]
    rep = syncaudit.audit({"chapter": "1", "duration": 34.0, "shots": shots},
                          {"segments": segs},
                          _words("sentence one sentence two sentence three"),
                          settings={"gate_window": 0.05})
    # Sentence 3's coverage is 13/16 = 81% (passes 80%), so the failure
    # that must appear is the window failure, not coverage.
    assert rep["five_second"]["over_gate"] != []
    assert any("five-second window" in f for f in rep["gate"]["failures"])


# ------------------------------------------------------------------ Rule 8: failing filler is wrong-picture

def test_failing_impact_filler_counts_as_wrong_picture():
    segs = [_seg(1, 0.0, 6.0, ["P2"], sequence=["P2", "P9"])]
    segs[0]["filler_impact"] = {"P9": {"pass": False, "reason": "no scene note"}}
    shots = [_shot(1, 0.0, 6.0, [("P9", 0.0)])]
    rep = syncaudit.audit({"chapter": "1", "duration": 6.0, "shots": shots},
                          {"segments": segs}, _words("sentence one"))
    assert rep["wrong_picture"]["samples"] == 12
    assert any("wrong-picture" in f for f in rep["gate"]["failures"])


# ------------------------------------------------------------------ Pair-arrival fix

def test_pair_second_card_arrives_on_its_own_words():
    # Ch1 boundary pair: sentence 1 is 0.06s, sentence 2 starts at 0.06.
    # The old 0.6s floor made the second card 0.66s late (past the 0.5s
    # gate); now it arrives exactly at second_at = 0.06.
    panels = [{"id": "P001-01", "width": 300, "height": 300, "text": ""},
              {"id": "P001-02", "width": 300, "height": 300, "text": ""}]
    units = [{"start": 0.0, "end": 0.06, "panels": ["P001-01"], "cues": [],
              "emphasis": [], "sentence": "a", "sentence_starts": [0.0]},
             {"start": 0.06, "end": 6.0, "panels": ["P001-02"], "cues": [],
              "emphasis": [], "sentence": "b", "sentence_starts": [0.06]}]
    plan = build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "x"},
                      duration=6.0, audio=None, vcfg={}, image_for=_img_for,
                      alignment_units=units, settings=_settings())
    pairs = [s for s in plan["shots"] if s["layout"] == "pair"]
    assert pairs, "the boundary pair must stay a pair shot"
    second = pairs[0]["cards"][1]
    assert second["panel"] == "P001-02"
    assert second["at"] == pytest.approx(0.06)
    assert second["at"] < 0.5  # inside the gate, not the old 0.6s floor


# ------------------------------------------------------------------ Rule 9: QA info-only

def test_quality_report_panels_shown_is_info_only():
    panels = {"panels": [{"id": "A", "width": 300, "height": 300, "text_only": False},
                         {"id": "B", "width": 300, "height": 300, "text_only": False}]}
    plan = {"format": "16:9", "fps": 30, "chapter": "1", "duration": 9.0, "shots": [
        {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single",
         "cards": [{"panel": "A", "crop": [0, 0, 1, 1]}]}]}
    qa = quality_report(plan, panels, None, 3.0)
    assert qa["panels_shown"] == 1 and qa["panels_total"] == 2
    assert qa["panels_shown_ratio"] == 0.5
    assert qa["max_shots"] == 3          # floor(9 / 3)
    assert qa["min_shot"] == 4.0
    assert qa["failures"] == []          # shown=1/2 is NOT a failure


def test_place_coverage_is_deleted():
    assert not hasattr(align_engine, "place_coverage")
    assert hasattr(align_engine, "place_impactful")
