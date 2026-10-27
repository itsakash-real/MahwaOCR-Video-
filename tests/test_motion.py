"""Parameterized motion engine tests (fix/video-vertical-motion, MOTION ONLY).

Target LOCKED by user decision 2026-10-26: match the reference video
(https://youtu.be/5Bf9zpjEs8g, measured 22 sampled shots) -- mostly
static holds with hard cuts, plus exactly two narrow exceptions: a slow
zoom-in on wide text-heavy images, and a very slight vertical drift on
tall strips. NO alternation, NO pan, NO zoom-out, NO full slide.

The engine is pure functions over numbers: routing, zoom/drift math,
and the overscan margin strictly required for a motion to execute
without revealing background. No renderer, no planner, no existing mode
is touched by this module -- these tests pin the math the calibration
follow-up (after the second video's analysis) will re-tune via
MotionParams.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.video.motion import (  # noqa: E402
    MOTION_KINDS,
    MotionParams,
    assign_motions,
    drift_for,
    motion_is_moving,
    overscan_for,
    route,
    zoom_text_for,
)

W, H = 1080.0, 1920.0  # 9:16 canvas


def test_route_defaults_to_hold():
    assert route(900, 1200, W, H) == "hold"      # square-ish panel
    assert route(1600, 900, W, H) == "hold"      # landscape, not text-heavy
    assert route(1080, 1920, W, H) == "hold"     # exactly frame-shaped
    assert route(900, 1500, W, H) == "hold"      # portrait but not taller than frame


def test_route_tall_strips_drift():
    assert route(900, 2400, W, H) == "drift"     # 0.375, taller than frame
    assert route(900, 2000, W, H) == "drift"


def test_route_zoom_text_only_for_wide_text_heavy():
    assert route(1600, 900, W, H, text_heavy=True) == "zoom_text"
    assert route(1600, 900, W, H, text_heavy=False) == "hold"
    # Text-heavy but NOT wide holds: the exception is specifically for
    # WIDE text images; no tall-text or square-text profile is invented.
    assert route(900, 2400, W, H, text_heavy=True) == "drift"  # tall wins
    assert route(900, 1200, W, H, text_heavy=True) == "hold"


def test_zoom_text_spans_the_shot_in_the_measured_band():
    z = zoom_text_for(5.0)
    assert (z["start"], z["end"]) == (1.0, 1.18)  # midpoint of measured 1.15-1.2
    assert abs(z["rate"] * 5.0 - 0.18) < 1e-9    # rate x duration = total span
    assert z["drift"] == 0.02                    # slight downward drift (estimated)
    assert abs(z["drift_speed"] * 5.0 - 0.02) < 1e-9


def test_drift_is_capped_tiny_not_a_slide():
    d = drift_for(8.0)
    assert d["total"] == 0.02                    # far below panelfit's 5-13%
    assert abs(d["speed"] * 8.0 - 0.02) < 1e-9
    assert d["dir"] == "down"


def test_assign_motions_no_alternation_mostly_holds():
    items = [
        {"panel_w": 900, "panel_h": 1200, "duration": 3.0},
        {"panel_w": 900, "panel_h": 1200, "duration": 3.0},
        {"panel_w": 900, "panel_h": 2400, "duration": 3.0},
        {"panel_w": 1600, "panel_h": 900, "duration": 5.0, "text_heavy": True},
        {"panel_w": 900, "panel_h": 1200, "duration": 3.0},
    ]
    motions = assign_motions(items, W, H)
    kinds = [m["kind"] for m in motions]
    # Adjacent holds are EXPECTED (the reference is mostly holds) -- the
    # old alternation rule is superseded and must NOT appear here.
    assert kinds == ["hold", "hold", "drift", "zoom_text", "hold"]
    assert all(k in MOTION_KINDS for k in kinds)
    assert "zoom_text" in kinds and "drift" in kinds


def test_assign_motions_never_emits_other_profiles():
    motions = assign_motions([
        {"panel_w": 900, "panel_h": 1200, "duration": 3.0},
        {"panel_w": 900, "panel_h": 2400, "duration": 3.0},
        {"panel_w": 1600, "panel_h": 900, "duration": 5.0, "text_heavy": True},
        {"panel_w": 500, "panel_h": 2000, "duration": 4.0},
    ], W, H)
    assert all(m["kind"] in MOTION_KINDS for m in motions)
    assert not any(m["kind"] in ("pan", "zoom_out", "slide", "shake") for m in motions)


def test_hold_is_valid_static_and_others_move():
    assert motion_is_moving({"kind": "hold"}) is False  # static BY DESIGN
    assert motion_is_moving({"kind": "zoom_text", "start": 1.0, "end": 1.18}) is True
    assert motion_is_moving({"kind": "drift", "total": 0.02}) is True
    assert motion_is_moving({"kind": "drift", "total": 0.0}) is False


def test_overscan_covers_only_what_each_motion_needs():
    assert overscan_for({"kind": "hold"}) == {"source_scale": 1.0, "staged_extra": 0.0}
    z = overscan_for({"kind": "zoom_text"})
    assert z["source_scale"] >= 1.18  # covers the zoom endpoint
    d = overscan_for({"kind": "drift", "total": 0.02})
    assert d["staged_extra"] == 0.02  # tiny: the drift total, nothing more


def test_params_are_tunable_for_calibration():
    p = MotionParams(zoom_text_end=1.15, tall_drift_total=0.01)
    z = zoom_text_for(5.0, p)
    assert (z["start"], z["end"]) == (1.0, 1.15)
    d = drift_for(5.0, p)
    assert d["total"] == 0.01
    motions = assign_motions([{"panel_w": 1600, "panel_h": 900, "duration": 5.0,
                               "text_heavy": True}], W, H, p)
    assert motions[0]["end"] == 1.15
