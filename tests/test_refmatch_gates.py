"""Reference-match gates (the refmatch mission, 2026-10-24).

Four acceptance gates, straight from the measured reference spec:
1. Pre-processing: after 15px gap slicing + ink-to-ink trim, a panel
   asset carries NO white/black margin run >= 15px (the
   no-glued-panels rule) — gutter strips slice, exterior margins crop.
2. Composition: every fitted foreground that is not at the upscale cap
   touches a pair of frame walls (top/bottom for height-fit kinds,
   left/right for width-fit kinds); a drifting pillar's art stays
   beyond both walls for the whole span.
3. Motion routing: aspect (w/h) < 0.72 -> vertical drift at the pinned
   constant speed; >= 0.72 -> zoom; >= 1.6 (fill) and tall strips
   (h/w >= 1.6, traverse) as measured.
4. Audio continuity: the master narration runs UNBROKEN under a title
   card — a continuous tone into a plan with a title-card shot renders
   to an output whose audio stream contains zero silence and whose
   length matches the video.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.sessions import SessionStore  # noqa: E402
from manhwa_ocr.video.frames import Format, PanelFitView, format_from  # noqa: E402
from manhwa_ocr.video.plan import (PF_DRIFT_SPEED, PF_MAX_UPSCALE,  # noqa: E402
                                   panelfit_fit, panelfit_spec)
from manhwa_ocr.video.trim import (TRIM2_GAP_MIN_PX, apply_trim_v2,  # noqa: E402
                                   compute_trim_v2, slice_gaps)

FMT: Format = format_from("16:9", 30)   # 1920x1080


def _ffmpeg_ok() -> bool:
    try:
        from manhwa_ocr.video.ffmpeg import ffmpeg_exe

        ffmpeg_exe()
        return True
    except Exception:  # noqa: BLE001
        return False


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_ok(), reason="FFmpeg not available")


# ------------------------------------------------- gate 1: margin-free assets

def _art(d: ImageDraw.ImageDraw, y0: int, y1: int, colour, w: int) -> None:
    """A textured art block: no interior row is uniformly pure white/black."""
    d.rectangle((0, y0, w - 1, y1 - 1), fill=colour)
    for x in range(0, w, 7):
        d.line((x, y0, x, y1 - 1), fill=tuple(c // 2 for c in colour))


def _glued_strip() -> Image.Image:
    """300x460 strip: exterior white margins, three art blocks, one 30px
    WHITE gap and one 20px BLACK gap between them (all interior gaps
    >= TRIM2_GAP_MIN_PX)."""
    w, h = 300, 460
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    _art(d, 40, 170, (168, 96, 66), w)      # panel A (40px white margin above)
    # rows 170-200: 30px white gutter (interior)
    _art(d, 200, 330, (66, 110, 168), w)    # panel B
    d.rectangle((0, 330, w - 1, 349), fill=(0, 0, 0))   # 20px black gutter
    _art(d, 350, 430, (110, 140, 90), w)    # panel C (30px white margin below)
    return img


def _margin_row_runs(arr: np.ndarray, *, white_min: int = 242, black_max: int = 13):
    """(max white run, max black run) over rows that are >= 99.5% margin."""
    white = (arr.min(axis=2) >= white_min).mean(axis=1) >= 0.995
    black = (arr.max(axis=2) <= black_max).mean(axis=1) >= 0.995

    def longest(mask):
        best = cur = 0
        for v in mask:
            cur = cur + 1 if v else 0
            best = max(best, cur)
        return best

    return longest(white), longest(black)


def test_gutter_strips_slice_at_every_gap_over_15px():
    pieces = slice_gaps(_glued_strip())
    assert len(pieces) == 3                     # the white AND the black gutter both cut
    assert pieces == [(0, 170), (200, 330), (350, 460)]


def test_sliced_assets_carry_no_margin_runs_over_15px():
    img = _glued_strip()
    assets = []
    for y0, y1 in slice_gaps(img):
        res = compute_trim_v2(img.crop((0, y0, img.width, y1)))
        assets.append(apply_trim_v2(img.crop((0, y0, img.width, y1)), res))
    assert len(assets) == 3
    for a in assets:
        arr = np.asarray(a.convert("RGB"))
        w_run, b_run = _margin_row_runs(arr)
        assert w_run < TRIM2_GAP_MIN_PX and b_run < TRIM2_GAP_MIN_PX
        # ink-to-ink: the first and last rows of the asset carry art
        assert not _margin_row_runs(arr[:1])[0] and not _margin_row_runs(arr[-1:])[0]


def test_gap_under_15px_is_not_a_slice_point():
    # A thin white break inside the art is NOT a gutter (the floor is 15px).
    img = Image.new("RGB", (300, 200), (255, 255, 255))
    d = ImageDraw.Draw(img)
    _art(d, 0, 100, (150, 80, 80), 300)
    d.rectangle((0, 100, 299, 108), fill=(255, 255, 255))    # 9px white break
    _art(d, 109, 200, (80, 80, 150), 300)
    assert slice_gaps(img) == [(0, 200)]


# ---------------------------------------------- gate 2: wall contact on frame

CONTACT_CASES = [  # (pw, ph, kind, wall pair it must touch)
    (900, 1200, "zoom", "tb"),      # height-fit pillar: base_h == 1080
    (900, 1400, "drift", "tb"),     # oversized pillar: taller than the frame
    (2000, 900, "fill", "both"),    # cover: fills every wall
    (900, 2400, "drift", "tb"),     # tall strip: height-fit + travel, never width-fit
]


@pytest.mark.parametrize("pw,ph,kind,walls", CONTACT_CASES)
def test_fitted_foreground_touches_the_frame_walls(pw, ph, kind, walls):
    k, bw, bh, s = panelfit_fit(pw, ph, FMT, dur=3.0)
    assert k == kind and s < PF_MAX_UPSCALE          # a genuine fit, not the cap
    if walls in ("tb", "both"):
        assert bh >= FMT.height - 0.5
    if walls in ("lr", "both"):
        assert bw >= FMT.width - 0.5


def test_capped_fit_floats_instead_of_stretching():
    # The ONE exception: a panel whose wall-touching scale would pass the
    # upscale cap sits smaller (never stretched) — the quality guard wins.
    k, bw, bh, s = panelfit_fit(300, 400, FMT)
    assert s == PF_MAX_UPSCALE and bh < FMT.height and bw < FMT.width


def _textured(pw: int, ph: int) -> Image.Image:
    img = Image.new("RGB", (pw, ph), (140, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, pw, 11):
        d.line((x, 0, x, ph), fill=(60, 40, 30))
    return img


def test_drift_art_stays_beyond_both_walls_for_the_whole_span():
    img = _textured(900, 1400)
    spec = panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=4.0)
    v = PanelFitView(img, spec, {"at": 0.0}, 0.0, 4.0, FMT)
    for t in (0.0, 1.0, 2.0, 3.0, 3.9):
        dx, dy, dw, dh = v._place(t)
        assert dy <= 0.5 and dy + dh >= FMT.height - 0.5    # art beyond top AND bottom
        assert dw / dh == pytest.approx(900 / 1400, rel=0.01)  # never stretched


# ------------------------------------------------- gate 3: motion routing

ROUTING_CASES = [  # (pw, ph, expected kind)
    (719, 1000, "drift"),     # aspect 0.719 < 0.72
    (720, 1000, "zoom"),      # aspect 0.720 — NOT < 0.72
    (725, 1000, "zoom"),
    (900, 1400, "drift"),
    (900, 1200, "zoom"),
    (1600, 1000, "fill"),     # aspect exactly 1.6
    (900, 2400, "drift"),     # tall strip: the drift family, never a fly-through
]


@pytest.mark.parametrize("pw,ph,kind", ROUTING_CASES)
def test_motion_routes_on_the_aspect_threshold(pw, ph, kind):
    assert panelfit_fit(pw, ph, FMT, dur=2.0)[0] == kind


def test_drift_speed_is_the_pinned_constant():
    sp = panelfit_spec(719, 1000, "pan_down", FMT, {}, dur=2.0)
    assert sp["kind"] == "drift" and sp["drift_rate"] == pytest.approx(PF_DRIFT_SPEED)
    z = panelfit_spec(720, 1000, "push_in", FMT, {}, dur=2.0)
    assert z["kind"] == "zoom" and z["zoom_rate"] == pytest.approx(0.05)


# --------------------------------------- gate 4: unbroken audio under cards

def _page(path: Path, w: int, blocks, h: int) -> Path:
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for y0, y1, colour in blocks:
        _art(d, y0, y1, colour, w)
    img.save(path)
    return path


@needs_ffmpeg
def test_title_card_overlays_unbroken_narration(tmp_path):
    """The full panelfit path: a plan with a split-flap title-card shot
    renders with its narration stream INTACT — one continuous tone in,
    zero silence out, container length == narration length."""
    from manhwa_ocr.video import build as vb, cues
    from manhwa_ocr.video import align_engine
    from manhwa_ocr.video.ffmpeg import ffmpeg_exe, run, video_info
    from manhwa_ocr.video.media import pages_dir
    from manhwa_ocr.video.panels import build_panels
    from manhwa_ocr.video.plan import plan_path

    store = SessionStore(tmp_path)
    sess = store.create("RefMatch")
    sess.add_links(["https://x.com/1"])
    (sess.path / "chapter_1.md").write_text("# 1\n\nstuff\n")
    pages = pages_dir(sess.path, "1")
    pages.mkdir(parents=True)
    _page(pages / "page_001.png", 300, [(40, 400, (200, 60, 60)),
                                        (460, 820, (60, 160, 60))], 900)
    _page(pages / "page_002.png", 300, [(0, 300, (60, 60, 200)),
                                        (360, 700, (180, 160, 40))], 760)
    (sess.path / "audio").mkdir()
    run(["-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-c:a", "libmp3lame",
         str(sess.path / "audio" / "chapter_1.mp3")])
    cfg = load_config(series__workdir=str(sess.path), series__name="RefMatch")
    cfg.video["mode"] = "panelfit"
    cfg.video["workers"] = 1
    cfg.video["min_hold"] = 1.0
    cues.write_outputs(
        sess.path, "1",
        "{P001-01} Age four. {P001-01} Week six of possession. "
        "{P001-02} The duke walks into the hall and the guards snap to attention.", None)
    build_panels(sess.path, "1", log=lambda *_: None)
    align_engine.build_alignment(sess.path, "1", cfg, sync="off", log=lambda *_: None)
    vb.make_plan(sess, "1", cfg, sync="off", log=lambda *_: None)
    plan = json.loads(plan_path(sess.path, "1").read_text())

    # A title-card shot exists, and the card CONSUMED plan time only: the
    # shots still tile [0, duration] with no gap and no overlap.
    assert plan["panelfit_title_cards"], "the timeline cue must become a card"
    shots = sorted(plan["shots"], key=lambda s: float(s["start"]))
    assert float(shots[0]["start"]) == 0.0
    assert float(shots[-1]["end"]) == pytest.approx(float(plan["duration"]), abs=0.01)
    for a, b in zip(shots, shots[1:]):
        assert float(b["start"]) == pytest.approx(float(a["end"]), abs=0.01)
    # Captions are OFF in this mode even though the text carries sentences.
    assert plan["captions"] == [] and plan["captions_enabled"] is False

    out = vb.render_chapter(sess, "1", cfg, preview=True, allow_estimated=True,
                            log=lambda *_: None)
    info = video_info(out)
    assert info["audio"], "the narration track must be present in the output"
    assert info["duration"] == pytest.approx(10.0, abs=0.3)
    # The narration is a continuous 440Hz tone: any cut, split or mute
    # (e.g. at the title card) would show as detected silence. Zero
    # silence_start events == the master audio ran unbroken end to end.
    probe = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(out),
         "-map", "0:a", "-af", "silencedetect=noise=-45dB:d=0.20", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "silence_start" not in (probe.stderr or ""), \
        f"the output audio contains silence — the narration was cut:\n{probe.stderr[-800:]}"
