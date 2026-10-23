"""Title Card Generator (video/titlecards.py + plan.title_card_overlays):
timeline cues in the narration become split-flap card OVERLAYS drawn
over the running shot (analysis fix 3 — the timeline and the master
audio are never cut for a card), measured from
the reference video — black field, serif label, two beige flap tiles
(33% of frame height), one Anton ALL-CAPS line. Detection must
reproduce the reference's three cards exactly and must NOT fire on
ordinary sentences that merely mention numbers or time.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.config import load_config
from manhwa_ocr.video import plan as plan_mod
from manhwa_ocr.video import syncaudit, titlecards
from manhwa_ocr.video.frames import ShotRenderer, format_from
from manhwa_ocr.video.plan import build_plan, title_card_overlays, validate
from manhwa_ocr.video.titlecards import card_duration, detect_title_cue, render_title_card

FMT = format_from("16:9", 30)
VCFG = load_config().video


# --------------------------------------------------------------- detection

@pytest.mark.parametrize("text,label,number,line", [
    ("Age 4. Week 6 of possession.", "Age", 4, "WEEK 6 OF POSSESSION."),
    ("Age 4. Week 15 of possession.", "Age", 4, "WEEK 15 OF POSSESSION."),
    ("Age 8. Year 4 of possession.", "Age", 8, "YEAR 4 OF POSSESSION."),
])
def test_detect_reproduces_the_reference_cards(text, label, number, line):
    assert detect_title_cue(text) == {"label": label, "number": number, "line": line}


def test_detect_number_words():
    assert detect_title_cue("Age four. Week six of possession.") == \
        {"label": "Age", "number": 4, "line": "WEEK 6 OF POSSESSION."}
    assert detect_title_cue("Age twenty-four. Month two of training.") == \
        {"label": "Age", "number": 24, "line": "MONTH 2 OF TRAINING."}


def test_detect_other_cue_shapes():
    assert detect_title_cue("Week 6 of possession.") == \
        {"label": "Week", "number": 6, "line": "WEEK 6 OF POSSESSION."}
    assert detect_title_cue("Three years later, he returned to the academy.") == \
        {"label": "Year", "number": 3, "line": "3 YEARS LATER."}
    # An Age fragment: no line yet — the planner merges the next cue in.
    assert detect_title_cue("Age 4.") == {"label": "Age", "number": 4, "line": ""}


@pytest.mark.parametrize("text", [
    "He trained for 4 weeks to master the form.",
    "The duke was thirty years old.",
    "She was age 4 when the war began.",
    "In week 6, the report finally arrived.",
    "He gained 15 levels in a single night.",
    "Chapter 4 begins with a funeral.",
    "The fourth week of the tournament was brutal.",
    "",
    None,
])
def test_detect_never_fires_on_ordinary_sentences(text):
    assert detect_title_cue(text) is None


def test_card_duration_clamps_to_the_measured_window():
    # The reference's four measured cards run 1.40-2.13s (B1d F6).
    assert card_duration(0.9) == 1.4
    assert card_duration(1.8) == pytest.approx(1.8)
    assert card_duration(5.0) == 2.1


# --------------------------------------------------------------- rendering

def _region(arr, y0, y1, x0, x1):
    return arr[int(y0):int(y1), int(x0):int(x1)]


def test_render_card_pixels():
    img = render_title_card(1920, 1080, "Age", 4, "WEEK 6 OF POSSESSION.")
    arr = np.asarray(img)
    assert tuple(arr[0, 0]) == (0, 0, 0) and tuple(arr[1079, 1919]) == (0, 0, 0)
    # Tile band: the two beige tiles occupy the middle band (tiles block
    # is 33% of frame height, stack centred) — beige + dark digits.
    band = _region(arr, 330, 660, 660, 1260).reshape(-1, 3).astype(int)
    beige = np.mean(np.all(np.abs(band - np.array(titlecards.TILE_FILL)) <= 12, axis=1))
    dark = np.mean(np.max(band, axis=1) < 70)
    assert beige > 0.25 and dark > 0.02
    # Serif label above the tiles: some light pixels, on black.
    label = _region(arr, 200, 300, 700, 1220).reshape(-1, 3)
    assert np.mean(np.min(label, axis=1) > 200) > 0.002
    # Big line below the tiles: white Anton caps.
    line = _region(arr, 720, 880, 300, 1620).reshape(-1, 3)
    assert np.mean(np.min(line, axis=1) > 240) > 0.01


def test_render_card_big_line_matches_the_measured_height():
    # F6: the reference's big line measures 12.2% of frame height
    # (ink); B1c's measured only 9.3%. Anton is wider than the
    # reference's font, so height is the matched dimension.
    img = render_title_card(1920, 1080, "Age", 4, "WEEK 6 OF POSSESSION.")
    arr = np.asarray(img).astype(int)
    white = arr.min(axis=2) > 240                       # the line is pure white
    rows = np.flatnonzero(white.any(axis=1))
    assert len(rows)
    assert (rows[-1] - rows[0] + 1) / 1080 == pytest.approx(0.122, abs=0.012)


def test_render_card_without_line_or_label():
    img = render_title_card(1920, 1080, "Week", 12, "")
    arr = np.asarray(img)
    assert tuple(arr[0, 0]) == (0, 0, 0)
    band = _region(arr, 380, 700, 660, 1260).reshape(-1, 3).astype(int)
    assert np.mean(np.all(np.abs(band - np.array(titlecards.TILE_FILL)) <= 12, axis=1)) > 0.25


def test_renderer_draws_a_title_card_shot():
    shot = {"id": "S001", "start": 0.0, "end": 2.5, "layout": "single",
            "background": "black", "cards": [], "effects": [], "callouts": [],
            "title_card": {"label": "Age", "number": 4, "line": "WEEK 6 OF POSSESSION."}}
    r = ShotRenderer(shot, {}, FMT)
    a = r.frame_array(0.5)
    assert tuple(a[0, 0]) == (0, 0, 0)
    assert np.array_equal(a, r.frame_array(1.5))     # a card never moves


# --------------------------------------------------------------- plan overlays

def test_overlay_fires_at_the_cue_sentence_start():
    # Analysis fix 3: the card is an overlay at the cue's own start —
    # no shot is consumed, split or shifted for it.
    wins = [{"text": "Age 4. Week 6 of possession.", "start": 6.0, "end": 7.5}]
    out = title_card_overlays(wins, {})
    assert out == [{"at": 6.0, "duration": 1.5, "label": "Age", "number": 4,
                    "line": "WEEK 6 OF POSSESSION.",
                    "sentence": "Age 4. Week 6 of possession."}]


def test_overlay_long_cue_clamps_to_the_measured_max():
    wins = [{"text": "Age 8. Year 4 of possession.", "start": 6.0, "end": 11.0}]
    out = title_card_overlays(wins, {})
    assert out[0]["at"] == 6.0 and out[0]["duration"] == 2.1


def test_overlay_merges_an_age_fragment_with_the_next_cue():
    wins = [{"text": "Age 4.", "start": 20.0, "end": 20.8},
            {"text": "Week 6 of possession.", "start": 20.9, "end": 22.0}]
    out = title_card_overlays(wins, {})
    assert len(out) == 1
    assert out[0]["line"] == "WEEK 6 OF POSSESSION."
    assert out[0]["at"] == 20.0 and out[0]["duration"] == 2.0   # span 20.0-22.0


def test_overlay_no_cues_or_disabled_means_no_overlays():
    wins = [{"text": "He trained for 4 weeks.", "start": 6.0, "end": 8.0}]
    assert title_card_overlays(wins, {}) == []
    assert title_card_overlays(wins, {"title_cards": False}) == []
    assert title_card_overlays([], {}) == []


def test_renderer_draws_an_overlay_over_the_running_shot():
    shot = {"id": "S001", "start": 0.0, "end": 5.0, "layout": "single",
            "background": "blur", "effects": [], "callouts": [],
            "cards": [{"panel": "P1", "crop": [0, 0, 1, 1], "enter": "cut",
                       "at": 0.0, "motion": "push_in"}]}
    img = Image.new("RGB", (900, 1200), (120, 90, 70))
    overlays = [{"at": 1.0, "duration": 2.0, "label": "Age", "number": 4,
                 "line": "WEEK 6 OF POSSESSION."}]
    r = ShotRenderer(shot, {"P1": img}, FMT, title_overlays=overlays)
    before = r.frame_array(0.5)
    assert before.mean() > 5                       # the panel shot, not a card
    during = r.frame_array(1.5)
    assert tuple(during[0, 0]) == (0, 0, 0)        # the card's black field
    assert not np.array_equal(before, during)
    after = r.frame_array(4.0)
    assert after.mean() > 5                        # the shot resumes underneath


# --------------------------------------------------------------- build + validate + audit

def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 11):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


PANELS = [{"id": "P001-01", "width": 900, "height": 1200, "text": ""},
          {"id": "P001-02", "width": 900, "height": 1000, "text": ""}]


def test_build_plan_with_a_cue_sentence_gets_a_card_overlay():
    vcfg = {**VCFG, "mode": "panelfit"}
    sentences = [
        {"start": 0.0, "text": "He was a nameless servant.", "panels": ["P001-01"],
         "cues": [], "emphasis": []},
        {"start": 4.0, "text": "Age 4. Week 6 of possession.", "panels": ["P001-02"],
         "cues": [], "emphasis": []},
        {"start": 5.5, "text": "The child opened his eyes.", "panels": ["P001-01"],
         "cues": [], "emphasis": []},
    ]
    plan = build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "tc"},
                      duration=9.5, audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=sentences)
    # Analysis fix 3: NO card shot exists — the cue is a plan-level
    # overlay at the sentence's own start, and the shots are exactly
    # the ones the pass planned (nothing consumed or split).
    assert not any(s.get("title_card") for s in plan["shots"])
    overlays = plan["title_card_overlays"]
    assert len(overlays) == 1
    assert overlays[0]["at"] == pytest.approx(4.0)
    assert overlays[0]["duration"] == pytest.approx(1.5)   # clamp(5.5 - 4.0)
    assert overlays[0]["line"] == "WEEK 6 OF POSSESSION."
    assert plan["panelfit_title_cards"] == overlays        # QA reads the same list
    # The plan still tiles the whole duration, back to back.
    spans = [(float(s["start"]), float(s["end"])) for s in plan["shots"]]
    assert spans[0][0] == 0.0 and spans[-1][1] == 9.5
    assert all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1))
    assert validate(plan, {p["id"] for p in PANELS}) == []


def test_validate_rejects_a_malformed_title_card():
    plan = {"version": plan_mod.PLAN_VERSION, "duration": 2.5, "shots": [
        {"id": "S001", "start": 0.0, "end": 2.5, "layout": "single", "background": "black",
         "exit": "cut", "transition": "cut", "cards": [], "effects": [], "callouts": [],
         "title_card": {"label": "Age", "number": "four", "line": "WEEK 6."}}],
        "chapter_cards": [], "sfx": [], "captions": []}
    errs = validate(plan, {"P001-01"})
    assert any("title_card" in e for e in errs)


def test_validate_rejects_a_malformed_title_card_overlay():
    plan = {"version": plan_mod.PLAN_VERSION, "duration": 6.0, "shots": [
        {"id": "S001", "start": 0.0, "end": 6.0, "layout": "single", "background": "blur",
         "exit": "cut", "transition": "cut", "effects": [], "callouts": [],
         "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut",
                    "at": 0.0, "motion": "push_in"}]}],
        "chapter_cards": [], "sfx": [], "captions": [],
        "title_card_overlays": [{"at": 2.0, "duration": 9.0, "label": "Age",
                                 "number": "four", "line": "WEEK 6."}]}
    errs = validate(plan, {"P001-01"})
    assert any("title_card_overlays" in e for e in errs)


def test_audit_counts_card_time_as_covering_its_cue():
    segs = [
        {"sentence": 1, "text": "a servant", "start": 0.0, "end": 2.0,
         "tagged_panels": ["P1"], "display_action": "SHOW", "selected_panel": "P1",
         "selected_by_align": "P1", "sequence": ["P1"]},
        {"sentence": 2, "text": "Age 4. Week 6 of possession.", "start": 2.0, "end": 3.5,
         "tagged_panels": ["P2"], "display_action": "SHOW", "selected_panel": "P2",
         "selected_by_align": "P2", "sequence": ["P2"]},
        {"sentence": 3, "text": "he woke", "start": 3.5, "end": 6.0,
         "tagged_panels": ["P2"], "display_action": "SHOW", "selected_panel": "P2",
         "selected_by_align": "P2", "sequence": ["P2"]},
    ]
    shots = [
        {"id": "S001", "start": 0.0, "end": 2.0, "layout": "single", "background": "blur",
         "cards": [{"panel": "P1", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                    "motion": "push_in"}]},
        {"id": "S002", "start": 2.0, "end": 4.0, "layout": "single", "background": "blur",
         "cards": [{"panel": "P2", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                    "motion": "push_in"}]},
        {"id": "S003", "start": 4.0, "end": 6.0, "layout": "single", "background": "blur",
         "cards": [{"panel": "P2", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                    "motion": "push_in"}]},
    ]
    plan = {"chapter": "1", "duration": 6.0, "mode": "panelfit", "shots": shots,
            "title_card_overlays": [{"at": 2.0, "duration": 2.0, "label": "Age",
                                     "number": 4, "line": "WEEK 6 OF POSSESSION."}]}
    rep = syncaudit.audit(plan, {"segments": segs}, [])
    # The cue sentence is fully "covered" by its card overlay, and the
    # next sentence opening under the overlay's tail is not a late cut.
    cov = {c["sentence"]: c for c in rep["tagged_coverage"]}
    assert cov[2]["coverage"] == 1.0 and cov[2]["pass"] is True
    assert rep["late_cuts"] == []
    assert rep["gate"]["pass"] is True
