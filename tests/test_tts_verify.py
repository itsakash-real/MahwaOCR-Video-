"""Tests for the whisper-based post-synthesis audio self-check (video/tts_verify.py).

No real whisper/pywhispercpp dependency: `transcribe` is stubbed with a
canned word list, matching how `video align`'s own tests avoid the model.
"""

import json

import pytest

from manhwa_ocr.util import UserError
from manhwa_ocr.video import tts_verify


def _setup(root, sentences, sentence_entries):
    recaps = root / "recaps"
    recaps.mkdir(parents=True, exist_ok=True)
    cues = {"version": 1, "chapter": "1",
            "sentences": [{"text": t, "panels": ["P001-01"], "same": False} for t in sentences]}
    (recaps / "chapter_1.cues.json").write_text(json.dumps(cues), encoding="utf-8")

    audio = root / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    (audio / "chapter_1.m4a").write_bytes(b"fake-audio")
    manifest = {"version": 1, "chapter": "1", "provider": "deepgram", "model": "aura-2-thalia-en",
                "gap_seconds": 0.35, "chars": 0, "total_seconds": 0.0,
                "generated_at": "now", "sentences": sentence_entries}
    (audio / "chapter_1.tts.json").write_text(json.dumps(manifest), encoding="utf-8")


def _words(text, start, seconds):
    toks = text.lower().rstrip(".").split()
    n = len(toks)
    step = seconds / n
    out = []
    for i, w in enumerate(toks):
        t0 = start + i * step
        out.append({"w": w, "start": round(t0, 3), "end": round(t0 + step * 0.8, 3)})
    return out


def test_matching_audio_passes_clean(tmp_path):
    _setup(tmp_path, ["The hero wins the fight."],
          [{"n": 1, "chars": 24, "seconds": 2.0, "start": 0.0}])
    heard = _words("The hero wins the fight.", 0.0, 2.0)
    result = tts_verify.verify_chapter_audio(tmp_path, "1", log=lambda *a: None,
                                             transcribe=lambda *a, **k: heard)
    assert result["ok"] is True
    assert result["flagged"] == []


def test_mismatched_words_are_flagged(tmp_path):
    _setup(tmp_path, ["The hero wins the fight."],
          [{"n": 1, "chars": 24, "seconds": 2.0, "start": 0.0}])
    # Whisper heard something almost entirely different -- simulating a garbled/mispronounced read.
    heard = _words("completely unrelated noise static garble.", 0.0, 2.0)
    result = tts_verify.verify_chapter_audio(tmp_path, "1", log=lambda *a: None,
                                             transcribe=lambda *a, **k: heard)
    assert result["ok"] is False
    assert result["flagged"][0]["n"] == 1


def test_truncated_duration_flagged_even_with_matching_words(tmp_path):
    _setup(tmp_path, ["The hero wins the big final fight tonight."],
          [{"n": 1, "chars": 40, "seconds": 0.3, "start": 0.0}])
    heard = _words("The hero wins the big final fight tonight.", 0.0, 0.3)
    result = tts_verify.verify_chapter_audio(tmp_path, "1", log=lambda *a: None,
                                             transcribe=lambda *a, **k: heard)
    assert result["ok"] is False
    assert any("truncation" in issue for issue in result["flagged"][0]["issues"])


def test_writes_report_file(tmp_path):
    _setup(tmp_path, ["Clean sentence here."],
          [{"n": 1, "chars": 20, "seconds": 1.5, "start": 0.0}])
    heard = _words("Clean sentence here.", 0.0, 1.5)
    tts_verify.verify_chapter_audio(tmp_path, "1", log=lambda *a: None,
                                    transcribe=lambda *a, **k: heard)
    report_path = tmp_path / "audio" / "chapter_1.tts_verify.json"
    assert report_path.exists()
    assert json.loads(report_path.read_text(encoding="utf-8"))["ok"] is True


def test_missing_manifest_raises(tmp_path):
    (tmp_path / "recaps").mkdir()
    with pytest.raises(UserError, match="Run `video tts"):
        tts_verify.verify_chapter_audio(tmp_path, "1", log=lambda *a: None, transcribe=lambda *a, **k: [])


def test_no_pywhispercpp_raises_clear_error(tmp_path, monkeypatch):
    _setup(tmp_path, ["Clean sentence here."],
          [{"n": 1, "chars": 20, "seconds": 1.5, "start": 0.0}])
    from manhwa_ocr.video import sync as sync_mod
    monkeypatch.setattr(sync_mod, "available", lambda: False)
    with pytest.raises(UserError, match="pywhispercpp"):
        tts_verify.verify_chapter_audio(tmp_path, "1", log=lambda *a: None)
