"""P5 — UX / SHORTS: Shorts picker + manifest, status visibility, reviewer
overrides for chapter cards / callouts, end-screen spec.

Shorts never re-record or generate audio: they slice the chapter's own
narration (audio_offset on the clip plan). End-screen cards are a spec for
YouTube Studio, never burned into pixels.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.video import fixes  # noqa: E402
from manhwa_ocr.video import review as review_mod  # noqa: E402
from manhwa_ocr.video.build import _clip_plan, _editor_status  # noqa: E402
from manhwa_ocr.video.plan import build_plan  # noqa: E402
from manhwa_ocr.video.shorts import (END_CARD_TEXT, build_end_screen, pick_shorts_windows,  # noqa: E402
                                     score_shot, shorts_manifest, validate_end_screen,
                                     validate_shorts_manifest, apply_callout_overrides,
                                     apply_card_overrides)

PANELS = [{"id": f"P001-0{i}", "width": 300, "height": 300 if i % 2 else 700, "tall": i % 2 == 0, "text": ""}
          for i in range(1, 6)]
VCFG = load_config().video


def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 9):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


def _plan(duration=300.0, **kw):
    return build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "x"}, duration=duration,
                      audio=None, vcfg=VCFG, image_for=_img_for, **kw)


def _shot(i, start, end, **kw):
    s = {"id": f"S{i:03d}", "start": float(start), "end": float(end), "layout": "single",
         "background": "black", "sentence": f"Sentence {i} happens.",
         "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0, "motion": "push_in"}],
         "exit": "cut", "effects": [], "callouts": []}
    s.update(kw)
    return s


def _fake_plan(duration=300.0):
    shots = []
    t = 0.0
    i = 1
    while t < duration:
        end = min(duration, t + 6.0)
        shots.append(_shot(i, t, end))
        t, i = end, i + 1
    shots[0]["sentence"] = "Minwoo unlocks a hidden power?"
    shots[0]["effects"] = [{"type": "flash", "t": 1.0}]
    if len(shots) > 10:
        shots[10]["effects"] = [{"type": "shake", "t": 61.0}]
        shots[10]["callouts"] = [{"t": 60.4, "text": "SS-RANK", "style": "keyword", "dur": 1.2}]
    if len(shots) > 30:
        shots[30]["effects"] = [{"type": "shake", "t": 181.0}]
    return {"chapter": "1", "duration": duration, "format": "16:9", "shots": shots,
            "chapter_cards": [], "sfx": [], "hook": {"explicit_hook": True, "open_loops": 1},
            "crop_stats": {"mask_risk": ["P001-01"], "layout_switches": [{"panel": "P001-02"}]},
            "timing_warnings": []}


# ------------------------------------------------------------------ Shorts picker

def test_shorts_pick_three_non_overlapping_30_to_45s_windows():
    windows = pick_shorts_windows(_fake_plan(300.0))
    assert len(windows) == 3
    starts = [w["start"] for w in windows]
    assert starts == sorted(starts)
    for w in windows:
        assert 30.0 - 1e-9 <= w["end"] - w["start"] <= 45.0 + 1e-9
    for a, b in zip(windows, windows[1:]):
        assert b["start"] >= a["end"] + 1.0  # >=1s gap between Shorts


def test_shorts_first_window_covers_the_hook():
    windows = pick_shorts_windows(_fake_plan(300.0))
    assert windows[0]["start"] == pytest.approx(0.0)  # hook shot is the top score


def test_shorts_score_ranks_hook_and_peaks_above_filler():
    plan = _fake_plan(300.0)
    assert score_shot(plan["shots"][0], 0) > score_shot(plan["shots"][1], 1)
    assert score_shot(plan["shots"][10], 10) > score_shot(plan["shots"][1], 1)


def test_shorts_short_chapter_is_one_full_window():
    windows = pick_shorts_windows(_fake_plan(20.0))
    assert windows == [{"start": 0.0, "end": 20.0, "anchor_shot": "S001"}]


def test_shorts_manifest_fields_and_end_card():
    plan = _fake_plan(300.0)
    manifest = shorts_manifest(plan)
    assert manifest["format"] == "9:16" and manifest["count"] == 3
    for entry in manifest["shorts"]:
        assert entry["title_suggestion"] and entry["hook"] and entry["panels"]
        assert entry["end_card"]["text"] == END_CARD_TEXT
    assert validate_shorts_manifest(manifest) == []


def test_shorts_manifest_rejects_window_outside_chapter():
    bad = {"duration": 100.0, "shorts": [{"start": 90.0, "end": 120.0, "title_suggestion": "X"}]}
    assert validate_shorts_manifest(bad) and "inside" in validate_shorts_manifest(bad)[0]


# ------------------------------------------------------------------ clip slicing (audio is sliced, never generated)

def test_clip_plan_slices_audio_offset_and_shifts_events():
    plan = _fake_plan(300.0)
    plan["chapter_cards"] = [{"t": 65.0, "title": "The Trial", "dur": 1.75}]
    plan["sfx"] = [{"type": "impact", "t": 62.0}, {"type": "whoosh", "t": 10.0}]
    clip = _clip_plan(plan, 60.0, 95.0, "9:16")
    assert clip["audio_offset"] == pytest.approx(60.0)
    assert clip["duration"] == pytest.approx(35.0)
    assert clip["shots"][0]["start"] == pytest.approx(0.0)
    assert clip["shots"][-1]["end"] == pytest.approx(35.0)
    assert [c["title"] for c in clip["chapter_cards"]] == ["The Trial"]
    assert clip["chapter_cards"][0]["t"] == pytest.approx(5.0)
    assert [e["type"] for e in clip["sfx"]] == ["impact"]  # the 10s whoosh is outside
    assert clip["sfx"][0]["t"] == pytest.approx(2.0)
    # The callout inside the window rides into clip time.
    assert any(c["text"] == "SS-RANK" and c["t"] == pytest.approx(0.4)
               for s in clip["shots"] for c in s.get("callouts") or [])


def test_clip_plan_rejects_range_outside_chapter():
    from manhwa_ocr.util import UserError

    with pytest.raises(UserError, match="inside the chapter"):
        _clip_plan(_fake_plan(60.0), 50.0, 70.0, "16:9")


# ------------------------------------------------------------------ status visibility

def test_editor_status_surfaces_p0_to_p3_signals():
    plan = _fake_plan(300.0)
    plan["chapter_cards"] = [{"t": 60.0, "title": "The Trial", "dur": 1.75}]
    plan["timing_warnings"] = [{"code": "ESTIMATED", "message": "m", "fix": "re-align"}]
    ed = _editor_status(Path("/nonexistent"), plan, None)
    assert ed["mask_risk"] == 1 and ed["layout_switches"] == 1
    assert ed["chapter_cards"] == 1 and ed["callouts"] == 1
    assert ed["hook"]["explicit_hook"] is True and ed["hook"]["open_loops"] == 1
    assert ed["timing_warnings"] == [{"code": "ESTIMATED", "fix": "re-align"}]
    assert ed["music"] == {"file": None, "present": None}


def test_editor_status_broken_plan_is_empty_not_an_error():
    ed = _editor_status(Path("/nonexistent"), {"broken": True}, None)
    assert ed["mask_risk"] == 0 and ed["chapter_cards"] == 0 and ed["hook"] is None


# ------------------------------------------------------------------ reviewer overrides

def test_fixes_validate_accepts_card_and_callout_overrides():
    clean, problems = fixes.validate(
        {"chapter_cards": [{"shot_id": "S012", "title": "The Exam That Kills"}],
         "callouts": [{"shot_id": "S007", "text": "SS-RANK"}]}, {})
    assert problems == []
    assert clean["chapter_cards"] == {"S012": "The Exam That Kills"}
    assert clean["callouts"] == {"S007": "SS-RANK"}


@pytest.mark.parametrize("bad, text", [
    ({"chapter_cards": [{"shot_id": "S012", "title": "x" * 41}]}, "at most 40 chars"),
    ({"callouts": [{"shot_id": "S007", "text": "a\nb\nc"}]}, "at most 2 lines"),
    ({"callouts": [{"shot_id": "", "text": "SS-RANK"}]}, "non-empty string \"shot_id\""),
    ({"chapter_cards": [{"shot_id": "S012", "title": "A"}, {"shot_id": "S012", "title": "B"}]}, "duplicate shot_id"),
])
def test_fixes_validate_rejects_bad_overrides(bad, text):
    _, problems = fixes.validate(bad, {})
    assert problems and any(text in p for p in problems)


def test_apply_card_override_retitles_only_the_right_card():
    cards = [{"t": 0.0, "title": "Auto 1", "dur": 1.75}, {"t": 120.0, "title": "Auto 2", "dur": 1.75}]
    shots = [_shot(1, 0, 6), _shot(2, 120, 126)]
    out, warnings = apply_card_overrides(cards, {"S002": "The Exam That Kills"}, shots)
    assert out[0]["title"] == "Auto 1" and out[1]["title"] == "The Exam That Kills"
    assert warnings == []


def test_apply_overrides_warn_on_unknown_shot_instead_of_failing():
    _, warnings = apply_card_overrides([], {"S999": "Nope"}, [_shot(1, 0, 6)])
    assert warnings and "unknown shot" in warnings[0]
    _, warnings = apply_callout_overrides([_shot(1, 0, 6)], {"S999": "Nope"})
    assert warnings and "unknown shot" in warnings[0]


def test_apply_callout_override_replaces_shot_callouts():
    shots = [_shot(1, 10, 16, callouts=[{"t": 10.4, "text": "OLD", "style": "keyword", "dur": 1.2}])]
    out, _ = apply_callout_overrides(shots, {"S001": "SS-RANK"})
    assert [c["text"] for c in out[0]["callouts"]] == ["SS-RANK"]
    assert out[0]["callouts"][0]["t"] == pytest.approx(10.4)  # shot start + 0.4s, like the planner


def test_plan_applies_fixes_overrides_and_logs_warnings():
    plan = _plan(400.0, fixes={"chapter_cards": {}, "callouts": {}})
    assert plan["chapter_cards"]  # a >=6min video gets cards
    first_card = plan["chapter_cards"][0]
    card_shot = next(s for s in plan["shots"] if abs(s["start"] - first_card["t"]) <= 0.02)
    plan2 = _plan(400.0, fixes={"chapter_cards": {card_shot["id"]: "My Title"},
                                "callouts": {"S999": "Nope"}})
    assert plan2["chapter_cards"][0]["title"] == "My Title"
    assert any("S999" in w for w in plan2["override_warnings"])


def test_review_merge_keeps_card_and_callout_overrides():
    merged = review_mod.merge({}, {"chapter_cards": [{"shot_id": "S012", "title": "A"}],
                                     "callouts": [{"shot_id": "S007", "text": "SS-RANK"}]})
    assert merged["chapter_cards"][0]["title"] == "A"
    assert merged["callouts"][0]["text"] == "SS-RANK"


# ------------------------------------------------------------------ end screen

def test_end_screen_window_is_last_20s_with_three_slots():
    spec = build_end_screen(300.0, "5", series_name="Necromancer")
    assert spec["window"] == {"start": 280.0, "end": 300.0}
    assert [s["type"] for s in spec["slots"]] == ["next_video", "playlist", "subscribe"]
    assert spec["slots"][0]["target"] == "chapter_6.mp4"
    assert "WATCH NEXT" in spec["watch_next"] and "Necromancer" in spec["watch_next"]
    assert validate_end_screen(spec) == []


def test_end_screen_short_video_starts_at_zero():
    assert build_end_screen(10.0, "1")["window"] == {"start": 0.0, "end": 10.0}


def test_end_screen_rejects_too_long_window_and_missing_block():
    bad = {"window": {"start": 0.0, "end": 30.0}, "slots": [{"type": "next_video"}],
           "watch_next": "nothing"}
    errs = validate_end_screen(bad)
    assert any("last 20" in e for e in errs) and any("WATCH NEXT" in e for e in errs)


def test_plan_carries_end_screen_spec():
    plan = _plan(300.0)
    assert plan["end_screen"]["window"] == {"start": 280.0, "end": 300.0}
    assert validate_end_screen(plan["end_screen"]) == []


# ------------------------------------------------------------------ CLI

def test_cli_parser_accepts_shorts_and_endscreen():
    from manhwa_ocr.cli import build_parser

    p = build_parser()
    a = p.parse_args(["video", "shorts", "--chapter", "5", "--count", "2"])
    assert a.action == "shorts" and a.count == 2
    a = p.parse_args(["video", "endscreen", "--chapter", "5"])
    assert a.action == "endscreen"


def test_make_shorts_writes_manifest_and_end_card(monkeypatch, tmp_path):
    """make_shorts with a stubbed renderer: verifies wiring, not pixels."""
    from manhwa_ocr.video import build as vb

    plan = _fake_plan(300.0)
    plan["chapter_cards"] = [{"t": 65.0, "title": "The Trial", "dur": 1.75}]
    captured = []

    def fake_render(root, chapter, clip, vcfg, **kw):
        captured.append(clip)
        return Path(f"chapter_{chapter}.shorts_{kw.get('name', '').split('_')[-1]}.mp4")

    monkeypatch.setattr(vb, "load_panels", lambda root, chapter: {"panels": []})
    monkeypatch.setattr(vb, "load_plan", lambda root, chapter, ids: plan)
    monkeypatch.setattr(vb, "render", fake_render)
    sess = type("S", (), {"path": tmp_path, "slug": "necromancer", "name": "Necromancer"})()
    manifest = vb.make_shorts(sess, "1", type("C", (), {"video": VCFG})(), count=1, log=lambda *_: None)
    assert manifest["count"] == 1 and manifest["files"] == ["chapter_1.shorts_1.mp4"]
    assert (tmp_path / "video" / "chapter_1.shorts.json").exists()
    assert json.loads((tmp_path / "video" / "chapter_1.shorts.json").read_text())["count"] == 1
    clip = captured[0]
    assert clip["format"] == "9:16" and clip["audio_offset"] == pytest.approx(0.0)
    assert clip["chapter_cards"][-1]["title"] == END_CARD_TEXT  # end card appended
