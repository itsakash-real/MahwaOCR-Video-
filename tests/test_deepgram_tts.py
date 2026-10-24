"""Tests for the Deepgram TTS step (manhwa_ocr/video/tts.py).

No network and no real API key: the HTTP opener and the speak function are
stubbed, and the ffmpeg encode is replaced with a copy.
"""

import io
import json
import urllib.error
import wave
from types import SimpleNamespace

import pytest

from manhwa_ocr.util import UserError
from manhwa_ocr.video import tts


def _tiny_wav(seconds: float, rate: int = 24000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x01\x00" * int(rate * seconds))
    return out.getvalue()


def _write_cues(root, chapter="1", texts=("One sentence.", "A second, longer sentence.")):
    recaps = root / "recaps"
    recaps.mkdir(parents=True, exist_ok=True)
    cues = {"version": 1, "chapter": str(chapter),
            "sentences": [{"text": t, "panels": ["P001-01"], "same": False} for t in texts]}
    (recaps / f"chapter_{chapter}.cues.json").write_text(json.dumps(cues), encoding="utf-8")


# ---------------------------------------------------------------- sentences


def test_load_sentences_reads_cues(tmp_path):
    _write_cues(tmp_path)
    assert tts.load_sentences(tmp_path, "1") == ["One sentence.", "A second, longer sentence."]


def test_load_sentences_missing_cues_explains(tmp_path):
    with pytest.raises(UserError, match="Finalize the recap first"):
        tts.load_sentences(tmp_path, "9")


# ---------------------------------------------------------------- API key


def test_load_api_key_prefers_env(tmp_path, monkeypatch):
    key_file = tmp_path / "key.txt"
    key_file.write_text("file-key", encoding="utf-8")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "env-key")
    assert tts.load_api_key({}, str(key_file)) == "env-key"


def test_load_api_key_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    key_file = tmp_path / "key.txt"
    key_file.write_text("  file-key\n", encoding="utf-8")
    assert tts.load_api_key({}, str(key_file)) == "file-key"


def test_load_api_key_missing_raises_without_leaking(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(UserError, match="No Deepgram API key found"):
        tts.load_api_key({}, str(tmp_path / "nope.key"))


# ---------------------------------------------------------------- Deepgram call


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _FakeOpener:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.request = None

    def open(self, request, timeout=None):
        self.request = request
        return _FakeResponse(self.payload)


def test_deepgram_speak_sends_token_header_and_text():
    opener = _FakeOpener(_tiny_wav(0.1))
    out = tts.deepgram_speak("Hello world.", "aura-2-thalia-en", "SECRETKEY", opener=opener)
    assert out.startswith(b"RIFF")
    req = opener.request
    assert req.full_url.startswith("https://api.deepgram.com/v1/speak?")
    assert "model=aura-2-thalia-en" in req.full_url
    # incident 22: an unset speed is OMITTED (non-aura-2 models reject the
    # parameter); speed policy is resolved+validated by the caller
    assert "speed=" not in req.full_url
    assert req.get_header("Authorization") == "Token SECRETKEY"
    assert json.loads(req.data) == {"text": "Hello world."}


def test_deepgram_speak_uses_the_given_speed():
    opener = _FakeOpener(_tiny_wav(0.1))
    tts.deepgram_speak("Hello.", "aura-2-thalia-en", "SECRETKEY", speed=1.23, opener=opener)
    assert "speed=1.23" in opener.request.full_url


def test_deepgram_speak_error_surfaces_message_but_not_key():
    class BoomOpener:
        def open(self, request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {},
                io.BytesIO(json.dumps({"err_msg": "Invalid credentials."}).encode()))

    with pytest.raises(UserError) as excinfo:
        tts.deepgram_speak("Hi.", "aura-2-thalia-en", "SECRETKEY", opener=BoomOpener())
    assert "Invalid credentials." in str(excinfo.value)
    assert "SECRETKEY" not in str(excinfo.value)


# ---------------------------------------------------------------- stitching


def test_concat_wavs_joins_with_silence():
    joined, durations = tts.concat_wavs([_tiny_wav(0.5), _tiny_wav(0.25)], gap_seconds=0.1)
    assert durations == [0.5, 0.25]
    with wave.open(io.BytesIO(joined), "rb") as wf:
        total = wf.getnframes() / wf.getframerate()
    assert total == pytest.approx(0.5 + 0.1 + 0.25)


def test_concat_wavs_rejects_mismatched_formats():
    with pytest.raises(UserError, match="inconsistent WAV formats"):
        tts.concat_wavs([_tiny_wav(0.1, rate=24000), _tiny_wav(0.1, rate=16000)], gap_seconds=0.1)


# ---------------------------------------------------------------- end to end


def test_synthesize_chapter_writes_audio_manifest_and_caches(tmp_path, monkeypatch):
    _write_cues(tmp_path)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "SECRETKEY")
    monkeypatch.setattr(tts, "encode_m4a",
                        lambda wav_path, out_path: out_path.write_bytes(b"m4a"))
    calls = []

    def fake_speak(text, model, key):
        calls.append((text, model, key))
        return _tiny_wav(0.2)

    logs = []
    cfg = SimpleNamespace(extra={"tts": {"model": "aura-2-orion-en", "gap_seconds": 0.1}})
    out = tts.synthesize_chapter(tmp_path, "1", cfg, log=logs.append, speak=fake_speak)
    assert out == tmp_path / "audio" / "chapter_1.m4a"
    assert out.read_bytes() == b"m4a"
    assert len(calls) == 2 and all(c[1] == "aura-2-orion-en" for c in calls)
    assert all(c[2] == "SECRETKEY" for c in calls)

    manifest = json.loads((tmp_path / "audio" / "chapter_1.tts.json").read_text(encoding="utf-8"))
    assert manifest["model"] == "aura-2-orion-en"
    assert manifest["speed"] == tts.DEFAULT_SPEED
    assert manifest["total_seconds"] == pytest.approx(0.2 + 0.1 + 0.2)
    assert [s["n"] for s in manifest["sentences"]] == [1, 2]
    assert "SECRETKEY" not in json.dumps(manifest)

    # Second run: every sentence is cached, so nothing is re-synthesized.
    calls.clear()
    tts.synthesize_chapter(tmp_path, "1", cfg, log=logs.append, speak=fake_speak)
    assert calls == []


def test_synthesize_chapter_default_speed_is_1_23(tmp_path, monkeypatch):
    _write_cues(tmp_path, texts=("One sentence.",))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "SECRETKEY")
    monkeypatch.setattr(tts, "encode_m4a", lambda wav_path, out_path: out_path.write_bytes(b"m4a"))
    # an aura-2 voice with no explicit speed resolves to the tuned default.
    # (The VOICE is never silently defaulted: without a configured model the
    # command refuses -- tests/test_video_incidents.py, incident 2.)
    cfg = SimpleNamespace(extra={"tts": {"model": "aura-2-thalia-en"}})
    tts.synthesize_chapter(tmp_path, "1", cfg, log=print, speak=lambda *a: _tiny_wav(0.2))
    manifest = json.loads((tmp_path / "audio" / "chapter_1.tts.json").read_text(encoding="utf-8"))
    assert manifest["speed"] == 1.23


def test_changing_speed_invalidates_the_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _write_cues(tmp_path, texts=("One sentence.",))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "SECRETKEY")
    monkeypatch.setattr(tts, "encode_m4a", lambda wav_path, out_path: out_path.write_bytes(b"m4a"))
    calls = []

    def fake_speak(text, model, key):
        calls.append(text)
        return _tiny_wav(0.2)

    cfg = SimpleNamespace(extra={"tts": {"model": "aura-2-thalia-en"}})
    tts.synthesize_chapter(tmp_path, "1", cfg, log=print, speed=1.0, speak=fake_speak)
    assert len(calls) == 1

    calls.clear()
    tts.synthesize_chapter(tmp_path, "1", cfg, log=print, speed=1.23, speak=fake_speak)
    assert calls == ["One sentence."]  # different speed -> cache miss, re-synthesized


def test_synthesize_chapter_resynthesizes_a_corrupted_cache_entry(tmp_path, monkeypatch):
    _write_cues(tmp_path, texts=("One sentence.",))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "SECRETKEY")
    monkeypatch.setattr(tts, "encode_m4a",
                        lambda wav_path, out_path: out_path.write_bytes(b"m4a"))
    calls = []

    def fake_speak(text, model, key):
        calls.append(text)
        return _tiny_wav(0.2)

    cfg = SimpleNamespace(extra={"tts": {"model": "aura-2-thalia-en"}})
    tts.synthesize_chapter(tmp_path, "1", cfg, log=print, speak=fake_speak)
    assert len(calls) == 1

    # Simulate an interrupted write leaving a truncated cache entry on disk.
    (tmp_path / ".work" / "tts" / "ch1" / "s001.wav").write_bytes(b"\x00")
    calls.clear()
    tts.synthesize_chapter(tmp_path, "1", cfg, log=print, speak=fake_speak)
    assert calls == ["One sentence."]  # re-synthesized rather than trusting the corrupt cache
