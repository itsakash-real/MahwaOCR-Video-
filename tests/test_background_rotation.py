"""Deterministic background rotation + 2s-refresh guard (user rule, 2026-10-19).

Backgrounds rotate deterministically per shot index (0-based, plan order):
i % 10 == 4 -> "black", i % 10 == 9 -> "white", else "blur".
Exactly 80% blur / 10% black / 10% white over every 10 shots, evenly
spaced, same plan -> same backgrounds, no randomness.

2s-refresh guard: an 8s same-panel stretch must never be emitted as
multiple 2s shots with identical panel + identical crop + restarted
entrance (the old pattern that read as the panel "refreshing" every 2s).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.video.plan import (  # noqa: E402
    background_for_index, build_plan, ensure_split_crop_change)

VCFG = load_config().video
PANELS = [{"id": f"P001-0{i}", "width": 300, "height": 400, "text": ""}
          for i in range(1, 6)]


def _img_for(p):
    return Image.new("RGB", (300, 400), (120, 90, 70))


def _units(n, dur=3.0):
    return [{"start": k * dur, "end": (k + 1) * dur,
             "panels": [f"P001-0{(k % 5) + 1}"], "cues": [],
             "emphasis": [], "sentence": f"Sentence {k + 1}.",
             "sentence_starts": [k * dur]}
            for k in range(n)]


# ------------------------------------------------------------------ rotation


def test_rotation_20_shots_exact_counts_and_positions():
    bgs = [background_for_index(i) for i in range(20)]
    assert bgs[4] == "black" and bgs[14] == "black"
    assert bgs[9] == "white" and bgs[19] == "white"
    assert bgs.count("blur") == 16
    assert bgs.count("black") == 2
    assert bgs.count("white") == 2
    # Black and white never consecutive, never clustered.
    for k in range(19):
        assert not (bgs[k] in ("black", "white") and bgs[k + 1] in ("black", "white"))


def test_rotation_is_deterministic():
    first = [background_for_index(i) for i in range(30)]
    second = [background_for_index(i) for i in range(30)]
    assert first == second
    # Inset layout (never emitted) stays blur.
    assert background_for_index(4, "inset") == "blur"
    assert background_for_index(9, "inset") == "blur"


def test_plan_shots_follow_rotation_by_index():
    plan = build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "x"},
                      duration=72.0, audio=None, vcfg=VCFG, image_for=_img_for,
                      alignment_units=_units(24))
    shots = plan["shots"]
    assert len(shots) >= 10
    for k, s in enumerate(shots):
        assert s["background"] == background_for_index(k), f"shot {k} background"
    # Same plan built twice -> same backgrounds (no randomness).
    plan2 = build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "x"},
                       duration=72.0, audio=None, vcfg=VCFG, image_for=_img_for,
                       alignment_units=_units(24))
    assert [s["background"] for s in plan2["shots"]] == [s["background"] for s in shots]


def test_fixes_override_still_wins():
    plan = build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "x"},
                      duration=72.0, audio=None, vcfg=VCFG, image_for=_img_for,
                      alignment_units=_units(24),
                      fixes={"shot": {"P001-01": {"background": "white"}}})
    # Shot 0 would be blur by rotation; the explicit fixes.json override wins.
    assert plan["shots"][0]["background"] == "white"


# ------------------------------------------------------------------ 2s refresh


def test_8s_same_panel_split_never_refreshes_every_2s():
    """Old bug: an 8s panel was cut into 4 x 2s shots, same panel + same
    crop, each restarting its entrance — it read as the panel refreshing
    every 2s. The guard must punch the crop on every repeat so no two
    consecutive shots share (panel, crop)."""
    shots = [{"id": f"S00{k + 1}", "start": 2.0 * k, "end": 2.0 * (k + 1),
              "layout": "single",
              "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1],
                         "motion": "pan_left", "enter": "fade_in"}],
              "sentence": f"Part {k + 1}."} for k in range(4)]
    out, changes = ensure_split_crop_change(shots)
    assert len(changes) >= 2  # every repeat is punched (a punched shot breaks
    # the next comparison, so the pattern alternates full/punched — but no
    # two consecutive shots ever share panel + crop, which is the invariant)
    for k in range(1, 4):
        prev = out[k - 1]["cards"][0]
        cur = out[k]["cards"][0]
        assert (prev["panel"], list(prev["crop"])) != (cur["panel"], list(cur["crop"]))
    # Every shot the guard actually punched had its restarting pan -> push_in.
    changed_ids = {c["shot"] for c in changes}
    for s in out:
        if s["id"] in changed_ids:
            assert s["cards"][0]["motion"] == "push_in"


def test_no_two_consecutive_shots_share_panel_and_crop():
    shots = [{"id": "S001", "start": 0.0, "end": 2.0, "layout": "single",
              "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "motion": "push_in"}]},
             {"id": "S002", "start": 2.0, "end": 4.0, "layout": "single",
              "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "motion": "push_in"}]},
             {"id": "S003", "start": 4.0, "end": 6.0, "layout": "single",
              "cards": [{"panel": "P001-02", "crop": [0, 0, 1, 1], "motion": "push_in"}]}]
    out, _ = ensure_split_crop_change(shots)
    assert out[1]["cards"][0]["crop"] != [0, 0, 1, 1]
    assert out[2]["cards"][0]["crop"] == [0, 0, 1, 1]  # different panel: untouched
