"""Title Card Generator (video/titlecards.py + plan.insert_title_cards):
timeline cues in the narration become split-flap cards, measured from
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
from manhwa_ocr.video.plan import build_plan, insert_title_cards, validate
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


# --------------------------------------------------------------- plan insertion

def _shot(i, start, end, pid="P001-01"):
    return {"id": f"S{i:03d}", "start": float(start), "end": float(end),
            "layout": "single", "background": "blur", "exit": "cut",
            "transition": "cut", "effects": [], "callouts": [],
            "cards": [{"panel": pid, "crop": [0, 0, 1, 1], "enter": "cut",
                       "at": 0.0, "motion": "push_in"}],
            "sentence": None, "panelfit": True, "caption_bar": False}


def _tiles(shots):
    return [(float(s["start"]), float(s["end"])) for s in shots]


def test_insert_card_consumes_the_following_shots_head():
    shots = [_shot(1, 0, 6), _shot(2, 6, 12)]
    wins = [{"text": "Age 4. Week 6 of possession.", "start": 6.0, "end": 7.5}]
    out, report = insert_title_cards(shots, wins, {})
    assert _tiles(out) == [(0.0, 6.0), (6.0, 7.5), (7.5, 12.0)]   # clamp(1.5) = 1.5s card
    card = out[1]
    assert card["title_card"] == {"label": "Age", "number": 4, "line": "WEEK 6 OF POSSESSION."}
    assert card["cards"] == [] and card["id"] == "S002"
    assert report == [{"id": "S002", "at": 6.0, "duration": 1.5, "label": "Age",
                        "number": 4, "line": "WEEK 6 OF POSSESSION."}]


def test_insert_card_long_cue_clamps_to_the_measured_max():
    shots = [_shot(1, 0, 6), _shot(2, 6, 14)]
    wins = [{"text": "Age 8. Year 4 of possession.", "start": 6.0, "end": 11.0}]
    out, report = insert_title_cards(shots, wins, {})
    assert _tiles(out) == [(0.0, 6.0), (6.0, 8.1), (8.1, 14.0)]
    assert report[0]["duration"] == 2.1


def test_insert_merges_an_age_fragment_with_the_next_cue():
    shots = [_shot(1, 0, 20), _shot(2, 20, 30)]
    wins = [{"text": "Age 4.", "start": 20.0, "end": 20.8},
            {"text": "Week 6 of possession.", "start": 20.9, "end": 22.0}]
    out, report = insert_title_cards(shots, wins, {})
    cards = [s for s in out if s.get("title_card")]
    assert len(cards) == 1 and cards[0]["title_card"]["line"] == "WEEK 6 OF POSSESSION."
    assert (float(cards[0]["start"]), float(cards[0]["end"])) == (20.0, 22.0)


def test_insert_no_cues_leaves_the_plan_untouched():
    shots = [_shot(1, 0, 6), _shot(2, 6, 12)]
    wins = [{"text": "He trained for 4 weeks.", "start": 6.0, "end": 8.0}]
    out, report = insert_title_cards(shots, wins, {})
    assert out == shots and report == []
    out2, report2 = insert_title_cards(shots, wins, {"title_cards": False})
    assert out2 == shots and report2 == []


# --------------------------------------------------------------- build + validate + audit

def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 11):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


PANELS = [{"id": "P001-01", "width": 900, "height": 1200, "text": ""},
          {"id": "P001-02", "width": 900, "height": 1000, "text": ""}]


def test_build_plan_with_a_cue_sentence_gets_a_card_shot():
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
    cards = [s for s in plan["shots"] if s.get("title_card")]
    assert len(cards) == 1
    # clamp(1.5) = 1.5s; the start snaps to the cue shot's own start
    # (shots open `lead` — 0.15s — before their sentence).
    assert cards[0]["start"] == pytest.approx(3.85, abs=0.01)
    assert cards[0]["end"] - cards[0]["start"] == pytest.approx(1.5)
    assert cards[0]["title_card"]["line"] == "WEEK 6 OF POSSESSION."
    assert plan["panelfit_title_cards"][0]["id"] == cards[0]["id"]
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
        {"id": "S002", "start": 2.0, "end": 4.0, "layout": "single", "background": "black",
         "cards": [], "title_card": {"label": "Age", "number": 4,
                                     "line": "WEEK 6 OF POSSESSION."}},
        {"id": "S003", "start": 4.0, "end": 6.0, "layout": "single", "background": "blur",
         "cards": [{"panel": "P2", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                    "motion": "push_in"}]},
    ]
    plan = {"chapter": "1", "duration": 6.0, "mode": "panelfit", "shots": shots}
    rep = syncaudit.audit(plan, {"segments": segs}, [])
    # The cue sentence is fully "covered" by its card, and the next
    # sentence's panel arriving at the card's end is not a late cut.
    cov = {c["sentence"]: c for c in rep["tagged_coverage"]}
    assert cov[2]["coverage"] == 1.0 and cov[2]["pass"] is True
    assert rep["late_cuts"] == []
    assert rep["gate"]["pass"] is True


# ------------------------------------- B1e: a card is an overlay (E7)

def _ffmpeg_ok() -> bool:
    try:
        from manhwa_ocr.video.ffmpeg import ffmpeg_exe

        ffmpeg_exe()
        return True
    except Exception:  # noqa: BLE001
        return False


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_ok(), reason="FFmpeg not available")


@needs_ffmpeg
def test_title_card_render_keeps_the_audio_continuous(tmp_path):
    """E7: a title card replaces only the VISUAL for its window. The
    production render muxes the chapter narration as ONE continuous
    ffmpeg input (render._mux), so the audio under a card cannot be
    paused, split or muted by construction — proven here end to end:
    a 660 Hz tone sits under the card window (standing in for the
    card sentence's own words) between two 440 Hz stretches, and it
    must come through at full level with no silent gap anywhere."""
    import subprocess

    from manhwa_ocr.sessions import SessionStore
    from manhwa_ocr.video import render as render_mod
    from manhwa_ocr.video.ffmpeg import run as frun
    from manhwa_ocr.video.media import pages_dir
    from manhwa_ocr.video.panels import build_panels

    store = SessionStore(tmp_path)
    sess = store.create("CardAudio")
    sess.add_links(["https://x.com/1"])
    (sess.path / "chapter_1.md").write_text("# 1\n\nstuff\n")
    folder = pages_dir(sess.path, "1")
    folder.mkdir(parents=True, exist_ok=True)
    page = Image.new("RGB", (300, 1000), (255, 255, 255))
    d = ImageDraw.Draw(page)
    d.rectangle((10, 40, 290, 400), fill=(200, 60, 60))
    d.rectangle((10, 460, 290, 820), fill=(60, 60, 200))
    page.save(folder / "page_001.png")
    panels_data = build_panels(sess.path, "1", log=lambda *_: None)
    panel = panels_data["panels"][0]
    pid = panel["id"]
    spec = plan_mod.panelfit_spec(panel["width"], panel["height"], "push_in", FMT, {}, dur=2.0)
    (sess.path / "audio").mkdir(exist_ok=True)
    frun(["-f", "lavfi", "-i", "sine=frequency=440:duration=2",
          "-f", "lavfi", "-i", "sine=frequency=660:duration=2",
          "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
          "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]",
          "-map", "[out]", "-c:a", "pcm_s16le",
          str(sess.path / "audio" / "chapter_1.wav")])

    def pshot(i, a, b):
        return {"id": f"S{i:03d}", "start": a, "end": b, "layout": "single",
                "background": "blur", "exit": "cut", "transition": "cut",
                "panelfit": True, "caption_bar": False, "effects": [], "callouts": [],
                "sentence": None,
                "cards": [{"panel": pid, "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                           "motion": "push_in", "panelfit": spec}]}

    card_shot = {"id": "S002", "start": 2.0, "end": 4.0, "layout": "single",
                 "background": "blur", "exit": "cut", "transition": "cut",
                 "effects": [], "callouts": [], "sentence": None, "cards": [],
                 "title_card": {"label": "Age", "number": 4, "line": "WEEK 6 OF POSSESSION."}}
    plan = {"version": plan_mod.PLAN_VERSION, "chapter": "1", "duration": 6.0,
            "format": "16:9", "fps": 30, "mode": "panelfit",
            "audio": "audio/chapter_1.wav",
            "shots": [pshot(1, 0.0, 2.0), card_shot, pshot(3, 4.0, 6.0)],
            "chapter_cards": [], "sfx": [], "captions": []}
    out = render_mod.render(sess.path, "1", plan, VCFG, preview=True,
                            workers=1, log=lambda *_: None)
    # resolve ffmpeg the way the program does (PATH first, then the
    # imageio-ffmpeg bundle) -- a bare "ffmpeg" only works when it is on PATH
    from manhwa_ocr.video.ffmpeg import ffmpeg_exe
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(out), "-map", "0:a",
                          "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.float32)
    assert len(x) == pytest.approx(6 * 8000, abs=1200)

    def dominant(a, b):
        seg = x[int(a * 8000):int(b * 8000)]
        seg = seg * np.hanning(len(seg))
        mag = np.abs(np.fft.rfft(seg))
        return float(np.fft.rfftfreq(len(seg), 1 / 8000)[int(np.argmax(mag))])

    def rms(a, b):
        seg = x[int(a * 8000):int(b * 8000)]
        return float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0

    assert dominant(0.3, 1.7) == pytest.approx(440, abs=30)
    assert dominant(2.3, 3.7) == pytest.approx(660, abs=30)   # the tone UNDER the card
    assert rms(2.3, 3.7) > 0.05                                # at level, never muted
    assert dominant(4.3, 5.7) == pytest.approx(440, abs=30)
    step = 400  # 50 ms windows: no silent gap anywhere before the
    # mux's one global fade-out at the very end (which starts at 5.2s)
    levels = [float(np.sqrt(np.mean(x[i:i + step] ** 2)))
              for i in range(0, len(x) - step, step)]
    assert min(levels[2:100]) > 0.02
