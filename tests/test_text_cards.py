"""The narration-length contract and the text-card ruling (the user's
2026-10-25 rejection of the 3-minute copying-skills chapter 1: a speed-blind
word budget rendered 6 target minutes as 3.1 real minutes, and lettering-only
panels rode into the storyboard).

Two fixes, pinned here:
- the recap word budget scales with the configured TTS speed, and finalize
  REFUSES a chapter whose estimated narration runs outside
  [recap.min_minutes, recap.max_minutes] (6-8 minutes by default);
- lettering-only panels (dramatic one-liners, SFX captions, system boxes —
  white or black canvas) are flagged `text_dominant` at panel build, skipped
  by every selection filter, and a plan showing one fails the build QA.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from manhwa_ocr.script import _length_contract_errors, _target_words
from manhwa_ocr.video.bodytrim import _midtone
from manhwa_ocr.video.build import quality_report
from manhwa_ocr.video.panels import TEXT_MIDTONE_MAX, _text_card_flag
from manhwa_ocr.video.plan import _is_text_card, even_units


# --------------------------------------------------------------- length contract

def test_word_budget_scales_with_tts_speed():
    # 6.5 finished minutes at 1.3x: 6.5 * 150 * 1.3 — the speed-blind
    # 900-word budget was the 3-minute-chapter bug.
    assert _target_words(6.5, 1.3) == 1267
    assert _target_words(6.5, 1.0) == 975


def test_length_gate_refuses_a_short_recap():
    # 915 spoken words at 1.3x = ~4.7 finished minutes: the copying-skills
    # ch1 failure, now a finalize error.
    errs = _length_contract_errors(915, 1.3, 6.0, 8.0)
    assert len(errs) == 1 and "below the 6-minute floor" in errs[0]


def test_length_gate_accepts_the_contract_band():
    assert _length_contract_errors(1300, 1.3, 6.0, 8.0) == []     # ~6.7 min
    assert _length_contract_errors(1560, 1.3, 6.0, 8.0) == []     # exactly 8
    assert _length_contract_errors(1600, 1.3, 6.0, 8.0)           # over the cap
    assert _length_contract_errors(400, 1.3, 0, 0) == []          # bounds disabled


# --------------------------------------------------------------- text cards

def _img(fill, ink_spots=0, seed=1):
    arr = np.full((300, 400, 3), fill, dtype=np.uint8)
    rng = np.random.default_rng(seed)
    for _ in range(ink_spots):
        x, y = rng.integers(0, 380), rng.integers(0, 280)
        arr[y:y + 8, x:x + 20] = (15, 15, 15)
    return Image.fromarray(arr, "RGB")


def test_text_card_detected_on_white_or_black_canvas():
    # Giant glyphs on white: bimodal ink, no midtones, no figure -> card.
    assert _text_card_flag(_img((250, 250, 250), ink_spots=14), 300.0, 0.0, 0.004)
    # White glyphs on black canvas (the "WITHOUT" class): same verdict.
    inv = Image.fromarray(255 - np.asarray(_img((10, 10, 10), ink_spots=14)), "RGB")
    assert _text_card_flag(inv, 300.0, 0.0, 0.004)


def test_real_art_is_never_a_text_card():
    # Continuous midtones (a real scene, snow included) and any drawn
    # figure both veto the flag.
    rng = np.random.default_rng(3)
    arr = rng.integers(60, 200, size=(300, 400, 3), dtype=np.uint8)
    assert not _text_card_flag(Image.fromarray(arr, "RGB"), 300.0, 0.0, 0.004)
    assert float(_midtone(Image.fromarray(arr, "RGB")).mean()) >= TEXT_MIDTONE_MAX
    # A character drawn on a dark panel: the figure exception wins.
    assert not _text_card_flag(_img((12, 12, 12), ink_spots=4), 300.0, 0.05, 0.004)


def test_caption_strips_are_text_cards_by_height():
    assert _text_card_flag(_img((250, 250, 250)), 60.0, 0.0, 0.004)


def test_is_text_card_covers_both_flags():
    assert _is_text_card({"text_only": True})
    assert _is_text_card({"text_dominant": True})
    assert not _is_text_card({"text_dominant": False, "text_only": False})


def test_selection_skips_text_cards():
    panels = [{"id": f"P{i:03d}-01", "width": 800.0, "height": 1000.0,
               "text": ""} for i in (1, 2, 3)]
    panels[1]["text_dominant"] = True
    units = even_units(panels, 9.0, {})
    shown = [u["panels"][0] for u in units]
    assert "P002-01" not in shown and len(shown) == 2


# --------------------------------------------------------------- build QA gate

def _plan_showing(panel_row):
    plan = {"format": "16:9", "fps": 30, "duration": 2.0, "shots": [
        {"id": "S001", "start": 0.0, "end": 2.0, "layout": "single",
         "panelfit": True,
         "cards": [{"panel": panel_row["id"], "enter": "cut",
                    "panelfit": {"kind": "zoom", "base_w": 600.0, "base_h": 1080.0,
                                 "zoom_end": 1.0, "zoom_rate": 0.0, "bg": True}}]}]}
    panels = {"panels": [panel_row]}
    return plan, panels


def test_qa_fails_a_plan_showing_a_text_card():
    plan, panels = _plan_showing({"id": "P019-06", "width": 800.0, "height": 57.0,
                                  "text_only": False, "text_dominant": True})
    qa = quality_report(plan, panels, None, 1.5)
    assert any("text cards" in f for f in qa["failures"])


def test_qa_escape_hatch_for_all_text_series():
    plan, panels = _plan_showing({"id": "P019-06", "width": 800.0, "height": 57.0,
                                  "text_only": False, "text_dominant": True})
    qa = quality_report(plan, panels, None, 1.5, allow_text_panels=True)
    assert not any("text cards" in f for f in qa["failures"])
