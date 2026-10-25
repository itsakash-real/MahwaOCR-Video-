"""Video feature: panels, cues, sync, plans, rendering, clips, compile, dashboard media.

Test pages are synthetic: coloured blocks separated by gutters at known rows,
so panel boundaries can be checked exactly. Rendering tests use tiny plans and
generated tones, and are skipped if FFmpeg isn't available.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.cli import main as cli  # noqa: E402
from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.sessions import SessionStore  # noqa: E402
from manhwa_ocr.util import UserError  # noqa: E402
from manhwa_ocr.video import align_engine, cues, sync  # noqa: E402
from manhwa_ocr.video.frames import ENTER_DUR, Format, ShotRenderer  # noqa: E402
from manhwa_ocr.video.media import audio_path, keep_pages, pages_dir  # noqa: E402
from manhwa_ocr.video.panels import build_panels, detect, load_panels, panel_image  # noqa: E402
from manhwa_ocr.video.plan import build_plan, even_units, hold_groups, validate  # noqa: E402


def _ffmpeg_ok() -> bool:
    try:
        from manhwa_ocr.video.ffmpeg import ffmpeg_exe

        ffmpeg_exe()
        return True
    except Exception:  # noqa: BLE001
        return False


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_ok(), reason="FFmpeg not available")


# ------------------------------------------------------------------ synthetic pages


def _page(path: Path, w: int, blocks, h: int, bg=(255, 255, 255)) -> Path:
    """blocks: [(y0, y1, colour)] painted with texture so rows aren't uniform."""
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    for y0, y1, colour in blocks:
        d.rectangle((10, y0, w - 10, y1 - 1), fill=colour)
        for x in range(10, w - 10, 7):  # vertical stripes: every row is busy
            d.line((x, y0, x, y1 - 1), fill=(colour[0] // 2, colour[1] // 2, colour[2] // 2))
    img.save(path)
    return path


def _strip(folder: Path, bg=(255, 255, 255)) -> list:
    """Two pages; panel 3 straddles the page boundary."""
    folder.mkdir(parents=True, exist_ok=True)
    p1 = _page(folder / "page_001.png", 300, [(40, 400, (200, 60, 60)), (460, 820, (60, 160, 60)),
                                              (880, 1000, (60, 60, 200))], 1000, bg)
    p2 = _page(folder / "page_002.png", 300, [(0, 300, (60, 60, 200)), (360, 700, (180, 160, 40))], 760, bg)
    return [p1, p2]


# ------------------------------------------------------------------ panels


def test_panels_split_at_gutters_and_join_across_pages(tmp_path):
    panels, norm_w = detect(_strip(tmp_path / "pages"))
    assert norm_w == 300
    assert len(panels) == 4
    spanning = [p for p in panels if len(p.segments) == 2]
    assert len(spanning) == 1  # the blue panel continues onto page 2
    assert spanning[0].segments[0]["page"] == 1 and spanning[0].segments[1]["page"] == 2
    assert [p.id for p in panels] == ["P001-01", "P001-02", "P001-03", "P002-01"]


def test_black_gutters_work_too(tmp_path):
    panels, _ = detect(_strip(tmp_path / "pages", bg=(0, 0, 0)))
    assert len(panels) == 4


def test_smooth_gradient_is_not_a_gutter(tmp_path):
    folder = tmp_path / "pages"
    folder.mkdir()
    img = Image.new("RGB", (300, 900), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for y in range(100, 800):  # a sky fading top to bottom, inside one panel
        v = int(60 + 150 * (y - 100) / 700)
        d.line((10, y, 289, y), fill=(v, v, 255))
    img.save(folder / "page_001.png")
    panels, _ = detect([folder / "page_001.png"])
    assert len(panels) == 1


def test_small_fragment_merges_and_credit_page_is_skipped(tmp_path):
    folder = tmp_path / "pages"
    folder.mkdir()
    Image.new("RGB", (1500, 900), (40, 40, 40)).save(folder / "page_000.png")  # landscape credits
    _page(folder / "page_001.png", 300, [(40, 500, (200, 60, 60)), (540, 640, (90, 90, 90)),
                                         (700, 1200, (60, 160, 60))], 1240)
    panels, _ = detect(sorted(folder.glob("*.png")))
    assert len(panels) == 2                       # the 100px fragment merged into a neighbour
    assert all(seg["page"] != 1 for p in panels for seg in p.segments)  # credits page ignored


def test_long_gutterless_stretch_is_split(tmp_path):
    folder = tmp_path / "pages"
    folder.mkdir()
    _page(folder / "page_001.png", 300, [(0, 3000, (120, 80, 160))], 3000)
    panels, _ = detect([folder / "page_001.png"])
    assert len(panels) >= 3
    assert all(p.height <= 3.2 * 300 + 1 for p in panels)


def test_build_panels_writes_json_sheets_and_crops(tmp_path):
    root = tmp_path / "s"
    _strip(pages_dir(root, "1"))
    data = build_panels(root, "1", log=lambda *_: None)
    assert load_panels(root, "1")["fingerprint"] == data["fingerprint"]
    assert list((root / "media" / "ch1").glob("panels_sheet_*.png"))
    crop = panel_image(root, "1", data["panels"][2], data["norm_width"])
    assert crop.height > 300  # stitched from two pages


def test_panel_scenes_come_from_kept_tile_notes(tmp_path):
    from manhwa_ocr.video.media import save_tile_notes

    root = tmp_path / "s"
    _strip(pages_dir(root, "1"))
    save_tile_notes(root, "1", [{"id": "p001_t01", "page": 1, "top": 0, "bottom": 420, "width": 300,
                                 "scene": "a red room", "text": "A: hello"}])
    data = build_panels(root, "1", log=lambda *_: None)
    assert data["panels"][0]["scene"] == "a red room"
    assert "hello" in data["panels"][0]["text"]


def test_finalize_keeps_pages_for_video(tmp_path):
    work_pages = tmp_path / ".work" / "ch3" / "pages"
    _strip(work_pages)
    assert keep_pages(tmp_path, "3", work_pages)
    assert len(list(pages_dir(tmp_path, "3").glob("*.png"))) == 2


def test_audio_lookup_accepts_padding_and_types(tmp_path):
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "chapter_03.m4a").write_bytes(b"x")
    (tmp_path / "audio" / "notes.txt").write_text("x")
    assert audio_path(tmp_path, "3").name == "chapter_03.m4a"
    assert audio_path(tmp_path, "4") is None


# ------------------------------------------------------------------ cues


NARRATION = (
    "{P001-01} The hero wakes up somewhere *unfamiliar*.\n\n"
    "[Panel: wide establishing shot]\n\n"
    "{same} He looks around, confused. {P001-02,P001-03} {impact} Then the ground explodes beneath him! "
    "{beat} {P002-01} Nobody saw that coming."
)


def test_cues_parse_tags_cues_and_emphasis():
    data = cues.parse(NARRATION, {"P001-01", "P001-02", "P001-03", "P002-01"})
    s = data["sentences"]
    assert [x["panels"] for x in s] == [["P001-01"], [], ["P001-02", "P001-03"], ["P002-01"]]
    assert s[1]["same"] is True
    assert s[2]["cues"] == ["impact"] and s[3]["cues"] == ["beat"]
    assert s[0]["emphasis"] == ["unfamiliar"]
    # Honest ratio: the {same} sentence is anchored too, so all 4 count.
    assert data["tagged_ratio"] == 1.0
    assert data["same_sentences"] == 1


def test_unknown_panel_ids_are_reported_not_used():
    data = cues.parse("{P009-09} Something happens here.", {"P001-01"})
    assert data["sentences"][0]["panels"] == []
    assert data["unknown_panels"] == ["P009-09"]


def test_quoted_question_mark_does_not_orphan_its_panel_tag():
    # A quoted ?/! followed by a lowercase continuation is a sub-clause, not
    # a sentence end -- splitting there would orphan the fragment from the
    # tag that opened the sentence.
    data = cues.parse('{P001-01} She asked, "Is it over?" but no one answered. '
                      '{P001-02} Then it happened.', {"P001-01", "P001-02"})
    texts = [s["text"] for s in data["sentences"]]
    assert texts == ['She asked, "Is it over?" but no one answered.', "Then it happened."]
    assert data["sentences"][0]["panels"] == ["P001-01"]


def test_quoted_exclamation_before_a_capitalised_sentence_still_splits():
    # The quote still closes a real sentence boundary when what follows is
    # capitalised -- only the lowercase-continuation case is protected.
    data = cues.parse('{P001-01} He shouted, "Stop!" She froze.', {"P001-01"})
    texts = [s["text"] for s in data["sentences"]]
    assert texts == ['He shouted, "Stop!', "She froze."]


def test_tts_text_has_no_tags_cues_or_markdown():
    text = cues.tts_text(NARRATION)
    assert "{" not in text and "[" not in text and "*" not in text
    assert "The hero wakes up somewhere unfamiliar." in text


def test_recap_writes_clean_md_tts_and_cues(tmp_path):
    from manhwa_ocr.script import RecapBuilder

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "chapter_1.md").write_text("# 1\n\nThings happen in chapter one.\n")
    _strip(pages_dir(ws, "1"))
    b = RecapBuilder(load_config(series__workdir=str(ws), series__name="T",
                                 recap__min_minutes=0, recap__max_minutes=0), log=lambda *_: None)
    prompt = b._prompt_for("1", 6)
    assert "P001-01" in prompt and "{P003-02}" in prompt  # panel list + tagging instructions
    b.prepare(["1"], 6)
    long = NARRATION + " " + "{same} More happens next in the story. " * 15
    (b.task_dir / "parts.json").write_text(json.dumps({"1": {"narration": long, "hook": "{P001-01} x"}}))
    b.finalize()
    md = (ws / "recaps" / "chapter_1.md").read_text(encoding="utf-8")
    assert "{P001" not in md and "[Panel: wide establishing shot]" in md
    assert (ws / "recaps" / "chapter_1.tts.txt").exists()
    assert cues.load_cues(ws, "1")["sentences"][0]["panels"] == ["P001-01"]


def test_untagged_recap_removes_stale_cues(tmp_path):
    cues.write_outputs(tmp_path, "1", "{P001-01} Tagged sentence here.", None)
    assert cues.cues_path(tmp_path, "1").exists()
    cues.write_outputs(tmp_path, "1", "Plain sentence with no tags.", None)
    assert not cues.cues_path(tmp_path, "1").exists()


# ------------------------------------------------------------------ sync alignment


def _words(text: str, start=0.5, per=0.35):
    out, t = [], start
    for w in text.split():
        out.append({"w": w, "start": round(t, 3), "end": round(t + per * 0.8, 3)})
        t += per
    return out


def test_align_exact_speech():
    sents = [{"text": "The hero wakes up."}, {"text": "Then the ground explodes."}]
    res = sync.align(sents, _words("The hero wakes up. Then the ground explodes."), 10.0)
    assert res["matched"] == 1.0
    assert res["sentences"][1]["start"] == pytest.approx(0.5 + 4 * 0.35, abs=0.01)


def test_align_survives_misheard_and_skipped_words():
    sents = [{"text": "Raon draws his sword."}, {"text": "Kim Dokja watches silently."},
             {"text": "The demon king laughs."}]
    heard = _words("Ron draws his sword. Kim dog ja watches. The demon king laughs.")
    res = sync.align(sents, heard, 12.0)
    starts = [s["start"] for s in res["sentences"]]
    assert starts == sorted(starts)
    assert starts[2] == pytest.approx(0.5 + 9 * 0.35, abs=0.4)


def test_align_spelled_numbers_and_emphasis_times():
    sents = [{"text": "He waited 7 years.", "emphasis": ["years"]}]
    res = sync.align(sents, _words("He waited seven years."), 5.0)
    assert res["matched"] == 1.0
    assert res["sentences"][0]["emphasis"] == [pytest.approx(0.5 + 3 * 0.35, abs=0.01)]


def test_align_without_words_estimates_by_length():
    sents = [{"text": "Short."}, {"text": "A much much longer sentence follows here."}]
    res = sync.align(sents, [], 10.0)
    assert res["sentences"][0]["start"] == 0.0
    assert 0 < res["sentences"][1]["start"] < 5


# ------------------------------------------------------------------ plans


PANELS = [{"id": f"P001-0{i}", "width": 300, "height": 300 if i % 2 else 700, "tall": i % 2 == 0, "text": ""}
          for i in range(1, 6)]
VCFG = load_config().video


def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 9):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


def _plan(duration, sentences=None, panels=PANELS, vcfg=VCFG):
    return build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "x"}, duration=duration,
                      audio=None, vcfg=vcfg, image_for=_img_for, sentences=sentences)


def _ids():
    return {p["id"] for p in PANELS}


def test_even_plan_covers_audio_exactly():
    plan = _plan(20.0)
    shots = plan["shots"]
    assert shots[0]["start"] == 0 and shots[-1]["end"] == 20.0
    assert all(b["start"] == pytest.approx(a["end"], abs=0.002) for a, b in zip(shots, shots[1:]))
    assert all(s["exit"] == "cut" for s in shots)
    assert validate(plan, _ids()) == []


def test_even_plan_keeps_a_calm_pace():
    # 40 panels in 120s: every panel shown, each shot held at least min_hold.
    many = [{"id": f"P0{i // 10 + 1:02d}-{i % 10 + 1:02d}", "width": 300, "height": 400, "text": ""}
            for i in range(40)]
    shots, stats = hold_groups(even_units(many, 120.0, VCFG), 3.0)
    durs = [s["end"] - s["start"] for s in shots]
    assert min(durs) >= 3.0 - 0.01
    shown = {p for s in shots for p in s["panels"]}
    assert shown == {p["id"] for p in many}


def test_even_plan_skips_text_only_panels():
    panels = [dict(p, text_only=(i == 1)) for i, p in enumerate(PANELS)]
    used = {u["panels"][0] for u in even_units(panels, 12.0, VCFG)}
    assert "P001-02" not in used


def test_even_plan_never_repeats_or_drops_a_panel_for_long_audio():
    units = even_units(PANELS[:2], 60.0, VCFG)
    assert [u["panels"][0] for u in units] == ["P001-01", "P001-02"]
    assert units[-1]["end"] == pytest.approx(60.0)


def test_synced_plan_follows_sentences_and_places_effects():
    sentences = [
        {"start": 0.4, "text": "a", "panels": ["P001-01"], "cues": [], "emphasis": []},
        {"start": 4.0, "text": "b", "panels": [], "cues": [], "emphasis": []},          # inherits P001-01
        {"start": 8.0, "text": "c", "panels": ["P001-03"], "cues": ["impact"], "emphasis": []},
        {"start": 13.4, "text": "d", "panels": ["P001-05"], "cues": [], "emphasis": []},  # 0.6s: merged into c
        {"start": 14.0, "text": "e", "panels": ["P001-02", "P001-04"], "cues": ["reveal"], "emphasis": []},
    ]
    vcfg = {**VCFG, "effects": {"shake": True, "punch": True, "flash": True}}
    plan = _plan(20.0, sentences, vcfg=vcfg)
    shots = plan["shots"]
    at = lambda t: next(s for s in shots if s["start"] <= t < s["end"])  # noqa: E731

    def newest(t):  # the card that most recently arrived on screen at time t
        shot = at(t)
        here = [c for c in shot["cards"] if shot["start"] + float(c.get("at") or 0) <= t + 1e-6]
        return here[-1]["panel"]

    assert newest(1.0) == "P001-01" and newest(6.0) == "P001-01"        # b has no tag: stays on P001-01
    assert newest(8.0 - VCFG["lead"]) == "P001-03"                       # arrives with sentence c
    assert any(e["type"] == "shake" for e in at(8.0)["effects"])
    assert newest(14.0) in ("P001-02", "P001-04")                        # sentence e
    assert "P001-05" not in {c["panel"] for s in shots if s["start"] < 14.0 for c in s["cards"]}
    assert any(s["layout"] in ("pair", "inset") and any(e["type"] == "flash" for e in s["effects"]) for s in shots)
    assert validate(plan, _ids()) == []


def test_sentence_shows_its_tagged_panels_in_order_and_nothing_else():
    sentences = [{"start": 0.0, "text": "a long one", "panels": ["P001-01", "P001-03"], "cues": [], "emphasis": []},
                 {"start": 9.0, "text": "next", "panels": ["P001-05"], "cues": [], "emphasis": []}]
    shots = _plan(14.0, sentences)["shots"]
    shown = []
    for s in shots:
        for c in s["cards"]:
            if not shown or shown[-1] != c["panel"]:
                shown.append(c["panel"])
    assert shown == ["P001-01", "P001-03", "P001-05"]          # tagged panels only, in order


def test_no_panel_is_shown_twice_in_a_row():
    sentences = [{"start": float(t), "text": str(t), "panels": [pid], "cues": [], "emphasis": []}
                 for t, pid in ((0, "P001-01"), (4, "P001-01"), (8, "P001-03"), (12, "P001-03"), (16, "P001-05"))]
    shots = _plan(30.0, sentences)["shots"]
    seq = [(c["panel"], tuple(c.get("crop") or [])) for s in shots for c in s["cards"]]
    # P3: a >8s shot is split into punch-in pieces of the SAME panel. Adjacent
    # equal panels are allowed only as split continuations (a different crop);
    # the same framing twice in a row is still a bug.
    for (p1, crop1), (p2, crop2) in zip(seq, seq[1:]):
        assert p1 != p2 or crop1 != crop2
    panels = [p for p, _ in seq]
    assert sorted(set(panels)) == ["P001-01", "P001-03", "P001-05"]   # tagged panels only, in order


def test_crops_are_vertical_pans_and_always_an_allowed_shape():
    from manhwa_ocr.video.frames import format_from, shapes_for

    fmt = format_from("16:9", 30)
    for s in _plan(40.0)["shots"]:
        for c in s["cards"]:
            assert c["motion"] not in ("pan_left", "pan_right")
            p = next(x for x in PANELS if x["id"] == c["panel"])
            x0, y0, x1, y1 = c["crop"]
            aspect = (x1 - x0) * p["width"] / ((y1 - y0) * p["height"])
            assert min(abs(math.log(aspect / a)) for a in shapes_for(s["layout"], fmt)) < 0.02


def test_text_only_tags_show_the_nearest_drawn_panel():
    panels = [dict(p, text_only=(p["id"] == "P001-03")) for p in PANELS]
    sentences = [{"start": 0.0, "text": "a", "panels": ["P001-01"], "cues": [], "emphasis": []},
                 {"start": 3.0, "text": "b", "panels": ["P001-03"], "cues": [], "emphasis": []}]
    shots = _plan(6.0, sentences, panels=panels)["shots"]
    used = [c["panel"] for s in shots for c in s["cards"]] + [s.get("bg_panel") for s in shots]
    assert "P001-03" not in used and "P001-04" in used


def test_credits_are_never_shown_even_when_tagged():
    panels = [dict(p, text_only=p["id"] == "P001-05", credits=p["id"] == "P001-05") for p in PANELS]
    sentences = [{"start": 0.0, "text": "a", "panels": ["P001-04"], "cues": [], "emphasis": []},
                 {"start": 3.0, "text": "b", "panels": ["P001-05"], "cues": [], "emphasis": []}]
    shots = _plan(6.0, sentences, panels=panels)["shots"]
    assert all(c["panel"] != "P001-05" for s in shots for c in s["cards"])


def test_detector_tells_lettering_from_art():
    from manhwa_ocr.video.crops import art_score, crop_art, is_text_only, looks_like_credits, text_bands

    caption = Image.new("RGB", (600, 300), (255, 255, 255))
    d = ImageDraw.Draw(caption)
    for i in range(6):
        d.text((40, 30 + 40 * i), "I WISH SOMEONE WOULD TELL ME " * 2, fill=(0, 0, 0))
    art = Image.effect_noise((600, 800), 40).convert("RGB")
    art = Image.merge("RGB", [art.getchannel(0).point(lambda v: v // 2 + 60)] * 3)
    assert is_text_only(art_score(caption), crop_art(caption))
    assert not is_text_only(art_score(art), crop_art(art))
    boxed = art.copy()
    ImageDraw.Draw(boxed).rectangle((0, 640, 600, 800), fill=(255, 255, 255))
    for i in range(3):
        ImageDraw.Draw(boxed).text((60, 660 + 40 * i), "THE SMELL OF HOT BLOOD " * 3, fill=(0, 0, 0))
    top, bottom = text_bands(boxed)
    assert top == 0 and 0.7 < bottom <= 0.82                      # the caption box is cut off
    assert looks_like_credits("Read at ASURASCANS.COM for the fastest releases")
    assert not looks_like_credits("He slipped the ring onto his finger")


def test_calm_defaults_have_no_flash_or_punch():
    sentences = [{"start": 0.0, "text": "a", "panels": ["P001-01"], "cues": ["reveal"], "emphasis": [1.5]},
                 {"start": 5.0, "text": "b", "panels": ["P001-03"], "cues": [], "emphasis": []}]
    shots = _plan(10.0, sentences)["shots"]
    assert not [e for s in shots for e in s["effects"] if e["type"] in ("flash", "punch")]


def test_pair_second_card_arrives_later_and_slides_in():
    sentences = [{"start": 0.0, "text": "a", "panels": ["P001-01", "P001-03"], "cues": [], "emphasis": []},
                 {"start": 5.0, "text": "b", "panels": ["P001-05", "P001-01"], "cues": [], "emphasis": []},
                 {"start": 10.0, "text": "c", "panels": ["P001-03", "P001-05"], "cues": [], "emphasis": []}]
    pairs = [s for s in _plan(15.0, sentences)["shots"] if s["layout"] == "pair"]
    assert pairs
    for shot in pairs:
        second = shot["cards"][1]
        dur = shot["end"] - shot["start"]
        assert second["enter"] == "slide_left" and 0.8 <= second["at"] <= dur - 1.2 + 1e-6


def test_crops_are_fractions_of_the_panel():
    for s in _plan(20.0)["shots"]:
        for c in s["cards"]:
            x0, y0, x1, y1 = c["crop"]
            assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1


def test_old_plans_are_rejected_with_a_fix():
    errs = validate({"version": 1, "shots": [], "duration": 1}, _ids())
    assert "--force" in errs[0]


def test_validate_explains_agent_mistakes():
    plan = _plan(30.0)
    shots = plan["shots"]
    assert len(shots) >= 3
    shots[0]["cards"][0]["panel"] = "P999-99"
    shots[1]["cards"][0]["motion"] = "spin"
    shots[1]["background"] = "purple"
    shots[2]["start"] += 1
    shots[0]["cards"][-1]["crop"] = [0.5, 0.5, 0.2, 0.9]
    text = "\n".join(validate(plan, _ids()))
    for bit in ("doesn't exist", "motion must be", "background must be", "back to back", "crop must be"):
        assert bit in text


# ------------------------------------------------------------------ frames


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


def test_frames_are_deterministic_and_effects_change_pixels():
    fmt = Format(320, 180, 30)
    images = {"A": _card_panel()}
    shot = _shot(effects=[{"type": "flash", "t": 2.0}])
    on = {"flash": True}
    a1 = ShotRenderer(shot, images, fmt, on).frame_array(0.5)
    a2 = ShotRenderer(shot, images, fmt, on).frame_array(0.5)
    assert (a1 == a2).all()
    assert ShotRenderer(shot, images, fmt, on).frame_array(2.06).mean() > a1.mean() + 40
    end = ShotRenderer(shot, images, fmt, video_end=4.0, end_fade=True).frame_array(3.99)
    assert end.mean() < 10


def test_push_in_moves_through_whole_shot():
    # P3: continuous motion — push_in no longer settles and holds mid-shot.
    fmt = Format(320, 180, 30)
    r = ShotRenderer(_shot(end=5), {"A": _card_panel()}, fmt)
    assert r.static_from == pytest.approx(5.0)
    f3, f4 = (r.frame_array(t).astype(int) for t in (3.0, 4.5))
    assert abs(f4 - f3).mean() > 0  # still moving late in the shot


def test_hold_settles_then_holds():
    # Hand-set `hold` (payoff beats only) keeps the old ease-out-then-hold.
    fmt = Format(320, 180, 30)
    shot = _shot(end=5, cards=[{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "hold"}])
    r = ShotRenderer(shot, {"A": _card_panel()}, fmt)
    assert r.static_from <= 3.0                       # settles within ~2.6s, then holds
    f0, f1, f2 = (r.frame_array(t).astype(int) for t in (0.0, 1.0, 2.0))
    assert (r.frame_array(4.5) == r.frame_array(4.0)).all()
    assert abs(f1 - f0).mean() > abs(f2 - f1).mean()  # ease-out: most of the move happens early


def test_backgrounds_differ():
    fmt = Format(320, 180, 30)
    images = {"A": _card_panel()}
    means = {bg: ShotRenderer(_shot(background=bg), images, fmt).frame_array(3.5)[:5, :5].mean()
             for bg in ("black", "white", "blur")}
    assert means["black"] < 20 and means["white"] > 235 and 20 < means["blur"] < 235


@pytest.mark.parametrize("enter", ["fade_in", "slide_up", "slide_down", "blur_in", "zoom_in", "swing_right",
                                   "pull_down", "fit_in_frame", "slide_left", "slide_right"])
def test_entrances_are_not_rushed(enter):
    fmt = Format(320, 180, 30)
    cards = [{"panel": "A", "crop": [0, 0, 1, 1], "enter": enter, "at": 0, "motion": "hold"}]
    r = ShotRenderer(_shot(cards=cards), {"A": _card_panel()}, fmt)
    assert 0.5 <= ENTER_DUR[enter] <= 1.0              # CapCut-like, not a snap
    assert r.cards[0].entrance(0.2) != r.cards[0].entrance(3.9)  # still animating at 0.2s


@pytest.mark.parametrize("enter", ["slide_left", "slide_right", "slide_up", "slide_down",
                                   "pull_down", "fit_in_frame", "blur_in"])
def test_entrance_alpha_does_not_outrun_its_own_motion(enter):
    # Regression: alpha used to ramp to 100% by just 55% of the entrance
    # duration while position (dx/dy/scale/blur) kept changing fast over
    # the full duration -- a card went fully opaque while still rapidly
    # sliding/shrinking/sharpening, reading as a pop/snap instead of a
    # smooth entrance (found via a pair's second card arriving mid-shot).
    # Alpha must now still be ramping while there is real motion left.
    fmt = Format(320, 180, 30)
    cards = [{"panel": "A", "crop": [0, 0, 1, 1], "enter": enter, "at": 0, "motion": "hold"}]
    card = ShotRenderer(_shot(cards=cards), {"A": _card_panel()}, fmt).cards[0]
    d = card.enter_dur
    mid = card.entrance(mid_t := d * 0.5)
    motion_left = abs(mid["dx"]) + abs(mid["dy"]) + abs(mid["scale"] - 1) + mid["blur"]
    assert mid["alpha"] < 0.98 or motion_left < 1.0


def test_pair_first_card_moves_left_when_second_arrives():
    fmt = Format(640, 360, 30)
    images = {"A": _card_panel(), "B": Image.new("RGB", (300, 400), (30, 200, 30))}
    cards = [{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "hold"},
             {"panel": "B", "crop": [0, 0, 1, 1], "enter": "slide_left", "at": 1.5, "motion": "hold"}]
    r = ShotRenderer(_shot(layout="pair", cards=cards, end=5), images, fmt)
    assert r.cards[0].centre(1.0)[0] == pytest.approx(320, abs=2)
    assert r.cards[0].centre(4.0)[0] < 320 - 80
    green = lambda a: (a[..., 1] > 150) & (a[..., 0] < 80)  # noqa: E731
    assert not green(r.frame_array(1.0)).any() and green(r.frame_array(4.0)).any()


# ------------------------------------------------------------------ rendering


def _session_with_video(tmp_path, seconds=4):
    from manhwa_ocr.video.ffmpeg import run

    store = SessionStore(tmp_path)
    sess = store.create("Vid")
    sess.add_links(["https://x.com/1"])
    (sess.path / "chapter_1.md").write_text("# 1\n\nstuff\n")
    _strip(pages_dir(sess.path, "1"))
    (sess.path / "audio").mkdir()
    run(["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "libmp3lame",
         str(sess.path / "audio" / "chapter_1.mp3")])
    cfg = load_config(series__workdir=str(sess.path), series__name="Vid")
    cfg.video["workers"] = 1
    cfg.video["min_hold"] = 1.0   # short test clips; the 3 s floor has its own tests
    return store, sess, cfg


@needs_ffmpeg
def test_build_renders_preview_matching_audio(tmp_path):
    from manhwa_ocr.video import build as vb
    from manhwa_ocr.video.ffmpeg import video_info

    _, sess, cfg = _session_with_video(tmp_path)
    vb.make_plan(sess, "1", cfg, sync="off", log=lambda *_: None)
    out = vb.render_chapter(sess, "1", cfg, preview=True, log=lambda *_: None)
    info = video_info(out)
    assert (info["width"], info["height"]) == (854, 480)
    assert info["audio"] and info["duration"] == pytest.approx(4.0, abs=0.15)
    assert list((sess.path / "video").glob("chapter_1.storyboard_*.png"))
    assert (sess.path / ".work" / "video" / "ch1" / "REVIEW.md").exists()


@needs_ffmpeg
def test_editing_one_shot_rerenders_only_it(tmp_path):
    from manhwa_ocr.video import build as vb
    from manhwa_ocr.video.plan import plan_path

    _, sess, cfg = _session_with_video(tmp_path, seconds=14)
    vb.make_plan(sess, "1", cfg, sync="off", log=lambda *_: None)
    vb.render_chapter(sess, "1", cfg, preview=True, log=lambda *_: None)
    pf = plan_path(sess.path, "1")
    plan = json.loads(pf.read_text())
    assert len(plan["shots"]) >= 2
    card = plan["shots"][0]["cards"][0]
    card["motion"] = "pan_up" if card["motion"] != "pan_up" else "pull_out"
    pf.write_text(json.dumps(plan))
    logs = []
    vb.render_chapter(sess, "1", cfg, preview=True, log=logs.append)
    assert any("rendering 1 " in line for line in logs)


@needs_ffmpeg
def test_hand_edited_plan_is_not_overwritten(tmp_path):
    from manhwa_ocr.video import build as vb
    from manhwa_ocr.video.plan import plan_path

    _, sess, cfg = _session_with_video(tmp_path)
    vb.make_plan(sess, "1", cfg, sync="off", log=lambda *_: None)
    pf = plan_path(sess.path, "1")
    plan = json.loads(pf.read_text())
    plan["shots"][0]["cards"][0]["motion"] = "hold"
    plan["inputs"]["duration"] = -1          # pretend the audio changed
    pf.write_text(json.dumps(plan))
    logs = []
    vb.make_plan(sess, "1", cfg, sync="off", log=logs.append)
    assert json.loads(pf.read_text())["shots"][0]["cards"][0]["motion"] == "hold"
    assert any("left alone" in line for line in logs)


@needs_ffmpeg
def test_render_refuses_estimated_timing_unless_forced(tmp_path):
    from manhwa_ocr.video import build as vb

    _, sess, cfg = _session_with_video(tmp_path)
    cues.write_outputs(sess.path, "1", "{P001-01} The hero wakes up somewhere unfamiliar.", None)
    build_panels(sess.path, "1", log=lambda *_: None)
    align_engine.build_alignment(sess.path, "1", cfg, sync="off", log=lambda *_: None)
    vb.make_plan(sess, "1", cfg, sync="off", log=lambda *_: None)
    with pytest.raises(UserError, match="only estimated"):
        vb.render_chapter(sess, "1", cfg, preview=True, log=lambda *_: None)
    out = vb.render_chapter(sess, "1", cfg, preview=True, allow_estimated=True, log=lambda *_: None)
    assert out.exists()


@needs_ffmpeg
def test_plan_fails_without_alignment_when_tags_exist(tmp_path):
    """plan.py must never fall back to reinterpreting cues.json tags itself:
    a chapter with tags but no chapter_N.alignment.json must fail planning,
    not quietly build a plan from the raw tags."""
    from manhwa_ocr.video import build as vb

    _, sess, cfg = _session_with_video(tmp_path)
    cues.write_outputs(sess.path, "1", "{P001-01} The hero wakes up somewhere unfamiliar.", None)
    build_panels(sess.path, "1", log=lambda *_: None)
    with pytest.raises(UserError, match="has no alignment yet"):
        vb.make_plan(sess, "1", cfg, sync="off", log=lambda *_: None)


@needs_ffmpeg
def test_plan_fails_on_invalid_alignment_json(tmp_path):
    """A broken alignment.json must fail planning, not fall back to cues.json."""
    from manhwa_ocr.video import build as vb

    _, sess, cfg = _session_with_video(tmp_path)
    cues.write_outputs(sess.path, "1", "{P001-01} The hero wakes up somewhere unfamiliar.", None)
    build_panels(sess.path, "1", log=lambda *_: None)
    path = align_engine.alignment_path(sess.path, "1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json {{{", encoding="utf-8")
    with pytest.raises(UserError, match="unreadable"):
        vb.make_plan(sess, "1", cfg, sync="off", log=lambda *_: None)


@needs_ffmpeg
def test_plan_fails_when_alignment_validation_failed(tmp_path):
    """An alignment that failed its own validation must stop planning outright,
    not silently proceed (or fall back to cues.json) anyway."""
    from manhwa_ocr.video import build as vb

    _, sess, cfg = _session_with_video(tmp_path)
    cues.write_outputs(sess.path, "1", "{P001-01} The hero wakes up somewhere unfamiliar.", None)
    build_panels(sess.path, "1", log=lambda *_: None)
    align_engine.build_alignment(sess.path, "1", cfg, sync="off", log=lambda *_: None)
    path = align_engine.alignment_path(sess.path, "1")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["validation"]["status"] = "FAIL"
    data["validation"]["problems"] = [{"sentence": 1, "check": "panels_exist", "detail": "synthetic failure"}]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(UserError, match="failed validation"):
        vb.make_plan(sess, "1", cfg, sync="off", log=lambda *_: None)


@needs_ffmpeg
def test_plan_uses_alignment_decision_not_raw_cue_tag(tmp_path):
    """Even with cues.json present, the plan's shots must reflect the alignment
    engine's decision, not a direct re-read of the cue tag - proving plan.py
    truly treats alignment.json as the only source, not a second opinion."""
    from manhwa_ocr.video import build as vb
    from manhwa_ocr.video.plan import load_plan

    _, sess, cfg = _session_with_video(tmp_path)
    cues.write_outputs(sess.path, "1",
                       "{P001-01} The hero wakes up somewhere unfamiliar. {P001-01} He looks around in shock.",
                       None)
    build_panels(sess.path, "1", log=lambda *_: None)
    align_engine.build_alignment(sess.path, "1", cfg, sync="off", log=lambda *_: None)
    # Hand-edit the alignment to force sentence 2 onto a different panel than its cue tag.
    path = align_engine.alignment_path(sess.path, "1")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["segments"][1]["selected_panel"] = "P001-02"
    data["segments"][1]["display_action"] = "SHOW"
    path.write_text(json.dumps(data), encoding="utf-8")

    vb.make_plan(sess, "1", cfg, sync="off", log=lambda *_: None)
    plan = load_plan(sess.path, "1", {"P001-01", "P001-02", "P001-03", "P002-01"})
    panels_shown = [c["panel"] for s in plan["shots"] for c in s["cards"]]
    assert "P001-02" in panels_shown


def test_status_surfaces_alignment_review_queue(tmp_path):
    """A terminal-only user has no agent reading video align's own output for
    them - `video status` must show a non-empty review queue on its own."""
    from manhwa_ocr.video import build as vb

    _, sess, cfg = _session_with_video(tmp_path)
    assert vb.chapter_status(sess.path, "1")["alignment"] is None  # nothing built yet

    cues.write_outputs(sess.path, "1", "{P001-01} The hero wakes up somewhere unfamiliar.", None)
    build_panels(sess.path, "1", log=lambda *_: None)
    align_engine.build_alignment(sess.path, "1", cfg, sync="off", log=lambda *_: None)
    clean = vb.chapter_status(sess.path, "1")["alignment"]
    # P0: status also surfaces WHY timing is estimated (timing_warnings),
    # so a terminal-only user sees the fallback reason, not just the mode.
    assert clean == {"broken": False, "sync_mode": "estimated", "validation_status": "PASS", "review_queue": 0,
                     "timing_warnings": [{"code": "sync_off",
                                          "message": "sync=off was passed, so timing is estimated from text length.",
                                          "fix": "Re-run without --off for real sync: video align --chapter 1"}]}

    path = align_engine.alignment_path(sess.path, "1")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["review_queue"] = [1]
    path.write_text(json.dumps(data), encoding="utf-8")
    flagged = vb.chapter_status(sess.path, "1")["alignment"]
    assert flagged["review_queue"] == 1


@needs_ffmpeg
def test_render_refuses_unreviewed_alignment_queue_unless_forced(tmp_path):
    """A non-empty alignment review queue means at least one sentence's panel
    was never confirmed by a human - rendering must refuse by default, the
    same way it already refuses "estimated" timing."""
    from manhwa_ocr.video import build as vb

    _, sess, cfg = _session_with_video(tmp_path)
    cues.write_outputs(sess.path, "1", "{P001-01} The hero wakes up somewhere unfamiliar.", None)
    build_panels(sess.path, "1", log=lambda *_: None)
    align_engine.build_alignment(sess.path, "1", cfg, sync="off", log=lambda *_: None)
    vb.make_plan(sess, "1", cfg, sync="off", log=lambda *_: None)

    path = vb.plan_path(sess.path, "1")
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan["alignment_review_queue"] = [1]
    path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(UserError, match="never reviewed"):
        vb.render_chapter(sess, "1", cfg, preview=True, allow_estimated=True, strict=True, log=lambda *_: None)
    out = vb.render_chapter(sess, "1", cfg, preview=True, allow_estimated=True, allow_unreviewed=True,
                            strict=True, log=lambda *_: None)
    assert out.exists()
    # Without --strict a flagged sentence is only a warning: the video still renders.
    logs = []
    assert vb.render_chapter(sess, "1", cfg, preview=True, allow_estimated=True, log=logs.append).exists()
    assert any("flagged for review" in line for line in logs)


@needs_ffmpeg
def test_clip_and_compile(tmp_path):
    from manhwa_ocr.video import build as vb
    from manhwa_ocr.video.ffmpeg import video_info

    _, sess, cfg = _session_with_video(tmp_path, seconds=6)
    vb.make_plan(sess, "1", cfg, sync="off", log=lambda *_: None)
    vb.render_chapter(sess, "1", cfg, log=lambda *_: None)
    clip = vb.make_clip(sess, "1", cfg, 1.0, 4.0, fmt="9:16", preview=True, log=lambda *_: None)
    info = video_info(clip)
    assert info["height"] > info["width"] and info["duration"] == pytest.approx(3.0, abs=0.15)
    comp = vb.compile_videos(sess, ["1"], log=lambda *_: None)
    assert video_info(comp)["duration"] == pytest.approx(6.0 + 2.5, abs=0.3)  # title card + chapter


@needs_ffmpeg
def test_cli_video_build_and_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MANHWA_SESSIONS_ROOT", str(tmp_path))
    # pin the config: the developer's live manhwa-ocr.yaml (video.mode,
    # recap.style, ...) must not leak into this test
    pinned = tmp_path / "pinned.yaml"
    pinned.write_text("output_folder: {}\n".format(tmp_path), encoding="utf-8")
    monkeypatch.setenv("MANHWA_CONFIG", str(pinned))
    store, sess, _ = _session_with_video(tmp_path, seconds=12)   # the real config's 3 s minimum needs room
    store.activate(sess.slug)
    assert cli(["video", "build", "--chapters", "1", "--preview", "--no-sync", "--workers", "1"]) == 0
    assert cli(["video", "status"]) == 0
    out = capsys.readouterr().out
    assert "rendered" in out and "even" in out


def test_build_without_audio_explains_where_it_goes(tmp_path):
    from manhwa_ocr.video import build as vb

    store = SessionStore(tmp_path)
    sess = store.create("NoAudio")
    _strip(pages_dir(sess.path, "1"))
    cfg = load_config(series__workdir=str(sess.path), series__name="NoAudio")
    with pytest.raises(UserError, match="audio/chapter_1.mp3"):
        vb.make_plan(sess, "1", cfg, log=lambda *_: None)


# ------------------------------------------------------------------ dashboard media


@pytest.fixture()
def media_server(tmp_path):
    from manhwa_ocr.ui.server import LocalServer, make_handler

    store = SessionStore(tmp_path)
    sess = store.create("Media")
    (sess.path / "video").mkdir()
    (sess.path / "video" / "chapter_1.mp4").write_bytes(bytes(range(256)) * 40)  # 10240 bytes
    httpd = LocalServer(("127.0.0.1", 0), make_handler(store, lambda *_: None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/api/session/media", sess
    httpd.shutdown()
    httpd.server_close()


def _req(url, headers=None, data=None, method="GET"):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}, data=data, method=method),
                                  timeout=5)


def test_video_served_with_ranges(media_server):
    base, _ = media_server
    full = _req(base + "/media?kind=video&chapter=1")
    assert full.status == 200 and full.headers["Accept-Ranges"] == "bytes" and len(full.read()) == 10240
    part = _req(base + "/media?kind=video&chapter=1", {"Range": "bytes=100-199"})
    assert part.status == 206 and part.headers["Content-Range"] == "bytes 100-199/10240"
    assert part.read() == (bytes(range(256)) * 40)[100:200]
    tail = _req(base + "/media?kind=video&chapter=1", {"Range": "bytes=-10"})
    assert len(tail.read()) == 10
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(base + "/media?kind=video&chapter=1", {"Range": "bytes=99999-"})
    assert exc.value.code == 416


@pytest.mark.parametrize("query", ["kind=video&chapter=../../x", "kind=bogus&chapter=1", "kind=video&chapter=9"])
def test_media_lookup_rejects_bad_requests(media_server, query):
    base, _ = media_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(base + "/media?" + query)
    assert exc.value.code in (400, 404)


def test_audio_upload(media_server):
    base, sess = media_server
    hdr = {"X-Manhwa-OCR": "1", "Content-Type": "audio/mpeg"}
    r = _req(base + "/audio?chapter=2&ext=mp3", hdr, b"ID3fakeaudio", "POST")
    assert json.loads(r.read())["saved"] == "chapter_2.mp3"
    _req(base + "/audio?chapter=2&ext=wav", {**hdr, "Content-Type": "audio/wav"}, b"RIFFfake", "POST")
    assert sorted(p.name for p in (sess.path / "audio").iterdir()) == ["chapter_2.wav"]  # replaced, not duplicated
    for bad in ("/audio?chapter=2&ext=exe", "/audio?chapter=../x&ext=mp3"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _req(base + bad, hdr, b"x", "POST")
        assert exc.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(base + "/audio?chapter=2&ext=mp3", {"Content-Type": "audio/mpeg"}, b"x", "POST")
    assert exc.value.code == 403  # missing write header


# ------------------------------------------------------------------ workflow guidance


def test_chapter_spec_round_trips():
    from manhwa_ocr.util import chapter_spec, expand_chapters

    assert chapter_spec(["5", "1", "2", "3", "11", "7.5"]) == "1-3,5,7.5,11"
    assert expand_chapters(chapter_spec(["1", "2", "4"])) == ["1", "2", "4"]


def test_next_walks_through_recap_audio_and_video(tmp_path):
    from manhwa_ocr.status import next_steps

    store = SessionStore(tmp_path)
    sess = store.create("Flow")
    sess.add_links(["https://x.com/1", "https://x.com/2"])
    for c in ("1", "2"):
        (sess.path / f"chapter_{c}.md").write_text(f"# {c}\n\nstuff\n")
    text = "\n".join(next_steps(sess))
    assert "STEP 2 (agent)" in text and "--chapters 1-2 --prepare" in text

    (sess.path / "recaps").mkdir()
    for c in ("1", "2"):
        (sess.path / "recaps" / f"chapter_{c}.md").write_text("Narration.")
        cues.write_outputs(sess.path, c, "{P001-01} Narration.", None)
    text = "\n".join(next_steps(sess))
    assert "STEP 3 (you" in text and "chapter_1.tts.txt" in text and "chapter_1.mp3" in text
    assert "AGENT: nothing left for you" in text

    (sess.path / "audio").mkdir()
    (sess.path / "audio" / "chapter_1.mp3").write_bytes(b"x")
    text = "\n".join(next_steps(sess))
    assert "STEP 4 (agent)" in text and "video build --chapters 1 " in text
    assert "STEP 3 (you" in text and "AGENT: nothing left" not in text   # chapter 2 still needs audio


def test_dashboard_serves_tts_script(media_server):
    base, sess = media_server
    cues.write_outputs(sess.path, "1", "{P001-01} Say *this* out loud.", None)
    doc = json.loads(_req(base + "/doc?type=tts&id=1").read())
    assert doc["text"].strip() == "Say this out loud."


# ------------------------------------------------------------------ review fixes


def test_short_first_sentence_folds_into_the_next():
    sentences = [{"start": 0.0, "text": "Hm.", "panels": ["P001-01"], "cues": [], "emphasis": []},
                 {"start": 0.3, "text": "Then a lot happens.", "panels": ["P001-03"], "cues": [], "emphasis": []}]
    plan = _plan(10.0, sentences)
    assert validate(plan, _ids()) == []
    assert plan["shots"][0]["start"] == 0 and "Hm." in plan["shots"][0]["sentence"]


def test_short_last_shot_does_not_start_dark():
    fmt = Format(320, 180, 30)
    shot = _shot(start=0, end=0.4, background="white")
    plain = ShotRenderer(shot, {"A": _card_panel()}, fmt).frame_array(0.0)
    faded = ShotRenderer({**shot, "exit": "fade_out"}, {"A": _card_panel()}, fmt,
                         video_end=0.4, end_fade=True).frame_array(0.0)
    assert (faded == plain).all()


def test_clipped_pair_keeps_first_card_in_place():
    fmt = Format(640, 360, 30)
    images = {"A": _card_panel(), "B": Image.new("RGB", (300, 400), (30, 200, 30))}
    cards = [{"panel": "A", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "hold"},
             {"panel": "B", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "hold"}]
    r = ShotRenderer(_shot(layout="pair", cards=cards, end=3, pair_move_at=-2.0), images, fmt)
    assert r.cards[0].centre(0.0)[0] == pytest.approx(r.cards[0].centre(2.0)[0])


@needs_ffmpeg
def test_old_style_plan_is_rebuilt_and_clip_keeps_chapter_cache(tmp_path):
    from manhwa_ocr.video import build as vb
    from manhwa_ocr.video.plan import plan_path

    _, sess, cfg = _session_with_video(tmp_path, seconds=6)
    vb.make_plan(sess, "1", cfg, sync="off", log=lambda *_: None)
    pf = plan_path(sess.path, "1")
    old = json.loads(pf.read_text())
    old["version"] = 1
    pf.write_text(json.dumps(old))
    assert vb.chapter_status(sess.path, "1")["plan"]["outdated"]
    logs = []
    assert vb.make_plan(sess, "1", cfg, sync="off", log=logs.append)["version"] == 2
    assert any("old editing style" in line for line in logs)

    vb.render_chapter(sess, "1", cfg, preview=True, log=lambda *_: None)
    vb.make_clip(sess, "1", cfg, 1.0, 4.0, preview=True, log=lambda *_: None)
    logs = []
    vb.render_chapter(sess, "1", cfg, preview=True, log=logs.append)
    assert any("rendering 0 " in line for line in logs)


@needs_ffmpeg
def test_silent_tail_is_not_part_of_the_video(tmp_path):
    from manhwa_ocr.video import build as vb
    from manhwa_ocr.video.ffmpeg import audio_timing, run

    _, sess, cfg = _session_with_video(tmp_path, seconds=4)
    padded = sess.path / "audio" / "chapter_1.wav"
    run(["-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-filter_complex", "[0][1]concat=n=2:v=0:a=1[a]", "-map", "[a]", "-t", "12", str(padded)])
    (sess.path / "audio" / "chapter_1.mp3").unlink()
    timing = audio_timing(padded)
    assert timing["duration"] == pytest.approx(12.0, abs=0.1) and timing["speech_end"] == pytest.approx(4.0, abs=0.2)
    logs = []
    plan = vb.make_plan(sess, "1", cfg, sync="off", log=logs.append)
    assert plan["duration"] == pytest.approx(4.0 + vb.END_TAIL, abs=0.25)
    assert any("silence at the end" in line for line in logs)
