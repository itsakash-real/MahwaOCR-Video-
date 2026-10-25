"""Parameterized motion engine tests (fix/video-vertical-motion, MOTION ONLY).

The engine is pure functions over numbers: routing, zoom/slide math,
alternation, and the overscan margin strictly required for a motion to
execute without revealing background. No renderer, no planner, no
existing mode is touched by this module -- these tests pin the math the
reference-calibration pass will later re-tune via MotionParams.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.video.motion import (  # noqa: E402
    MotionParams,
    assign_motions,
    classify,
    motion_is_moving,
    overscan_for,
    slide_distance,
    slide_for,
    zoom_for,
)

W, H = 1080.0, 1920.0  # 9:16 canvas


def test_classify_routes_by_frame_aspect():
    assert classify(900, 1200, W, H) == "zoom"     # 0.75, wider than frame
    assert classify(1600, 900, W, H) == "zoom"     # landscape
    assert classify(1080, 1920, W, H) == "zoom"    # exactly frame-shaped: never a 0-travel slide
    assert classify(900, 2400, W, H) == "slide"    # 0.375, taller than frame
    assert classify(900, 2000, W, H) == "slide"


def test_slide_distance_is_width_fit_minus_frame():
    assert slide_distance(900, 2400, W, H) == 2880.0 - 1920.0  # 960.0
    assert slide_distance(900, 1200, W, H) == 0.0
    assert slide_distance(1600, 900, W, H) == 0.0


def test_zoom_span_completes_exactly_over_the_shot():
    zin = zoom_for("in", 5.0)
    assert (zin["start"], zin["end"]) == (1.0, 1.2)
    assert abs(zin["rate"] * 5.0 - 0.2) < 1e-9  # rate x duration = total span
    zout = zoom_for("out", 4.0)
    assert (zout["start"], zout["end"]) == (1.2, 1.0)
    assert abs(zout["rate"] * 4.0 + 0.2) < 1e-9
    assert zin["rate"] > 0 and zout["rate"] < 0


def test_zoom_rate_mode_uses_fixed_rate_with_cap():
    p = MotionParams(zoom_mode="rate", zoom_rate=0.05, zoom_cap=1.2)
    z = zoom_for("in", 10.0, p)
    assert z["rate"] == 0.05 and z["end"] == 1.2  # 1.0 + 0.5 capped at 1.2
    z = zoom_for("in", 2.0, p)
    assert z["end"] == 1.1  # under the cap: no hold yet


def test_slide_spans_the_shot_and_syncs_to_duration():
    s = slide_for("down", 900, 2400, W, H, 8.0)
    assert s["delta_y"] == 960.0
    assert abs(s["speed"] * 8.0 - 960.0) < 1e-9  # speed x duration = travel
    assert s["staged_h"] == 1920.0 + 960.0  # no extra headroom by default


def test_assign_motions_alternates_globally_and_per_kind():
    items = [
        {"panel_w": 900, "panel_h": 1200, "duration": 5.0},   # zoom
        {"panel_w": 900, "panel_h": 2400, "duration": 5.0},   # slide
        {"panel_w": 1600, "panel_h": 900, "duration": 5.0},   # zoom
        {"panel_w": 900, "panel_h": 2400, "duration": 5.0},   # slide
        {"panel_w": 900, "panel_h": 1200, "duration": 5.0},   # zoom
        {"panel_w": 900, "panel_h": 2400, "duration": 5.0},   # slide
    ]
    motions = assign_motions(items, W, H)
    names = [f"{m['kind']}_{m['dir']}" for m in motions]
    assert names == ["zoom_in", "slide_down", "zoom_out",
                     "slide_up", "zoom_in", "slide_down"]
    assert all(a != b for a, b in zip(names, names[1:]))
    assert all(motion_is_moving(m) for m in motions)
    # Consecutive SHOTS differ: group back into shots of one card each here,
    # the same property the planner-level test asserts on shot lists.
    assert all(a != b for a, b in zip(names, names[1:]))


def test_assign_motions_never_emits_static():
    motions = assign_motions([
        {"panel_w": 1080, "panel_h": 1920, "duration": 3.0},  # exact frame: zoom, not 0-slide
        {"panel_w": 900, "panel_h": 2400, "duration": 3.0},
    ], W, H)
    assert motions[0]["kind"] == "zoom"
    assert all(motion_is_moving(m) for m in motions)
    assert not motion_is_moving({"kind": "zoom", "zoom_start": 1.0, "zoom_end": 1.0})
    assert not motion_is_moving({"kind": "slide", "delta_y": 0.0})


def test_overscan_covers_the_full_motion_range():
    # Zoom: staged source scale must reach the zoom endpoint (20% margin).
    z = {"kind": "zoom", "zoom_start": 1.0, "zoom_end": 1.2}
    assert overscan_for(z)["source_scale"] >= 1.2
    # Slide: staged extra must equal the travel (endpoints sit on edges).
    s = {"kind": "slide", "delta_y": 960.0}
    assert overscan_for(s)["staged_extra"] == 960.0
    # A wider calibration margin is honoured without code changes.
    p = MotionParams(overscan=1.25)
    assert overscan_for(z, p)["source_scale"] == 1.25


def test_params_are_tunable_for_calibration():
    p = MotionParams(zoom_in_end=1.15, zoom_out_start=1.15)
    z = zoom_for("in", 5.0, p)
    assert (z["start"], z["end"]) == (1.0, 1.15)
    motions = assign_motions([{"panel_w": 900, "panel_h": 1200, "duration": 5.0}], W, H, p)
    assert motions[0]["zoom_end"] == 1.15
