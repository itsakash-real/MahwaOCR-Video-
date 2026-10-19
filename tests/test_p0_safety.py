"""P0 safety lock: golden-chapter regression guards, loud fallbacks, determinism,
and whole-file validation for fixes/pairs/cues.

These tests lock the behaviours Chapters 1-4 relied on, so P1/P2/P3 editor
work cannot silently change them. They use synthetic panels/sentences only;
no audio fixtures, no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.util import UserError  # noqa: E402
from manhwa_ocr.video import align_engine, cues as cues_mod, sync  # noqa: E402
from manhwa_ocr.video.build import quality_report  # noqa: E402
from manhwa_ocr.video.pairing import validate_pairs  # noqa: E402
from manhwa_ocr.video.plan import build_plan, validate as validate_plan  # noqa: E402


# ------------------------------------------------------------------ helpers

PANELS = [{"id": f"P001-0{i}", "width": 300, "height": 300 if i % 2 else 700,
           "text": ""} for i in range(1, 6)]
VCFG = load_config().video


def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 9):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


def _sentences():
    return [
        {"text": "Ron draws his sword fast.", "panels": ["P001-01"]},
        {"text": "Kim watches the gate open wide.", "panels": ["P001-02"]},
        {"text": "The demon king laughs loudly.", "panels": ["P001-03"]},
    ]


# ------------------------------------------------------------------ golden: alignment determinism


def test_align_sentences_is_deterministic():
    panels = [dict(p, text="sword gate demon") for p in PANELS]
    first = align_engine.align_sentences(_sentences(), panels)
    second = align_engine.align_sentences(_sentences(), panels)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert [s["selected_panel"] for s in first] == ["P001-01", "P001-02", "P001-03"]


def test_align_same_sentence_holds_previous_panel():
    sentences = [
        {"text": "Ron draws his sword fast.", "panels": ["P001-01"]},
        {"text": "He thinks about home.", "same": True, "panels": []},
    ]
    segs = align_engine.align_sentences(sentences, PANELS)
    assert segs[1]["display_action"] == "HOLD_PREVIOUS"
    assert segs[1]["selected_panel"] is None


# ------------------------------------------------------------------ golden: plan determinism


def _build_plan():
    units = [{"start": 0.0, "end": 8.0, "panels": ["P001-01"], "cues": [], "emphasis": [],
              "sentence": "x", "sentence_starts": [0.0]},
             {"start": 8.0, "end": 16.0, "panels": ["P001-02"], "cues": [], "emphasis": [],
              "sentence": "y", "sentence_starts": [8.0]}]
    return build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "x"},
                      duration=16.0, audio=None, vcfg=dict(VCFG),
                      image_for=_img_for, alignment_units=units)


def test_build_plan_is_deterministic():
    first = _build_plan()
    second = _build_plan()
    assert json.dumps(first["shots"], sort_keys=True) == json.dumps(second["shots"], sort_keys=True)
    assert validate_plan(first, {p["id"] for p in PANELS}) == []


# ------------------------------------------------------------------ no silent fallbacks: timing warnings


def test_timed_sentences_records_whisper_unavailable_warning(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / "audio").mkdir(parents=True)
    (root / "audio" / "chapter_1.wav").write_bytes(b"not real audio")
    monkeypatch.setattr(sync, "available", lambda: False)
    from manhwa_ocr.video import ffmpeg

    monkeypatch.setattr(ffmpeg, "audio_timing",
                        lambda *a, **k: {"duration": 12.0, "speech_end": 12.0})
    warnings: list = []
    timed, mode, duration = align_engine._timed_sentences(
        root, "1", [{"text": "One two three four five six"}],
        {}, "auto", lambda *_: None, dict(sync.SYNC_DEFAULTS),
        warnings_out=warnings)
    assert mode == "estimated"
    assert duration == 12.0
    assert timed and timed[0]["timing_source"] == "estimated"
    assert warnings and warnings[0]["code"] == "whisper_unavailable"
    assert "video align" in warnings[0]["fix"]


def test_timed_sentences_records_transcribe_failure_warning(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / "audio").mkdir(parents=True)
    (root / "audio" / "chapter_1.wav").write_bytes(b"not real audio")
    monkeypatch.setattr(sync, "available", lambda: True)
    monkeypatch.setattr(sync, "transcribe", lambda *a, **k: (_ for _ in ()).throw(
        UserError("fake whisper boom")))
    from manhwa_ocr.video import ffmpeg

    monkeypatch.setattr(ffmpeg, "audio_timing",
                        lambda *a, **k: {"duration": 12.0, "speech_end": 12.0})
    warnings: list = []
    timed, mode, _ = align_engine._timed_sentences(
        root, "1", [{"text": "One two three four five six"}],
        {}, "auto", lambda *_: None, dict(sync.SYNC_DEFAULTS),
        warnings_out=warnings)
    assert mode == "estimated"
    assert warnings and warnings[0]["code"] == "whisper_transcribe_failed"
    assert "fake whisper boom" in warnings[0]["message"]


# ------------------------------------------------------------------ whole-file validation: cues + pairs


def test_validate_cues_names_sentence_and_field():
    assert cues_mod.validate_cues({"sentences": [{"text": "ok", "panels": ["P001-01"]}]}) == []
    problems = cues_mod.validate_cues({"sentences": [{"text": "  ", "panels": "P001-01"},
                                                   {"text": "fine", "panels": ["P001-01", "P001-01"]}]})
    assert any("sentence 1" in p and "text" in p for p in problems)
    assert any("sentence 1" in p and "panels" in p for p in problems)
    assert any("sentence 2" in p and "duplicate" in p for p in problems)
    assert cues_mod.validate_cues({"sentences": []})
    assert cues_mod.validate_cues([])


def test_load_cues_strict_fails_loudly_on_corrupt_file(tmp_path):
    root = tmp_path / "ws"
    (root / "recaps").mkdir(parents=True)
    (root / "recaps" / "chapter_1.cues.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(UserError, match="not valid JSON"):
        cues_mod.load_cues_strict(root, "1")
    (root / "recaps" / "chapter_1.cues.json").write_text(
        json.dumps({"sentences": [{"text": "", "panels": []}]}), encoding="utf-8")
    with pytest.raises(UserError, match="sentence 1"):
        cues_mod.load_cues_strict(root, "1")
    assert cues_mod.load_cues_strict(tmp_path / "empty", "1") is None


def test_validate_pairs_names_sentence_and_ids():
    by_id = {p["id"]: p for p in PANELS}
    assert validate_pairs({"1": ["P001-01"]}, 3, by_id) == []
    errors = validate_pairs({"1": ["P999-99"], "2": "not-a-list",
                             "99": ["P001-01"], "x": ["P001-01"]}, 3, by_id)
    assert any("P999-99" in e for e in errors)
    assert any("sentence 2" in e for e in errors)
    assert any("sentence 99" in e for e in errors)
    assert any('"x"' in e for e in errors)
    # _suggestions / _comment keys are notes, never errors.
    assert validate_pairs({"_suggestions": {"1": {}}, "1": ["P001-01"]}, 3, by_id) == []


def test_validate_pairs_rejects_text_only_and_duplicates():
    by_id = {p["id"]: dict(p, text_only=True) if p["id"] == "P001-01" else p for p in PANELS}
    errors = validate_pairs({"1": ["P001-01"]}, 3, by_id)
    assert any("text/credits" in e for e in errors)
    errors = validate_pairs({"1": ["P001-02", "P001-02"]}, 3, {p["id"]: p for p in PANELS})
    assert any("duplicate" in e for e in errors)


# ------------------------------------------------------------------ golden: QA failures stop the build


def _plan_with_shots(shots, duration=10.0):
    return {"chapter": "1", "format": "16:9", "fps": 30, "duration": duration,
            "sync": "synced", "shots": shots, "crop_stats": {}}


def _shot(start, end, panel, crop=(0.0, 0.0, 1.0, 0.75)):
    return {"id": "S001", "start": start, "end": end, "layout": "single",
            "background": "blur", "cards": [{"panel": panel, "crop": list(crop)}]}


def test_quality_report_fails_on_bad_shape_but_not_on_a_missing_panel():
    panels = {"panels": [{"id": "P001-01", "width": 300, "height": 400},
                         {"id": "P001-02", "width": 300, "height": 400}]}
    # Only P001-01 shown -> P001-02 missing. Crop 1.0x0.1 is not an allowed shape.
    plan = _plan_with_shots([_shot(0.0, 10.0, "P001-01", crop=(0.0, 0.0, 1.0, 0.1))])
    qa = quality_report(plan, panels, None, 3.0)
    # Rule 9: a missing panel is INFO ONLY, never a failure.
    assert qa["missing"] == ["P001-02"]
    assert qa["panels_shown"] == 1 and qa["panels_total"] == 2
    assert not any("would never appear" in f for f in qa["failures"])
    assert any("allowed shapes" in f for f in qa["failures"])


def test_quality_report_fails_on_too_short_shot():
    panels = {"panels": [{"id": "P001-01", "width": 300, "height": 400}]}
    plan = _plan_with_shots([_shot(0.0, 0.1, "P001-01")], duration=0.1)
    qa = quality_report(plan, panels, None, 3.0)
    assert any("too short" in f for f in qa["failures"])


def test_quality_report_passes_a_clean_plan():
    panels = {"panels": [{"id": "P001-01", "width": 300, "height": 400}]}
    # 300x400 panel, crop 1.0 x 0.75 -> aspect (300)/(300) = 1.0 -> 1:1, allowed for single.
    plan = _plan_with_shots([_shot(0.0, 10.0, "P001-01", crop=(0.0, 0.0, 1.0, 0.75))])
    qa = quality_report(plan, panels, None, 3.0)
    assert qa["failures"] == []
