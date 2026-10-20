"""Cinematic video mode: full-screen panels, no cards."""

import numpy as np
from PIL import Image, ImageDraw

from manhwa_ocr.config import DEFAULT_CONFIG
from manhwa_ocr.video import cinematic as cine
from manhwa_ocr.video.cinematic import (
    CinematicShot,
    draw_name_card,
    layout_for,
    luminous_pillars,
)
from manhwa_ocr.video.frames import Format
from manhwa_ocr.video.plan import _cinematic_pass


def _panel(w=300, h=500, color=(200, 50, 50)):
    p = Image.new("RGB", (w, h), color)
    ImageDraw.Draw(p).rectangle((w // 4, h // 4, 3 * w // 4, 3 * h // 4), fill=(20, 20, 220))
    return p


# ------------------------------------------------------------------ layout


def test_layout_for_picks_tall_and_fullbleed():
    assert layout_for(0.5) == cine.LAYOUT_TALL
    assert layout_for(1.0) == cine.LAYOUT_TALL
    assert layout_for(1.34) == cine.LAYOUT_TALL
    assert layout_for(1.35) == cine.LAYOUT_FULLBLEED
    assert layout_for(2.0) == cine.LAYOUT_FULLBLEED
    assert layout_for("junk") == cine.LAYOUT_TALL


def test_luminous_pillars_fill_frame_and_stay_bright():
    panel = _panel(300, 500, (180, 100, 60))
    out = luminous_pillars(panel, 640, 360)
    assert out.size == (640, 360)
    # Luminous, never a dark scrim: mean brightness stays close to the panel's.
    assert np.asarray(out).mean() > np.asarray(panel).mean() * 0.75


def test_name_card_draws_in_lower_third():
    img = Image.new("RGB", (640, 360), (30, 30, 30))
    before = np.asarray(img).copy()
    draw_name_card(img, "Kim Jin-Ho", alpha=1.0)
    after = np.asarray(img)
    assert not (before == after).all()
    diff = (after.astype(int) - before.astype(int)).sum(axis=2) != 0
    ys = np.where(diff.any(axis=1))[0]
    assert ys.min() > 360 * 0.6  # lower third only


def test_name_card_empty_is_noop():
    img = Image.new("RGB", (640, 360), (30, 30, 30))
    before = np.asarray(img).copy()
    draw_name_card(img, "", alpha=1.0)
    draw_name_card(img, "X", alpha=0.0)
    assert (np.asarray(img) == before).all()


# ------------------------------------------------------------------ shot rendering


def _cshot(panel, **kw):
    shot = {"start": 0, "end": 4, "cards": [{"panel": "A"}]}
    shot.update(kw)
    return CinematicShot(shot, {"A": panel}, Format(640, 360, 30))


def test_tall_panel_fills_frame_height():
    # 300x500 panel on a 640x360 frame: full height, centred.
    arr = _cshot(_panel(300, 500)).frame_array(0.0)
    assert arr.shape == (360, 640, 3)
    # The centre column near the top is the panel itself (tall red).
    assert arr[10, 320, 0] > 150
    # Pillars on the far left are the blurred panel, not black.
    assert arr[180, 10].mean() > 40


def test_fullbleed_panel_covers_frame():
    arr = _cshot(_panel(800, 300), layout="fullbleed").frame_array(0.0)
    assert arr.shape == (360, 640, 3)


def test_zoom_moves_pixels_over_time():
    cs = _cshot(_panel(300, 500))
    a = cs.frame_array(0.05).astype(int)
    b = cs.frame_array(3.9).astype(int)
    assert np.abs(a - b).mean() > 1.0  # slow push-in is visible
    # Pull-back goes the other way but still moves.
    cs2 = _cshot(_panel(300, 500), zoom_dir=-1)
    c = cs2.frame_array(0.05).astype(int)
    d = cs2.frame_array(3.9).astype(int)
    assert np.abs(c - d).mean() > 1.0


def test_focus_shifts_reframe():
    a = _cshot(_panel(300, 500), focus=(0.5, 0.35)).frame_array(1.0)
    b = _cshot(_panel(300, 500), focus=(0.5, 0.65)).frame_array(1.0)
    assert not (a == b).all()


def test_name_card_renders_on_shot():
    plain = _cshot(_panel(300, 500)).frame_array(0.5)
    named = _cshot(_panel(300, 500), name_card="Squishy", name_card_at=0.15).frame_array(0.5)
    assert not (plain == named).all()
    # Before the card's appearance time, the frame is clean (same t).
    plain_early = _cshot(_panel(300, 500)).frame_array(0.05)
    early = _cshot(_panel(300, 500), name_card="Squishy", name_card_at=0.15).frame_array(0.05)
    assert (plain_early == early).all()


# ------------------------------------------------------------------ plan pass


class _Designer:
    def __init__(self, aspects):
        self.aspects = aspects

    def panel_aspect(self, pid):
        return self.aspects.get(pid, 0.6)


def _mkshot(start, end, panel="P001-01", sentence=""):
    return {"id": "S001", "start": start, "end": end, "layout": "single",
            "background": "blur",
            "cards": [{"panel": panel, "crop": [0, 0, 1, 1], "enter": "cut",
                       "at": 0, "motion": "push_in"}],
            "exit": "cut", "effects": [], "transition": "cut"}


def _mkunit(start, end, sentence="", panels=("P001-01",)):
    return {"start": start, "end": end, "sentence": sentence, "panels": list(panels)}


def test_cinematic_pass_marks_shots_and_hard_cuts():
    designer = _Designer({"P001-01": 0.6, "P002-01": 1.8})
    shots = [_mkshot(0, 4), _mkshot(4, 8, panel="P002-01")]
    units = [_mkunit(0, 4, "Squishy wakes up."), _mkunit(4, 8, "A dragon appears.")]
    out = _cinematic_pass(shots, units, designer, {}, ["Squishy"])
    assert all(s["cinematic"] for s in out)
    assert all(s["transition"] == "cut" and s["exit"] == "cut" for s in out)
    assert out[0]["cinematic_layout"] == cine.LAYOUT_TALL
    assert out[1]["cinematic_layout"] == cine.LAYOUT_FULLBLEED
    assert out[0]["zoom_dir"] == 1 and out[1]["zoom_dir"] == -1  # alternating
    assert "caption_bar" not in out[0]


def test_name_card_only_on_first_appearance():
    designer = _Designer({})
    shots = [_mkshot(0, 3), _mkshot(3, 6), _mkshot(6, 9)]
    units = [_mkunit(0, 3, "Squishy wakes up."),
             _mkunit(3, 6, "Squishy meets the Queen."),
             _mkunit(6, 9, "The Queen smiles at Squishy.")]
    out = _cinematic_pass(shots, units, designer, {}, ["Squishy", "Queen"])
    assert out[0]["name_card"] == "Squishy"
    assert out[1]["name_card"] == "Queen"
    assert out[2]["name_card"] is None  # both already introduced


def test_reframe_splits_long_shots():
    designer = _Designer({})
    shots = [_mkshot(0, 12)]
    units = [_mkunit(0, 12, "A very long sentence about the battle.")]
    out = _cinematic_pass(shots, units, designer, {}, [])
    assert len(out) >= 3  # 12s -> recursive halves
    assert all(float(s["end"]) - float(s["start"]) >= 2.0 for s in out)
    panels = [s["cards"][0]["panel"] for s in out]
    assert all(p == "P001-01" for p in panels)  # same panel, reframe only
    assert any(s.get("reframe") for s in out[1:])
    # Back to back, ids re-numbered.
    assert [s["id"] for s in out] == [f"S{i + 1:03d}" for i in range(len(out))]
    for a, b in zip(out, out[1:]):
        assert abs(float(a["end"]) - float(b["start"])) < 1e-6


def test_reframe_disabled_by_config():
    designer = _Designer({})
    shots = [_mkshot(0, 12)]
    units = [_mkunit(0, 12, "A very long sentence.")]
    out = _cinematic_pass(shots, units, designer, {"reframe": False}, [])
    assert len(out) == 1


# ------------------------------------------------------------------ plan integration


def _cine_vcfg(**over):
    from manhwa_ocr.config import load_config
    vcfg = dict(load_config().video)
    vcfg["mode"] = "cinematic"
    ccfg = dict(vcfg.get("cinematic") or {})
    ccfg.update(over)
    vcfg["cinematic"] = ccfg
    return vcfg


def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    return img


def test_build_plan_cinematic_end_to_end():
    from manhwa_ocr.video.plan import build_plan, validate
    panels = [{"id": f"P001-0{i}", "width": 300, "height": 700 if i % 2 else 400, "text": ""}
              for i in range(1, 5)]
    units = [{"start": float(i * 5), "end": float(i * 5 + 5),
              "sentence": f"Squishy does thing number {i}.",
              "sentence_starts": [float(i * 5)], "cues": [], "emphasis": [],
              "panels": [f"P001-0{(i % 4) + 1}"]} for i in range(6)]
    plan = build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "x"},
                      duration=30.0, audio=None, vcfg=_cine_vcfg(),
                      image_for=_img_for, alignment_units=units,
                      characters=["Squishy"])
    assert validate(plan, {p["id"] for p in panels}) == []
    shots = plan["shots"]
    assert shots and all(s.get("cinematic") for s in shots)
    assert plan["captions_enabled"] is False
    assert plan["pacing"]["min_shot"] == 2.0
    assert plan["pacing"]["mode"] == "cinematic"
    # Name card fires once, on the first sentence mentioning Squishy.
    named = [s for s in shots if s.get("name_card")]
    assert len(named) == 1 and named[0]["name_card"] == "Squishy"
    # No pair layouts in cinematic mode.
    assert all(s["layout"] != "pair" for s in shots)


def test_build_plan_classic_unchanged():
    from manhwa_ocr.config import load_config
    from manhwa_ocr.video.plan import build_plan
    panels = [{"id": f"P001-0{i}", "width": 300, "height": 700, "text": ""}
              for i in range(1, 4)]
    units = [{"start": float(i * 7), "end": float(i * 7 + 7),
              "sentence": f"Sentence number {i} here.",
              "sentence_starts": [float(i * 7)], "cues": [], "emphasis": [],
              "panels": [f"P001-0{(i % 3) + 1}"]} for i in range(4)]
    plan = build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "x"},
                      duration=30.0, audio=None, vcfg=dict(load_config().video),
                      image_for=_img_for, alignment_units=units)
    assert not any(s.get("cinematic") for s in plan["shots"])
    assert plan["pacing"]["min_shot"] == 3.0
    assert plan["captions_enabled"] is True


# ------------------------------------------------------------------ config


def test_config_defaults_to_classic_with_cinematic_section():
    vcfg = DEFAULT_CONFIG["video"]
    assert vcfg["mode"] == "classic"
    ccfg = vcfg["cinematic"]
    assert ccfg["captions"] is False
    assert ccfg["name_cards"] is True
    assert ccfg["reframe"] is True
    assert ccfg["min_hold"] == 2.0
