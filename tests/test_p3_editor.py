"""P3 — EDITOR LAYER: continuous motion, pacing split, callouts, chapter cards,
SFX, music defaults, hook / open-loop markers.

One commit per sub-feature in the implementation; the tests are grouped the
same way so a regression names the piece that broke.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.video import cues  # noqa: E402
from manhwa_ocr.video.editor import (_inset_card, _inset_crop, build_chapter_cards,  # noqa: E402
                                     chapter_cards_to_timestamps, extract_callout, hook_status, pacing_qa,
                                     plan_sfx, resolve_music, resolve_sfx_files, split_long_shot, validate_callout)
from manhwa_ocr.video.frames import Format, ShotRenderer, _draw_callout  # noqa: E402
from manhwa_ocr.video.plan import build_plan, validate  # noqa: E402
from manhwa_ocr.video.render import _audio_graph  # noqa: E402

PANELS = [{"id": f"P001-0{i}", "width": 300, "height": 300 if i % 2 else 700, "tall": i % 2 == 0, "text": ""}
          for i in range(1, 6)]
VCFG = load_config().video


def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 9):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


def _plan(duration, sentences=None, panels=PANELS, vcfg=VCFG, **kw):
    return build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "x"}, duration=duration,
                      audio=None, vcfg=vcfg, image_for=_img_for, sentences=sentences, **kw)


def _card_panel():
    panel = Image.new("RGB", (300, 400), (200, 50, 50))
    ImageDraw.Draw(panel).rectangle((100, 100, 200, 200), fill=(20, 20, 220))
    return panel


def _shot(**kw):
    s = {"start": 0, "end": 4, "layout": "single", "background": "black",
         "cards": [{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "push_in"}],
         "exit": "cut", "effects": []}
    s.update(kw)
    return s


# ------------------------------------------------------------------ motion


def test_planner_never_emits_hold():
    plan = _plan(120.0)
    motions = [c["motion"] for s in plan["shots"] for c in s["cards"]]
    assert motions and "hold" not in motions
    assert set(motions) <= {"push_in", "pull_out", "pan_left", "pan_right", "pan_up", "pan_down"}


def test_consecutive_shots_vary_motion():
    plan = _plan(300.0)
    motions = [s["cards"][0]["motion"] for s in plan["shots"] if s["cards"]]
    assert len(set(motions)) > 1  # varied direction, never the same drift twice in a row


def test_push_in_moves_through_whole_shot():
    fmt = Format(320, 180, 30)
    r = ShotRenderer(_shot(end=5), {"A": _card_panel()}, fmt)
    assert r.static_from == pytest.approx(5.0)  # never settles mid-shot
    f3, f4 = (r.frame_array(t).astype(int) for t in (3.0, 4.5))
    assert abs(f4 - f3).mean() > 0  # still moving late in the shot


def test_hold_settles_then_holds():
    # Hand-set payoff beats keep the old ease-out-then-hold behaviour.
    fmt = Format(320, 180, 30)
    shot = _shot(end=5, cards=[{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "hold"}])
    r = ShotRenderer(shot, {"A": _card_panel()}, fmt)
    assert r.static_from <= 3.0
    assert (r.frame_array(4.5) == r.frame_array(4.0)).all()
    f0, f1, f2 = (r.frame_array(t).astype(int) for t in (0.0, 1.0, 2.0))
    assert abs(f1 - f0).mean() > abs(f2 - f1).mean()  # ease-out: most of the move happens early


# ------------------------------------------------------------------ pacing


def test_split_long_shot_punches_in():
    shot = {"id": "S001", "start": 0.0, "end": 10.0, "layout": "single", "background": "black",
            "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "push_in"}],
            "exit": "cut", "effects": [], "sentence": "A very long sentence."}
    parts = split_long_shot(shot)
    assert len(parts) == 2  # ceil(10 / 8): every piece in (4, 8]s, above the 3s min_hold floor
    assert all(3.0 <= p["end"] - p["start"] <= 8.0 + 0.001 for p in parts)
    assert parts[0]["start"] == 0.0 and parts[-1]["end"] == 10.0
    # Later pieces punch in: the crop shrinks toward the centre.
    assert parts[1]["cards"][0]["crop"] != [0, 0, 1, 1]
    assert parts[1]["cards"][0]["crop"][0] > 0 and parts[1]["cards"][0]["crop"][2] < 1
    assert parts[0]["id"] == "S001-a" and parts[1]["id"] == "S001-b"


def test_inset_crop_preserves_aspect_even_near_the_30pct_floor():
    # Regression: clamping width and height independently against the 30%
    # floor could stop one axis earlier than the other, skewing the
    # punched-in box's pixel aspect into one no allowed card shape matches
    # -- QA then failed on exactly the punched-in (second) piece of a split
    # shot. A box whose two axes sit at different fractions of the floor
    # must still come out with the SAME w/h ratio as the input.
    box = [0.0, 0.0, 1.0, 0.32]   # h=0.32 sits right at the 30% floor; w=1.0 has plenty of room
    out = _inset_crop(box, 0.08)
    in_ratio = (box[2] - box[0]) / (box[3] - box[1])
    out_ratio = (out[2] - out[0]) / (out[3] - out[1])
    assert out_ratio == pytest.approx(in_ratio, rel=1e-3)   # 4dp rounding in the return is expected
    assert out[3] - out[1] >= 0.30 - 1e-3   # the floor itself still holds


def test_inset_card_scales_pad_y_with_the_crop_so_effective_aspect_holds():
    # Regression: a whole-panel-with-padding card (pad_y > 0) punched in by
    # crop alone left pad_y's fraction unchanged while the crop shrank,
    # growing padding's share of the card's EFFECTIVE height (crop + 2*pad)
    # and skewing it into an aspect no allowed shape matches -- this broke
    # exactly the punched-in (second) piece of a split shot in real builds
    # (P011-02, P026-01 in chapter 1: QA failed only on the "-b" piece).
    card = {"panel": "A", "crop": [0.0, 0.0, 1.0, 1.0], "pad_y": 0.125, "motion": "push_in"}
    pw, ph = 900.0, 720.0

    def effective_aspect(c):
        x0, y0, x1, y1 = c["crop"]
        return ((x1 - x0) * pw) / (((y1 - y0) + 2 * c.get("pad_y", 0.0)) * ph)

    before = effective_aspect(card)
    after_card = _inset_card(card, 0.08)
    after = effective_aspect(after_card)
    assert after == pytest.approx(before, rel=1e-2)
    assert after_card["pad_y"] < card["pad_y"]   # padding shrank along with the crop
    assert after_card["crop"] != card["crop"]


def test_split_long_shot_leaves_short_shots_alone():
    shot = {"id": "S001", "start": 0.0, "end": 5.0, "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1]}]}
    assert split_long_shot(shot) == [shot]


def test_pacing_qa_flags_long_single_card_shots():
    qa = pacing_qa([{"id": "S001", "start": 0.0, "end": 10.0, "cards": [{"panel": "P001-01"}]},
                    {"id": "S002", "start": 10.0, "end": 13.0, "cards": [{"panel": "P001-02"}]}])
    assert [x["shot"] for x in qa["long_shots"]] == ["S001"]
    assert qa["max_shot"] == pytest.approx(10.0)


def test_build_plan_splits_long_units():
    units = [{"panels": ["P001-01"], "start": 0.0, "end": 12.0,
              "sentence": "A very long sentence that holds one frame far too long.",
              "cues": [], "sentence_starts": [0.0]}]
    plan = _plan(12.0, alignment_units=units)
    assert len(plan["shots"]) >= 2
    assert all(s["end"] - s["start"] <= 8.0 + 0.001 for s in plan["shots"])
    assert plan["pacing"]["split_shots"] >= 1
    assert validate(plan, {p["id"] for p in PANELS}) == []


# ------------------------------------------------------------------ callouts


def test_extract_callout_keyword_number_and_none():
    assert extract_callout("He reaches SS-RANK and the hall goes silent.", ["impact"]) == {
        "text": "SS-RANK", "style": "keyword"}
    assert extract_callout("The strike deals 50,000 damage to the boss.", ["impact"])["style"] == "number"
    assert extract_callout("He reaches SS-RANK and the hall goes silent.", []) is None  # peaks only
    assert extract_callout("He walks into the room quietly.", ["impact"]) is None  # no keyword


def test_validate_callout_rejects_long_and_multiline():
    assert validate_callout({"t": 0.4, "text": "OK", "style": "keyword"}) == []
    assert validate_callout({"t": 0.4, "text": "x" * 41, "style": "keyword"})
    assert validate_callout({"t": 0.4, "text": "a\nb\nc", "style": "keyword"})
    assert validate_callout({"t": -1, "text": "OK", "style": "keyword"})


def test_callout_renders_in_bottom_band_only():
    fmt = Format(320, 180, 30)
    img = Image.new("RGB", (320, 180), (200, 50, 50))
    _draw_callout(img, {"t": 0.4, "text": "SS-RANK", "style": "keyword", "dur": 1.2}, fmt, 0.5)
    top = img.crop((0, 0, 320, 120))
    bottom = img.crop((0, 140, 320, 180))
    assert top.tobytes() == Image.new("RGB", (320, 120), (200, 50, 50)).tobytes()  # never over faces
    assert bottom.tobytes() != Image.new("RGB", (320, 40), (200, 50, 50)).tobytes()
    img2 = Image.new("RGB", (320, 180), (200, 50, 50))
    _draw_callout(img2, {"t": 0.4, "text": "SS-RANK", "style": "keyword", "dur": 1.2}, fmt, 5.0)
    assert img2.tobytes() == Image.new("RGB", (320, 180), (200, 50, 50)).tobytes()  # outside window: no-op


def test_build_plan_attaches_callouts_to_impact_shots():
    units = [{"panels": ["P001-01"], "start": 0.0, "end": 4.0,
              "sentence": "He reaches SS-RANK and the hall goes silent.",
              "cues": ["impact"], "sentence_starts": [0.0]}]
    plan = _plan(4.0, alignment_units=units)
    callouts = [c for s in plan["shots"] for c in s.get("callouts") or []]
    assert callouts and callouts[0]["text"] == "SS-RANK"


# ------------------------------------------------------------------ chapter cards


def test_chapter_cards_every_six_minutes():
    shots = [{"id": f"S{i:03d}", "start": float(i * 10), "end": float(i * 10 + 10),
              "sentence": "The trial begins with a shocking reveal today."} for i in range(80)]
    cards = build_chapter_cards(shots, 800.0)
    assert [c["t"] for c in cards] == [360.0, 720.0]
    assert all(0 < len(c["title"]) <= 40 for c in cards)
    assert all(c["dur"] == pytest.approx(1.75) for c in cards)


def test_chapter_cards_none_for_short_video():
    shots = [{"id": "S001", "start": 0.0, "end": 60.0, "sentence": "Short."}]
    assert build_chapter_cards(shots, 300.0) == []


def test_chapter_cards_timestamps():
    assert chapter_cards_to_timestamps([{"t": 366.0, "title": "The Trial Begins"}]) == ["06:06 The Trial Begins"]


# ------------------------------------------------------------------ SFX + music


def test_plan_sfx_maps_events():
    shots = [{"start": 0.0, "end": 4.0, "sentence": "A system window appears before him.",
              "effects": [{"type": "shake", "t": 1.0}, {"type": "flash", "t": 3.0}]}]
    events = plan_sfx(shots, [{"t": 360.0, "title": "Part Two", "dur": 1.75}])
    by_type = {e["type"] for e in events}
    assert by_type == {"whoosh", "impact", "sting", "ding"}
    assert {"t": 360.0, "type": "whoosh"} in events
    assert {"t": 1.0, "type": "impact"} in events


def test_resolve_sfx_files_missing_dir_is_silent(tmp_path):
    found, missing = resolve_sfx_files(tmp_path, "sfx")
    assert found == {} and set(missing) == {"whoosh", "impact", "ding", "sting"}
    found, missing = resolve_sfx_files(tmp_path, None)
    assert found == {} and missing == []


def test_resolve_sfx_files_finds_dropped_files(tmp_path):
    (tmp_path / "sfx").mkdir()
    (tmp_path / "sfx" / "whoosh.mp3").write_bytes(b"fake")
    found, missing = resolve_sfx_files(tmp_path, "sfx")
    assert found == {"whoosh": tmp_path / "sfx" / "whoosh.mp3"}
    assert set(missing) == {"impact", "ding", "sting"}


def test_audio_graph_sfx_timing():
    g = _audio_graph(True, 10.0, 0.22, [(3, 1500, "whoosh"), (4, 5000, "impact")])
    assert "adelay=1500|1500" in g and "adelay=5000|5000" in g
    assert "amix=inputs=4" in g  # narr + ducked music + 2 SFX
    g0 = _audio_graph(False, 10.0, 0.22)
    assert "adelay" not in g0 and "amix=inputs=1" in g0


def test_resolve_music_explicit_bed_and_none(tmp_path):
    assert resolve_music({"music": "music/custom.mp3"}, tmp_path) == "music/custom.mp3"
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "bed.mp3").write_bytes(b"fake")
    assert resolve_music({"music": None}, tmp_path) == "music/bed.mp3"
    assert resolve_music({"music": None}, tmp_path / "nope") is None


# ------------------------------------------------------------------ hook / loops


def test_parse_hook_and_loop_markers():
    text = ("[HOOK]\n{P001-01} He reaches SS-RANK and the hall goes silent.\n"
            "[LOOP: Will he survive the trial?]\n{P001-02} The gate opens before him.\n")
    parsed = cues.parse(text, {"P001-01", "P001-02"})
    assert parsed["sentences"][0].get("hook") is True
    assert parsed["sentences"][1].get("loops") == ["Will he survive the trial?"]
    # Markers are editing cues, never spoken.
    assert "[HOOK]" not in cues.tts_text(text) and "LOOP" not in cues.tts_text(text)


def test_parse_without_markers_is_unchanged():
    parsed = cues.parse("{P001-01} He walks in.", {"P001-01"})
    assert "hook" not in parsed["sentences"][0] and "loops" not in parsed["sentences"][0]


def test_hook_status():
    with_hook = [{"text": "a", "hook": True, "loops": ["Will he survive?"]}, {"text": "b"}]
    st = hook_status(with_hook)
    assert st["explicit_hook"] is True and st["open_loops"] == 1 and st["warning"] is None
    st2 = hook_status([{"text": "a"}, {"text": "b"}])
    assert st2["explicit_hook"] is False and "[HOOK]" in (st2["warning"] or "")


def test_validate_cues_rejects_bad_hook():
    problems = cues.validate_cues({"sentences": [{"text": "x", "panels": [], "hook": "yes"}]})
    assert problems and "hook" in problems[0]
    assert cues.validate_cues({"sentences": [{"text": "x", "panels": [], "hook": True,
                                             "loops": ["Why?"]}]}) == []


# ------------------------------------------------------------------ plan integration + config


def test_plan_carries_p3_fields_and_validates():
    plan = _plan(400.0)
    assert isinstance(plan.get("chapter_cards"), list)
    assert isinstance(plan.get("sfx"), list)
    assert isinstance(plan.get("pacing"), dict)
    assert validate(plan, {p["id"] for p in PANELS}) == []


def test_config_defaults_include_p3_keys():
    vcfg = load_config().video
    assert vcfg.get("sfx_dir") is None
    assert float(vcfg.get("music_fade", 0)) == pytest.approx(0.5)
