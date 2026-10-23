"""CapCut edits + video-defect bugfixes (defects 1-3).

Covers: long shots never freeze, no same-panel split without a crop
change, frame-exact joins, no motion repeat / pan swap-repeat, pair_move
once per pair, bubble painting removed (panel returned unchanged),
bubble panels with no clean crop dropped, transitions frame-exact
words, key word yellow, bottom band), beat sync (cut moved <= 0.15s),
SFX/music missing-dir fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.video import verify as verify_mod  # noqa: E402
from manhwa_ocr.video.captions import (  # noqa: E402
    build_captions, split_sentence_to_chunks, validate_captions, write_ass)
from manhwa_ocr.video.crops import (  # noqa: E402
    bubble_overlaps_face, clean_bubbles, face_bubble_boxes, is_mask_risk)
from manhwa_ocr.video.editor import (  # noqa: E402
    SFX_ALL, plan_sfx, resolve_beats, resolve_music, resolve_sfx_files,
    snap_cuts_to_beats, split_long_shot)
from manhwa_ocr.video.frames import (  # noqa: E402
    TRANSITIONS, Format, ShotRenderer, transition_enter)
from manhwa_ocr.video.plan import (  # noqa: E402
    build_plan, ensure_split_crop_change, shot_frame_bounds)

PANELS = [{"id": f"P001-0{i}", "width": 300, "height": 300 if i % 2 else 700,
           "tall": i % 2 == 0, "text": ""} for i in range(1, 6)]
VCFG = load_config().video


def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 9):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


def _plan(duration, sentences=None, vcfg=None, **kw):
    return build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "x"},
                      duration=duration, audio=None, vcfg=vcfg or VCFG,
                      image_for=_img_for, sentences=sentences, **kw)


def _shot(**kw):
    s = {"start": 0, "end": 4, "layout": "single", "background": "black",
         "cards": [{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "push_in"}],
         "exit": "cut", "effects": []}
    s.update(kw)
    return s


def _card_panel():
    panel = Image.new("RGB", (300, 400), (200, 50, 50))
    ImageDraw.Draw(panel).rectangle((100, 100, 200, 200), fill=(20, 20, 220))
    return panel


def _gradient_panel():
    img = Image.new("RGB", (300, 400))
    px = img.load()
    for x in range(300):
        for y in range(400):
            px[x, y] = (x % 256, y % 256, (x + y) % 256)
    return img


# ------------------------------------------------------------------ defect 1


def test_long_shot_never_freezes_without_hold():
    fmt = Format(320, 180, 30)
    r = ShotRenderer(_shot(end=8, cards=[{"panel": "A", "crop": [0, 0, 1, 1],
                                         "enter": "cut", "at": 0, "motion": "push_in"}]),
                     {"A": _gradient_panel()}, fmt)
    assert not r.is_static(5.0)  # continuous motion: never a frozen frame
    f5 = r.frame_array(5.0)
    f6 = r.frame_array(6.0)
    assert (f5 != f6).any()  # still moving late in the shot


def test_hold_shot_still_freezes():
    fmt = Format(320, 180, 30)
    r = ShotRenderer(_shot(end=8, cards=[{"panel": "A", "crop": [0, 0, 1, 1],
                                         "enter": "cut", "at": 0, "motion": "hold"}]),
                     {"A": _card_panel()}, fmt)
    assert r.is_static(5.0)  # hand-set payoff holds keep the old freeze
    assert (r.frame_array(5.0) == r.frame_array(6.0)).all()


def test_same_panel_split_gets_crop_change():
    shots = [
        {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single",
         "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "motion": "pan_left"}],
         "sentence": "First part."},
        {"id": "S002", "start": 4.0, "end": 8.0, "layout": "single",
         "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "motion": "pan_right"}],
         "sentence": "Second part."},
    ]
    out, changes = ensure_split_crop_change(shots)
    assert len(changes) == 1
    assert changes[0]["shot"] == "S002" and "reason" in changes[0]
    assert out[1]["cards"][0]["crop"] != [0, 0, 1, 1]  # punched in
    assert out[1]["cards"][0]["motion"] == "push_in"  # restarting pan -> push_in


def test_same_panel_split_with_different_crop_untouched():
    shots = [
        {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single",
         "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "motion": "push_in"}]},
        {"id": "S002", "start": 4.0, "end": 8.0, "layout": "single",
         "cards": [{"panel": "P001-01", "crop": [0.1, 0.1, 0.9, 0.9], "motion": "push_in"}]},
    ]
    out, changes = ensure_split_crop_change(shots)
    assert changes == []
    assert out[1]["cards"][0]["crop"] == [0.1, 0.1, 0.9, 0.9]


def test_split_long_shot_pieces_never_restart_a_pan():
    shot = {"id": "S001", "start": 0.0, "end": 10.0, "layout": "single", "background": "black",
            "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0,
                       "motion": "pan_left"}],
            "exit": "cut", "effects": [], "sentence": "A very long sentence."}
    parts = split_long_shot(shot)
    assert len(parts) == 2
    assert parts[1]["cards"][0]["crop"] != [0, 0, 1, 1]
    assert parts[1]["cards"][0]["motion"] == "push_in"


def test_joins_are_frame_exact():
    shots = [{"id": "S001", "start": 0.0, "end": 1.0},
             {"id": "S002", "start": 1.0, "end": 2.5},
             {"id": "S003", "start": 2.5, "end": 4.0}]
    bounds = shot_frame_bounds(shots, 30, 4.0)
    assert bounds == [(0, 30), (30, 75), (75, 120)]  # contiguous, no gap/overlap
    assert bounds[0][0] == 0 and bounds[-1][1] == 120  # covers every frame


def test_zero_frame_shot_rejected():
    shots = [{"id": "S001", "start": 0.0, "end": 0.01}]
    with pytest.raises(Exception, match="shorter than one frame"):
        shot_frame_bounds(shots, 30, 0.01)


# ------------------------------------------------------------------ defect 2


def test_planner_never_repeats_motion():
    plan = _plan(300.0)
    motions = [s["cards"][0]["motion"] for s in plan["shots"] if s["cards"]]
    assert len(motions) > 4
    for a, b in zip(motions, motions[1:]):
        assert a != b  # never the same drift twice in a row


def test_planner_never_pans_twice_in_a_row():
    plan = _plan(300.0)
    motions = [s["cards"][0]["motion"] for s in plan["shots"] if s["cards"]]
    pans = [m for m in motions if m.startswith("pan")]
    assert pans  # pans still happen
    for a, b in zip(motions, motions[1:]):
        assert not (a.startswith("pan") and b.startswith("pan"))


def test_pair_move_fires_once_and_settles():
    fmt = Format(320, 180, 30)
    shot = {"start": 0, "end": 4, "layout": "pair", "background": "black",
            "cards": [{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "push_in"},
                      {"panel": "A", "crop": [0, 0, 1, 1], "enter": "slide_left", "at": 1.0,
                       "motion": "push_in"}],
            "exit": "cut", "effects": []}
    r = ShotRenderer(shot, {"A": _card_panel()}, fmt)
    first = r.cards[0]
    assert not first.pair_move_done(0.5)
    assert first.centre(0.0) != first.centre(2.0)  # the slide happens
    assert first.pair_move_done(2.0)
    assert first.centre(2.0) == first.centre(3.5) == first.rest_c  # ... once, then stays
    assert r.pair_move_done(2.0)


def test_pair_move_done_shot_starts_in_place():
    fmt = Format(320, 180, 30)
    shot = {"start": 0, "end": 4, "layout": "pair", "background": "black",
            "cards": [{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "push_in"},
                      {"panel": "A", "crop": [0, 0, 1, 1], "enter": "slide_left", "at": 1.0,
                       "motion": "push_in"}],
            "exit": "cut", "effects": [], "pair_move_done": True}
    r = ShotRenderer(shot, {"A": _card_panel()}, fmt)
    assert r.cards[0].centre(0.0) == r.cards[0].rest_c  # slide never re-triggers
    assert r.pair_move_done(0.0)


# ------------------------------------------------------------------ defect 3


def test_bubble_overlaps_face():
    assert bubble_overlaps_face((0.1, 0.2, 0.5, 0.4), (0.15, 0.45))  # inside
    assert bubble_overlaps_face((0.1, 0.4, 0.5, 0.6), (0.15, 0.45))  # touches bottom
    assert not bubble_overlaps_face((0.1, 0.5, 0.5, 0.8), (0.15, 0.45))  # below
    assert not bubble_overlaps_face((0.1, 0.0, 0.5, 0.1), (0.15, 0.45))  # above
    assert not bubble_overlaps_face((0.1, 0.2, 0.5, 0.4), None)  # no face: no overlap


def _face_maps(face_bubble, other_bubble):
    regions = []
    if face_bubble is not None:
        regions.append({"box": face_bubble, "fill": 0.9})
    if other_bubble is not None:
        regions.append({"box": other_bubble, "fill": 0.9})
    return {"bubble_regions": regions, "face_override": (0.15, 0.45)}


def test_clean_bubbles_returns_panel_unchanged_even_with_face_bubble():
    # Bubble painting removed entirely: even a bubble overlapping the
    # face box is returned pixel-identical -- no painted patch can ever
    # land on a face again. The crop/drop path deals with it instead.
    import numpy as np

    img = Image.new("RGB", (300, 400), (120, 90, 70))
    # Small (non-dominant) bubble overlapping the face range + one clear of it.
    maps = _face_maps((0.1, 0.2, 0.3, 0.4), (0.1, 0.6, 0.4, 0.9))
    assert not is_mask_risk(maps)  # small bubble: not mask-risk, but still on the face
    assert face_bubble_boxes(maps) == [(0.1, 0.2, 0.3, 0.4)]
    before = np.asarray(img.convert("RGB"), dtype=int).copy()
    out = clean_bubbles(img, maps)
    assert out is img
    assert (np.asarray(out, dtype=int) == before).all()  # pixel-identical
    painted = list(getattr(out, "painted_boxes", []))
    assert painted == []  # no painted patch anywhere, let alone on a face
    for box in painted:
        assert not bubble_overlaps_face(box, (0.15, 0.45))


def test_mask_risk_bubble_never_painted():
    import numpy as np

    img = Image.new("RGB", (300, 400), (120, 90, 70))
    maps = _face_maps((0.05, 0.1, 0.6, 0.5), None)  # dominant bubble over the face
    assert is_mask_risk(maps)
    before = np.asarray(img.convert("RGB"), dtype=int).copy()
    out = clean_bubbles(img, maps)
    assert out is img
    assert (np.asarray(out, dtype=int) == before).all()  # pixel-identical
    assert list(getattr(out, "painted_boxes", [])) == []


def test_mask_risk_panel_dropped_not_painted(monkeypatch):
    # verify_chapter drops a mask-risk panel with no clean crop instead of masking it.
    maps = _face_maps((0.05, 0.1, 0.6, 0.5), None)
    assert is_mask_risk(maps)

    def fake_maps(img, options=None):
        return maps

    def fake_pick(img, shapes, m=None):
        return {"shape": 0.75, "box": (0.0, 0.0, 1.0, 1.0), "pad_y": 0.0,
                "problems": ["mask_risk", "lettering"], "clean": False}

    monkeypatch.setattr(verify_mod, "_panel_maps", fake_maps)
    monkeypatch.setattr(verify_mod, "pick_card", fake_pick)
    panels = [{"id": "P001-01", "width": 300, "height": 400, "text": ""}]
    report = verify_mod.verify_chapter(panels, lambda p: Image.new("RGB", (300, 400)),
                                       [0.75], root=None, chapter=None, log=lambda *a: None)
    assert report["panels"]["P001-01"]["action"] == "dropped"
    assert report["dropped"][0]["panel"] == "P001-01"
    assert "mask" in report["dropped"][0]["reason"]


def test_bubble_panel_with_no_clean_crop_kept_not_painted_when_figure_present(monkeypatch):
    # Painting removed: a panel whose best crop still shows an ordinary
    # (non-mask-risk) bubble is never painted. F2 hardening: when a figure
    # is confidently present, it is KEPT (flagged for review), not dropped
    # -- a bubble beside a drawn character is normal paneling. A bubble
    # overlapping the face specifically still drops (probs=["face_bubble"],
    # see the mask-risk test above).
    def fake_maps(img, options=None):
        return {"_head": {"top": 0.1, "bottom": 0.4, "source": "skin", "confident": True,
                          "figure": True, "keep_top": None},
                "bubble_regions": [{"box": (0.2, 0.1, 0.8, 0.4), "fill": 0.9}],
                "painted": [], "edge_rows": (0.0, 0.0)}

    def fake_pick(img, shapes, m=None):
        return {"shape": 0.75, "box": (0.0, 0.0, 1.0, 1.0), "pad_y": 0.0,
                "problems": ["lettering"], "clean": False}

    monkeypatch.setattr(verify_mod, "_panel_maps", fake_maps)
    monkeypatch.setattr(verify_mod, "is_mask_risk", lambda maps: False)
    monkeypatch.setattr(verify_mod, "pick_card", fake_pick)
    panels = [{"id": "P001-01", "width": 300, "height": 400, "text": ""}]
    report = verify_mod.verify_chapter(panels, lambda p: Image.new("RGB", (300, 400)),
                                       [0.75], root=None, chapter=None, log=lambda *a: None)
    assert report["panels"]["P001-01"]["action"] == "kept (check lettering)"
    assert report["dropped"] == []
    assert report["bubble_beside_figure"] == ["P001-01"]


# ------------------------------------------------------------------ transitions


def test_transition_enter_mapping():
    assert transition_enter("cut") == "cut"
    assert transition_enter("fade") == "fade_in"
    assert transition_enter("slide") == "slide_left"
    assert transition_enter("zoom") == "zoom_in"
    assert transition_enter("whip") == "whip_in"
    assert transition_enter("glitch") == "cut"
    assert transition_enter("bogus") == "cut"  # unknown: hard cut, never an animation


def test_plan_transitions_are_valid_and_first_is_cut():
    plan = _plan(120.0)
    assert plan["shots"][0]["transition"] == "cut"
    for s in plan["shots"]:
        assert s["transition"] in TRANSITIONS
    for prev, cur in zip(plan["shots"], plan["shots"][1:]):
        if cur["transition"] == "fade":
            assert prev["exit"] == "fade_out"  # a fade fades the previous shot out


def test_glitch_at_most_once_per_60s():
    sentences = [{"start": i * 3.0, "text": f"Sentence number {i} lands hard here today.",
                  "panels": [f"P001-0{(i % 5) + 1}"], "cues": ["impact"]}
                 for i in range(60)]
    plan = _plan(180.0, sentences=sentences)
    glitches = sorted(e["t"] for s in plan["shots"] for e in s.get("effects") or []
                      if e.get("type") == "glitch")
    for a, b in zip(glitches, glitches[1:]):
        assert b - a >= 60.0


def test_transitions_are_frame_exact_no_flicker():
    fmt = Format(320, 180, 30)
    for enter in ("fade_in", "slide_left", "zoom_in", "whip_in", "blur_in"):
        shot = _shot(end=2, cards=[{"panel": "A", "crop": [0, 0, 1, 1], "enter": enter,
                                   "at": 0, "motion": "push_in"}])
        r = ShotRenderer(shot, {"A": _gradient_panel()}, fmt)
        prev = None
        for k in range(0, 25):  # every frame of the entrance window
            frame = r.frame_array(k / 30.0)
            assert frame.shape == (180, 320, 3) and frame.dtype.name == "uint8"
            if k >= 3:
                assert frame.mean() > 1  # never a black / dropped frame mid-entrance
            if prev is not None:
                assert abs(frame.astype(int) - prev.astype(int)).mean() < 120  # no 0.5s flicker jump
            prev = frame


def test_glitch_effect_is_temporary_rgb_split():
    fmt = Format(320, 180, 30)
    shot = _shot(end=4, effects=[{"type": "glitch", "t": 1.0}])
    r = ShotRenderer(shot, {"A": _gradient_panel()}, fmt)
    during = r.frame_array(1.05)
    after = r.frame_array(1.5)
    assert (during != after).any()  # the split is visible on the beat...
    # ...and gone 0.2s later (modulo the continuous push_in drift, compare
    # against a no-glitch renderer at the same times)
    plain = ShotRenderer(_shot(end=4), {"A": _gradient_panel()}, fmt)
    assert (r.frame_array(1.5) == plain.frame_array(1.5)).all()


# ------------------------------------------------------------------ captions


def test_caption_chunks_are_3_to_5_words():
    text = "The streamer walks into the dungeon and faces the boss alone today in silence"
    chunks = split_sentence_to_chunks(text)
    assert chunks
    assert all(3 <= len(c.split()) <= 5 for c in chunks)
    assert " ".join(chunks) == text
    assert split_sentence_to_chunks("Short sentence here.") == ["Short sentence here."]
    assert split_sentence_to_chunks("Six words make two chunks here") == ["Six words make", "two chunks here"]


def test_build_captions_spread_across_shot():
    shots = [{"id": "S001", "start": 0.0, "end": 6.0,
              "sentence": "The streamer walks into the dungeon and faces the boss alone today"}]
    caps = build_captions(shots)
    assert len(caps) == 3
    assert caps[0]["start"] == 0.0 and caps[-1]["end"] == 6.0
    assert all(c["end"] > c["start"] for c in caps)
    assert all(c["key"].lower() in c["text"].lower() for c in caps)
    assert validate_captions(caps, 6.0) == []


def test_write_ass_key_word_yellow_bottom_band(tmp_path):
    caps = [{"text": "The streamer walks into", "key": "streamer", "start": 0.0, "end": 2.0, "shot": "S001"},
            {"text": "the dungeon and faces", "key": "dungeon", "start": 2.0, "end": 4.0, "shot": "S001"}]
    ass = write_ass(caps, tmp_path / "captions.ass")
    text = ass.read_text(encoding="utf-8")
    assert "{\\c&H00FFFF&}streamer{\\c&HFFFFFF&}" in text  # key word yellow
    assert "Alignment" in text and ",2," in text  # bottom centre
    assert "Dialogue: 0,0:00:00.00,0:00:02.00" in text


def test_validate_captions_catches_long_chunk():
    caps = [{"text": "one two three four five six", "key": "three", "start": 0.0, "end": 2.0, "shot": "S001"}]
    assert validate_captions(caps, 2.0)  # 6 words: over the 5-word cap


def test_plan_carries_captions_by_default_and_off_when_configured():
    sentences = [{"start": i * 3.0, "text": f"Sentence number {i} lands hard here today.",
                  "panels": ["P001-01"], "cues": []} for i in range(10)]
    plan = _plan(30.0, sentences=sentences)
    assert plan["captions_enabled"] is True
    assert plan["captions"]  # full text, not keyword-only
    assert validate_captions(plan["captions"], plan["duration"]) == []
    off = dict(VCFG)
    off["captions"] = False
    plan_off = _plan(30.0, sentences=sentences, vcfg=off)
    assert plan_off["captions_enabled"] is False and plan_off["captions"] == []


# ------------------------------------------------------------------ beat sync


def test_beat_sync_moves_cut_at_most_015s():
    shots = [{"id": f"S{i + 1:03d}", "start": float(i * 3), "end": float((i + 1) * 3),
              "cards": [], "sentence": f"Sentence {i}."} for i in range(5)]
    beats = [2.9, 6.1, 8.9, 12.2]  # each near a cut, one far (12.2 vs 12.0 is 0.2 away)
    out, info = snap_cuts_to_beats(shots, beats)
    assert info["enabled"] is True
    for old, new in zip(shots, out):
        assert abs(new["start"] - old["start"]) <= 0.15 + 1e-9
        assert abs(new["end"] - old["end"]) <= 0.15 + 1e-9
    assert out[0]["start"] == 0.0 and out[-1]["end"] == 15.0  # first/last never move
    assert all(out[i]["end"] == out[i + 1]["start"] for i in range(len(out) - 1))  # still back to back


def test_beat_sync_no_beats_is_noop():
    shots = [{"id": "S001", "start": 0.0, "end": 3.0, "cards": []},
             {"id": "S002", "start": 3.0, "end": 6.0, "cards": []}]
    out, info = snap_cuts_to_beats(shots, None)
    assert info["enabled"] is False and out == shots
    out, info = snap_cuts_to_beats(shots, [])
    assert info["enabled"] is False
    # Beats far from every cut: nothing moves.
    out, info = snap_cuts_to_beats(shots, [100.0])
    assert info["moved"] == 0 and out[1]["start"] == 3.0


def test_resolve_beats_prefers_config_then_file(tmp_path):
    assert resolve_beats({"beats": [3.0, 1.0]}, None) == [1.0, 3.0]
    assert resolve_beats({}, tmp_path) is None
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "beats.json").write_text('{"beats": [2.5, 0.5]}', encoding="utf-8")
    assert resolve_beats({}, tmp_path) == [0.5, 2.5]


# ------------------------------------------------------------------ SFX / music fallback


def test_sfx_missing_dir_falls_back_silent(tmp_path):
    found, missing = resolve_sfx_files(tmp_path, "sfx", kinds=tuple(SFX_ALL))
    assert found == {} and set(missing) == set(SFX_ALL)  # all seven, none invented
    found, missing = resolve_sfx_files(tmp_path, None, kinds=tuple(SFX_ALL))
    assert found == {} and missing == []


def test_plan_sfx_capcut_extras():
    shots = [
        {"start": 0.0, "end": 4.0, "sentence": "He wins, but suddenly the gate opens wide.",
         "effects": [{"type": "flash", "t": 1.0}]},
        {"start": 4.0, "end": 10.5, "sentence": "A long reveal of the true boss chamber.",
         "effects": [{"type": "flash", "t": 5.0}]},
        {"start": 10.5, "end": 13.0, "sentence": "The sword slams into the ground.",
         "effects": []},
    ]
    events = plan_sfx(shots, [])
    by_type = {e["type"] for e in events}
    assert "scratch" in by_type  # twist reveal
    assert "rumble" in by_type  # long reveal > 5s
    assert "hit" in by_type  # impact sentence, no shake of its own
    assert "sting" in by_type  # flash still maps to sting


def test_music_missing_renders_without_it(tmp_path):
    assert resolve_music({}, tmp_path) is None
    assert resolve_music({"music": None}, tmp_path) is None


def test_grade_subtle_off_by_default():
    plan = _plan(30.0)
    assert all("grade" not in s for s in plan["shots"])
    graded = dict(VCFG)
    graded["grade"] = "subtle"
    plan_g = _plan(30.0, vcfg=graded)
    assert all(s.get("grade") == "subtle" for s in plan_g["shots"])
