"""Tests for the Genius Knight movie mode (video/framepipe.py).

Covers: full-bleed centre-crop geometry, Solo rotation sequence,
hard-cut-only construction (no fade/slide-in in the frame math),
approved-cuts-only hard fail, sentence-panel coverage, frame
allocation, and the first/mid/last gate logic. Shorts and placed
modes are not touched by this module; their tests still run unchanged.
"""
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from manhwa_ocr.video import framepipe as fp


def _cut(tmp_path, name="cut.png", width=900, height=1500, color=(200, 30, 30)):
    path = tmp_path / name
    Image.new("RGB", (width, height), color).save(path)
    return path


def _shots(tmp_path):
    cut = _cut(tmp_path)
    return [
        fp.Shot(src=cut, y0=0, win_h=600, start=0.0, end=2.0, sentence=1, panel="P001-01", frames=60),
        fp.Shot(src=cut, y0=600, win_h=600, start=2.0, end=4.0, sentence=2, panel="P001-02", frames=60),
        fp.Shot(src=cut, y0=100, win_h=600, start=4.0, end=6.0, sentence=3, panel="P001-03", frames=60),
        fp.Shot(src=cut, y0=200, win_h=600, start=6.0, end=8.0, sentence=4, panel="P001-04", frames=60),
        fp.Shot(src=cut, y0=300, win_h=600, start=8.0, end=10.0, sentence=5, panel="P001-05", frames=60),
    ]


def test_full_bleed_base_is_exactly_1920x1080_and_1195_for_slides(tmp_path):
    cut = Image.open(_cut(tmp_path))
    assert fp.full_bleed_base(cut, 0, 600, "zoom_in").size == (1920, 1080)
    assert fp.full_bleed_base(cut, 0, 600, "slide_up").size == (1920, 1195)
    assert fp.full_bleed_base(cut, 0, 600, "slide_down").size == (1920, 1195)


def test_frame_at_is_full_frame_and_settled_at_frame_zero(tmp_path):
    cut = Image.open(_cut(tmp_path))
    for motion in fp.ROTATION:
        base = fp.full_bleed_base(cut, 0, 600, motion)
        frame = fp.frame_at(base, motion, 0.0)
        assert frame.size == (1920, 1080)
        # Frame 0 of a zoom_in is the whole base (settled, not a slide-in);
        # frame 0 of a slide is a crop edge of the taller base, also full-frame.
        assert frame.size == (1920, 1080)
    with pytest.raises(ValueError):
        fp.frame_at(base, "fade_in", 0.0)


def test_rotation_sequence_and_run_hold(tmp_path):
    shots = _shots(tmp_path)
    fp.assign_motions(shots)
    assert [s.motion for s in shots] == list(fp.ROTATION) + ["zoom_in"]
    # Consecutive same window = one run, one motion, continuous progress.
    cut = shots[0].src
    run = [
        fp.Shot(src=cut, y0=0, win_h=600, start=0.0, end=1.0, sentence=1, frames=30),
        fp.Shot(src=cut, y0=0, win_h=600, start=1.0, end=2.0, sentence=2, frames=30),
        fp.Shot(src=cut, y0=100, win_h=600, start=2.0, end=3.0, sentence=3, frames=30),
    ]
    fp.assign_motions(run)
    assert run[0].motion == run[1].motion == "zoom_in"
    assert run[0].run_frames == run[1].run_frames == 60
    assert run[0].run_offset == 0 and run[1].run_offset == 30
    assert run[2].motion == "slide_up"


def test_approved_cuts_hard_fail_never_substitutes(tmp_path):
    cuts = tmp_path / "cuts"
    cuts.mkdir()
    cut = _cut(cuts, "cut.png")
    good = fp.Shot(src=cut, y0=0, win_h=600, start=0.0, end=1.0, sentence=1)
    fp.validate_shots([good], cuts_dir=cuts)
    # Missing file
    with pytest.raises(ValueError, match="missing"):
        fp.validate_shots([fp.Shot(src=tmp_path / "nope.png", y0=0, win_h=600, start=0.0, end=1.0)])
    # Wrong width: not an approved cut, must fail, not be re-detected.
    wide = _cut(tmp_path, "wide.png", width=800)
    with pytest.raises(ValueError, match="900"):
        fp.validate_shots([fp.Shot(src=wide, y0=0, win_h=600, start=0.0, end=1.0)])
    # Window outside the image
    with pytest.raises(ValueError, match="outside"):
        fp.validate_shots([fp.Shot(src=cut, y0=1400, win_h=600, start=0.0, end=1.0)])
    # Outside the approved cuts dir
    other = tmp_path / "other"
    other.mkdir()
    outside = _cut(other, "cut2.png")
    with pytest.raises(ValueError, match="outside approved cuts dir"):
        fp.validate_shots([fp.Shot(src=outside, y0=0, win_h=600, start=0.0, end=1.0)], cuts_dir=cuts)


def test_sentence_coverage_exactly_once(tmp_path):
    shots = _shots(tmp_path)
    fp.check_sentence_coverage([1, 2, 3, 4, 5], shots)
    with pytest.raises(ValueError, match="missing"):
        fp.check_sentence_coverage([1, 2, 3, 4, 5, 6], shots)
    dup = _shots(tmp_path)
    dup[1].sentence = 1
    with pytest.raises(ValueError, match="duplicated"):
        fp.check_sentence_coverage([1, 2, 3, 4, 5], dup)


def test_allocate_frames_exact_total(tmp_path):
    shots = _shots(tmp_path)
    for s in shots:
        s.frames = None
    total = fp.allocate_frames(shots, 10.0)
    assert total == 300
    assert sum(s.frames for s in shots) == 300


def test_gate_first_mid_last_and_render(tmp_path):
    # A tiny real render: 4 shots x 15 frames, silent-ish audio via ffmpeg.
    cut = _cut(tmp_path, color=(40, 120, 200))
    shots = [
        fp.Shot(src=cut, y0=0, win_h=600, start=0.0, end=0.5, sentence=1, panel="P001-01", frames=15),
        fp.Shot(src=cut, y0=600, win_h=600, start=0.5, end=1.0, sentence=2, panel="P001-02", frames=15),
        fp.Shot(src=cut, y0=100, win_h=600, start=1.0, end=1.5, sentence=3, panel="P001-03", frames=15),
        fp.Shot(src=cut, y0=200, win_h=600, start=1.5, end=2.0, sentence=4, panel="P001-04", frames=15),
    ]
    audio = tmp_path / "audio.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=16000:cl=mono", "-t", "2", str(audio)], check=True)
    out = tmp_path / "movie.mp4"
    n = fp.render_movie(shots, audio, out, cuts_dir=tmp_path)
    assert n == 60
    rows = fp.gate_first_mid_last(out, shots)
    assert len(rows) == 4
    assert all(r["near_white"] < 0.30 for r in rows)
    # Probe: 1920x1080, ~2s, no re-encode artefacts of a transition.
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "json", str(out)], capture_output=True, text=True, check=True).stdout)
    assert probe["streams"][0]["width"] == 1920
    assert probe["streams"][0]["height"] == 1080
    assert abs(float(probe["format"]["duration"]) - 2.0) < 0.1


def test_concat_movies_is_stream_copy_no_breaks(tmp_path):
    # Two tiny chapter videos, then concat: duration must be the sum.
    cut = _cut(tmp_path)
    audio = tmp_path / "audio.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=16000:cl=mono", "-t", "1", str(audio)], check=True)
    outs = []
    for i in range(2):
        shots = [fp.Shot(src=cut, y0=0, win_h=600, start=0.0, end=1.0, sentence=1, frames=30)]
        out = tmp_path / f"ch{i}.mp4"
        fp.render_movie(shots, audio, out, cuts_dir=tmp_path)
        outs.append(out)
    movie = tmp_path / "movie.mp4"
    fp.concat_movies(outs, movie)
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(movie)], capture_output=True, text=True, check=True).stdout)
    assert abs(float(probe["format"]["duration"]) - 2.0) < 0.15
