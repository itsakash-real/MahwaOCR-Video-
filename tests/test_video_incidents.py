"""Regression tests for the video-workflow incident log (research mission,
Phase 4): one test per incident the mission fixed, named after it.

Incidents already fixed elsewhere keep their existing tests:
  5  ({same} trap)  -> tests/test_p2_sync.py::same_no_hidden_panel
  17 (filler rules) -> tests/test_impactful_selection.py
  20/21 (cue leaks / quote splitting) -> tests/test_recap_refine.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.config import (DEFAULT_CONFIG, format_effective_video,  # noqa: E402
                               load_config)
from manhwa_ocr.util import UserError  # noqa: E402


# ---------------------------------------------------------------- incident 1


def test_incident_1_series_config_layers_over_working_yaml(tmp_path, monkeypatch):
    """--config <series>.yaml inherits keys from the working folder's own
    manhwa-ocr.yaml instead of silently falling back to code defaults."""
    from manhwa_ocr.config import resolve_config_chain

    monkeypatch.delenv("MANHWA_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "manhwa-ocr.yaml").write_text(
        "video:\n  mode: wide\n  min_hold: 2.5\ntts:\n  model: aura-2-orion-en\n")
    (tmp_path / "series.yaml").write_text("video:\n  mode: placed\n")
    layers = resolve_config_chain(tmp_path / "series.yaml")
    assert tmp_path / "manhwa-ocr.yaml" in layers      # the working yaml is a base layer
    cfg = load_config(tmp_path / "series.yaml", base_layers=layers)
    assert cfg.video["mode"] == "placed"          # the series file wins where it speaks
    assert cfg.video["min_hold"] == 2.5           # inherited from the working yaml (was: silent 3.0)
    assert cfg.tts["model"] == "aura-2-orion-en"  # inherited too (was: silent code default)


def test_incident_1_manhwa_config_stays_an_isolation_override(tmp_path, monkeypatch):
    """MANHWA_CONFIG (the hermetic-test pin) must not inherit the developer's
    live repo yaml when an explicit --config points at it."""
    from manhwa_ocr.config import DEFAULT_CONFIG, resolve_config_chain

    monkeypatch.chdir(tmp_path)
    pinned = tmp_path / "pinned.yaml"
    pinned.write_text("video:\n  min_hold: 2.0\n")
    monkeypatch.setenv("MANHWA_CONFIG", str(pinned))
    assert resolve_config_chain(pinned) == []          # nothing layers beneath the pin
    cfg = load_config(pinned)
    assert cfg.video["min_hold"] == 2.0
    # whatever the repo's own yaml says (e.g. mode: panelfit) never leaked in
    assert cfg.video["mode"] == DEFAULT_CONFIG["video"]["mode"]


def test_incident_1_unset_mode_is_marked_defaulted_in_the_echo(tmp_path, monkeypatch):
    from manhwa_ocr.config import find_config

    monkeypatch.delenv("MANHWA_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    # a local yaml that sets nothing video-related: find_config picks it (not
    # the repo's own), and with no explicit --config nothing layers beneath,
    # so the video section is pure defaults
    (tmp_path / "manhwa-ocr.yaml").write_text("output_folder: .\n")
    cfg = load_config(find_config())
    mode_line = next(l for l in format_effective_video(cfg).splitlines() if "mode=" in l)
    assert "DEFAULTED" in mode_line and "mode=classic" in mode_line
    (tmp_path / "manhwa-ocr.yaml").write_text("output_folder: .\nvideo:\n  mode: placed\n")
    mode_line = next(l for l in format_effective_video(load_config(find_config())).splitlines()
                     if "mode=" in l)
    assert "DEFAULTED" not in mode_line and "mode=placed" in mode_line


# ---------------------------------------------------------------- incident 2


def test_incident_2_no_silent_default_voice(tmp_path):
    from manhwa_ocr.video import tts

    (tmp_path / "recaps").mkdir()
    (tmp_path / "recaps" / "chapter_1.cues.json").write_text(json.dumps(
        {"version": 1, "sentences": [{"text": "Hello there.", "panels": []}]}))
    calls = []

    def fake_speak(text, model, key):
        calls.append(model)
        return b""

    cfg = type("C", (), {"tts": {}, "extra": {}})()
    with pytest.raises(UserError, match="No TTS voice configured"):
        tts.synthesize_chapter(tmp_path, "1", cfg, log=lambda *_: None, speak=fake_speak)
    assert not calls  # nothing was synthesized with a silently-defaulted voice


def test_incident_2_configured_voice_is_used_and_recorded(tmp_path):
    from manhwa_ocr.video import tts

    (tmp_path / "recaps").mkdir()
    (tmp_path / "recaps" / "chapter_1.cues.json").write_text(json.dumps(
        {"version": 1, "sentences": [{"text": "Hello there.", "panels": []}]}))
    # a real 0.1s silent WAV so the stitcher is happy
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(b"\x00\x00" * 2400)
    wav = buf.getvalue()

    cfg = type("C", (), {"tts": {"model": "aura-2-odysseus-en", "gap_seconds": 0.1}, "extra": {}})()
    tts.synthesize_chapter(tmp_path, "1", cfg, log=lambda *_: None, speak=lambda *a: wav)
    manifest = json.loads((tmp_path / "audio" / "chapter_1.tts.json").read_text())
    assert manifest["model"] == "aura-2-odysseus-en"


# ---------------------------------------------------------------- incident 3


def test_incident_3_echo_names_whisper_availability():
    from manhwa_ocr.video import sync as sync_mod

    cfg = load_config()
    echoed = format_effective_video(cfg)
    assert "whisper=" in echoed
    # the loud-fallback contract: estimated timing is warned with a code+fix,
    # and the echo reports whether whisper exists on THIS interpreter
    assert isinstance(sync_mod.available(), bool)


# ---------------------------------------------------------------- incident 4


def _pairing_fixture(tmp_path):
    """A real panels.json (from synthetic pages) + cues, so pairing's
    task/apply run exactly as in a session."""
    from PIL import ImageDraw

    from manhwa_ocr.video import cues as cues_mod
    from manhwa_ocr.video.panels import build_panels

    pages = tmp_path / "media" / "ch1" / "pages"
    pages.mkdir(parents=True)
    img = Image.new("RGB", (400, 1200), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for y0, y1, colour in [(0, 500, (40, 120, 60)), (560, 1140, (60, 60, 140))]:
        d.rectangle((10, y0, 390, y1 - 1), fill=colour)
        for x in range(10, 390, 7):
            d.line((x, y0, x, y1 - 1), fill=(colour[0] // 2, colour[1] // 2, colour[2] // 2))
    img.save(pages / "page_001.png")
    build_panels(tmp_path, "1", log=lambda *_: None)
    (tmp_path / "recaps").mkdir(exist_ok=True)
    ids = [p["id"] for p in json.loads((tmp_path / "media" / "ch1" / "panels.json").read_text())["panels"]
           if not p.get("text_only")]
    assert len(ids) >= 2
    cues_mod.write_outputs(
        tmp_path, "1",
        "{%s} First sentence about the first panel.\n\n{%s} Second sentence, second panel."
        % (ids[0], ids[1]),
        set(ids))
    return ids


def test_incident_4_pairs_json_carries_sentence_text_and_sha(tmp_path):
    from manhwa_ocr.video import pairing

    _pairing_fixture(tmp_path)
    pairing.write_task(tmp_path, "1", None, log=lambda *_: None)
    pairs = json.loads((tmp_path / ".work" / "video" / "ch1" / "pairs.json").read_text())
    assert pairs["_sentences"]["1"].startswith("First sentence")
    assert len(pairs["_cues_sha"]) == 16


def test_incident_4_apply_refuses_when_cues_were_resegmented(tmp_path):
    from manhwa_ocr.video import cues as cues_mod
    from manhwa_ocr.video import pairing

    ids = _pairing_fixture(tmp_path)
    pairing.write_task(tmp_path, "1", None, log=lambda *_: None)
    pairs_path = tmp_path / ".work" / "video" / "ch1" / "pairs.json"
    pairs = json.loads(pairs_path.read_text())
    pairs["2"] = [ids[0]]      # the edit the agent intended for THIS numbering
    pairs_path.write_text(json.dumps(pairs))
    # the recap is re-finalized with different sentence splits: numbering moved
    cues_mod.write_outputs(tmp_path, "1",
                           "{%s} One split changed here.\n\n{%s} Two.\n\n{%s} Three."
                           % (ids[0], ids[1], ids[0]), set(ids))
    with pytest.raises(UserError, match="older cues.json"):
        pairing.apply(tmp_path, "1", log=lambda *_: None)


# ---------------------------------------------------------------- incident 6


def test_incident_6_substitution_is_credited_by_the_sync_audit():
    """A dropped tagged panel replaced by a neighbour passes the audit when the
    plan records the substitution — instead of failing the original tag at 0%
    forever (and only manual edits to BOTH pairs.json and cues.json fixed it)."""
    from manhwa_ocr.video import syncaudit

    def seg(n, panel, start, end, **kw):
        return {"sentence": n, "text": f"s{n}", "start": start, "end": end,
                "display_action": "SHOW", "selected_panel": panel,
                "tagged_panels": [panel], "sequence": [panel], "narration_type": "VISUAL_ACTION",
                **kw}

    segments = [seg(1, "P001-01", 0.0, 3.2), seg(2, "P001-02", 3.2, 6.4)]
    plan = {"chapter": "1", "duration": 6.4, "shots": [
        {"id": "S001", "start": 0.0, "end": 3.2, "cards": [{"panel": "P001-01", "at": 0}]},
        {"id": "S002", "start": 3.2, "end": 6.4, "cards": [{"panel": "P001-01", "at": 0}]},
    ]}
    # P001-02 was dropped at plan time; the plan substituted P001-01 for it.
    plan["substitutions"] = [{"dropped": "P001-02", "shown": "P001-01", "reason": "mask risk"}]
    report = syncaudit.audit(plan, {"segments": segments}, [])
    cov2 = next(c for c in report["tagged_coverage"] if c["sentence"] == 2)
    assert cov2["pass"], report["gate"]
    # and without the record, the same plan FAILS sentence 2 (the old trap)
    report_old = syncaudit.audit({**plan, "substitutions": []}, {"segments": segments}, [])
    cov2_old = next(c for c in report_old["tagged_coverage"] if c["sentence"] == 2)
    assert not cov2_old["pass"]


def test_incident_6_pair_apply_consumes_substitutions_into_cues(tmp_path):
    from manhwa_ocr.video import pairing

    ids = _pairing_fixture(tmp_path)
    subs_path = tmp_path / ".work" / "video" / "ch1" / "substitutions.json"
    subs_path.parent.mkdir(parents=True, exist_ok=True)
    subs_path.write_text(json.dumps({"chapter": "1", "substitutions": [
        {"dropped": ids[1], "shown": ids[0], "reason": "mask risk: dominant bubble"}]}))
    pairing.write_task(tmp_path, "1", None, log=lambda *_: None)
    pairing.apply(tmp_path, "1", log=lambda *_: None)
    cues_data = json.loads((tmp_path / "recaps" / "chapter_1.cues.json").read_text())
    assert cues_data["sentences"][1]["panels"] == [ids[0]]   # re-tagged to what shows
    assert not subs_path.exists()                            # consumed, never re-applied


# ---------------------------------------------------------------- incident 10 + 15 + 19


def _rgb(colour, w=64, h=36):
    return np.zeros((h, w, 3), dtype=np.uint8) + np.array(colour, dtype=np.uint8)


def _placed_shot(spec_overrides=None):
    spec = {"off_x": 640.0, "off_y": 0.0, "base_w": 640.0, "base_h": 1080.0, "touches": "top_bottom"}
    spec.update(spec_overrides or {})
    return {"id": "S001", "placed": True, "cards": [{"placed": spec}]}


def test_incident_10_frameqc_flags_white_margins_inside_the_foreground():
    """A pure (ink-free) white margin FAILS when prep could not guarantee the
    unit's bounds: a trim-fallback unit (placed untrimmed) or a shave capped
    at its 40% limit — the classes that leave baked margins in the frame."""
    from manhwa_ocr.video import frameqc

    frame = _rgb((20, 20, 20))
    frame[:, 32:, :] = 250          # right half: PURE white margin, no ink
    for why, over in (("fallback", {"trim": [0.0, 0.0, 1.0, 1.0], "trim_fallback": True}),
                      ("capped", {"trim": [0.0, 0.0, 0.60, 1.0], "trim_fallback": False})):
        # both variants leave the RIGHT side unguaranteed (fallback unit, or
        # the right shave capped at 40%: 1 - 0.60 = 0.40)
        out = frameqc._shot_failures(_placed_shot(over), {"start": frame, "mid": frame, "end": frame},
                                     scale=1.0 / 3.0)
        assert any("white margin" in f for f in out["failures"]), (why, out)
        assert out["checks"]["white_margins"]["right"] > frameqc.WHITE_FAIL


def test_incident_10_frameqc_white_art_and_ink_bands_are_notes_not_failures():
    """Two classes a pixel metric cannot condemn, both reviewer NOTES:
    a near-white band carrying art (SFX overflow — shaving amputates it), and
    art that is legitimately white at its edge (foam/sky/logo canvas, where
    prep's trim verdict is trusted)."""
    from manhwa_ocr.video import frameqc

    # 1) ink-bearing band (SFX strokes inside the white right band)
    frame = _rgb((20, 20, 20))
    frame[:, 32:, :] = 250
    frame[10:13, 54:58, :] = 10
    out = frameqc._shot_failures(_placed_shot(), {"start": frame, "mid": frame, "end": frame},
                                 scale=1.0 / 3.0)
    assert not out["failures"], out
    assert any("carries art" in n for n in out["notes"]), out
    # 2) ink-free white band with TRUSTED bounds (trim shaved, not capped,
    #    not a fallback): the art itself is white at its edge
    frame2 = _rgb((20, 20, 20))
    frame2[:, 32:, :] = 250
    out2 = frameqc._shot_failures(
        _placed_shot({"trim": [0.0, 0.0, 1.0, 0.9], "trim_fallback": False}),
        {"start": frame2, "mid": frame2, "end": frame2}, scale=1.0 / 3.0)
    assert not out2["failures"], out2
    assert any("art is white" in n for n in out2["notes"]), out2


def test_incident_10_frameqc_accepts_art_that_reaches_the_walls():
    from manhwa_ocr.video import frameqc

    spec = {"off_x": 640.0, "off_y": 0.0, "base_w": 640.0, "base_h": 1080.0, "touches": "top_bottom"}
    frame = _rgb((30, 120, 60))     # busy art everywhere in the foreground
    shot = {"id": "S001", "placed": True, "cards": [{"placed": spec}]}
    out = frameqc._shot_failures(shot, {"start": frame, "mid": frame, "end": frame}, scale=1.0 / 3.0)
    assert not out["failures"], out
    assert out["checks"]["motion"] == 0.0    # a placed shot must not move


def test_incident_9_frameqc_flags_motion_in_a_placed_shot():
    from manhwa_ocr.video import frameqc

    spec = {"off_x": 0.0, "off_y": 0.0, "base_w": 1920.0, "base_h": 1080.0, "touches": "both"}
    a, b = _rgb((30, 120, 60)), _rgb((200, 60, 60))
    shot = {"id": "S001", "placed": True, "cards": [{"placed": spec}]}
    out = frameqc._shot_failures(shot, {"start": a, "mid": b, "end": b}, scale=1.0 / 30.0)
    assert any("moves" in f for f in out["failures"]), out


# ---------------------------------------------------------------- incident 11


def test_incident_11_pan_viewport_stays_inside_the_plan_window():
    """The pan overshoot (ch7 S43: visible bottom ~0.04 below the window) can
    never recurse: the sampled viewport stays inside the card's crop region at
    every point of the motion, pan and zoom both."""
    from manhwa_ocr.video.frames import Card, Format

    fmt = Format(1920, 1080, 30)
    panel = Image.new("RGB", (900, 2400), (40, 90, 40))
    for motion in ("pan_down", "pan_up", "pan_left", "pan_right", "push_in", "pull_out"):
        crop = [0.1, 0.2, 0.9, 0.8]
        card = Card(panel, {"crop": crop, "enter": "cut", "at": 0.0, "motion": motion,
                            "wide_display": True, "wide_zoom_scale": 0.5},
                    "single", 0, 0.0, 4.0, fmt, None)
        sw, sh = card.src.size
        for t in (0.0, 0.5, 1.0, 2.0, 3.9):
            x0, y0, vw, vh = card._viewport(t, 0.0)
            assert -1.0 <= x0 and x0 + vw <= sw + 1.0, (motion, t, x0, vw, sw)
            assert -1.0 <= y0 and y0 + vh <= sh + 1.0, (motion, t, y0, vh, sh)


# ---------------------------------------------------------------- incident 12


def test_incident_12_placed_mode_pins_captions_off():
    from manhwa_ocr.video.plan import build_plan

    panels = {"panels": [{"id": "P001-01", "width": 800, "height": 1200}], "norm_width": 800,
              "fingerprint": "fp"}
    units = [{"start": 0.0, "end": 4.0, "panels": ["P001-01"], "cues": [], "emphasis": [],
              "sentence": "A placed sentence.", "sentence_starts": [0.0]}]
    plan = build_plan(chapter="1", panels_data=panels, duration=4.0, audio=None,
                      vcfg={"mode": "placed", "format": "16:9", "fps": 30, "captions": True,
                            "min_hold": 3.0},
                      image_for=lambda p: Image.new("RGB", (800, 1200), (60, 80, 120)),
                      alignment_units=units, alignment_meta={"sync_mode": "synced"})
    assert plan["captions_enabled"] is False, "placed panels touch the bottom wall; captions would sit on art"


def test_incident_12_wide_whole_panel_flags_caption_band_text():
    from manhwa_ocr.video.crops import content_bands  # noqa: F401 - sanity import
    from manhwa_ocr.video.plan import wide_crop

    # A wide panel with a bright text band near its bottom: whole-panel wide
    # cards used to skip the caption-zone check entirely.
    img = Image.new("RGB", (1600, 902), (30, 30, 30))
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    for x in range(0, 1600, 6):     # striped "text" band, bottom 10%
        d.line((x, 812, x, 892), fill=(240, 240, 240), width=2)
    _, info = wide_crop(1600, 902, [0.0, 0.0, 1.0, 1.0], 800.0, img=img)
    assert info["whole"] is True
    assert info["caption_risk"] is True


# ---------------------------------------------------------------- incident 18


def test_incident_18_force_bypasses_the_whisper_cache_read(tmp_path, monkeypatch):
    from manhwa_ocr.video import sync as sync_mod

    audio = tmp_path / "a.wav"
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 1600)
    audio.write_bytes(buf.getvalue())

    fake_model_calls = []

    class FakeSeg:
        text = "hello"
        t0, t1 = 0, 100

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            fake_model_calls.append(1)
            return [FakeSeg()]

    monkeypatch.setattr(sync_mod, "available", lambda: True)
    pytest.importorskip("pywhispercpp.model")
    import pywhispercpp.model as pwm

    monkeypatch.setattr(pwm, "Model", FakeModel)

    cache = tmp_path / "timeline.json"
    sync_mod.transcribe(audio, model="tiny.en", cache=cache, log=lambda *_: None)
    assert fake_model_calls
    n_after_first = len(fake_model_calls)
    # a plain re-run reuses the cache: no re-transcription
    sync_mod.transcribe(audio, model="tiny.en", cache=cache, log=lambda *_: None)
    assert len(fake_model_calls) == n_after_first
    # --force re-transcribes even on a key match (the true cascade)
    sync_mod.transcribe(audio, model="tiny.en", cache=cache, log=lambda *_: None, force=True)
    assert len(fake_model_calls) > n_after_first
    assert cache.exists()


def test_incident_18_pipeline_forces_align_to_retranscribe(monkeypatch, tmp_path):
    from manhwa_ocr.video import align_engine, pipeline

    seen = {}

    class FakeSess:
        path = tmp_path
        slug = "s"

    def fake_build_alignment(root, chapter, cfg, *, sync="auto", log=print, force=False):
        seen["force"] = force
        return {"segments": [{"sentence": 1, "start": 0, "end": 1, "display_action": "SHOW",
                              "selected_panel": "P001-01", "text": "x"}], "sync_mode": "synced"}

    monkeypatch.setattr(align_engine, "build_alignment", fake_build_alignment)
    ctx = pipeline.Ctx(FakeSess(), "1", None, lambda *_: None)
    ctx.force = True
    pipeline._align(ctx)
    assert seen["force"] is True


# ---------------------------------------------------------------- incident 22


def test_incident_22_voice_speed_validation():
    from manhwa_ocr.video import tts

    with pytest.raises(UserError, match="No TTS voice"):
        tts.resolve_voice_and_speed(None, None)
    assert tts.resolve_voice_and_speed("aura-2-odysseus-en", None) == ("aura-2-odysseus-en", 1.23)
    assert tts.resolve_voice_and_speed("aura-2-odysseus-en", 1.35) == ("aura-2-odysseus-en", 1.35)
    with pytest.raises(UserError, match="range"):
        tts.resolve_voice_and_speed("aura-2-odysseus-en", 2.0)
    with pytest.raises(UserError, match="Aura-2"):
        tts.resolve_voice_and_speed("aura-1-helios-en", 1.2)     # explicit speed on aura-1 refuses
    assert tts.resolve_voice_and_speed("aura-1-helios-en", None) == ("aura-1-helios-en", None)


def test_incident_22_speed_param_omitted_for_non_aura2_models():
    from manhwa_ocr.video import tts

    captured = {}

    class _Op:
        def open(self, req, timeout=0):
            captured["url"] = req.full_url
            raise UserError("stop here")

    with pytest.raises(UserError):
        tts.deepgram_speak("Hi.", "aura-1-helios-en", "KEY", opener=_Op())
    assert "speed=" not in captured["url"]
    with pytest.raises(UserError):
        tts.deepgram_speak("Hi.", "aura-2-thalia-en", "KEY", speed=1.3, opener=_Op())
    assert "speed=1.3" in captured["url"]


def test_incident_22_em_dash_is_a_warning_double_hyphen_stays_an_error():
    from manhwa_ocr.video import tts_lint

    em = tts_lint.lint_sentence(1, "He turns — and the blade is already there.")
    dash = [i for i in em if i.code == "dash-pause"]
    assert dash and dash[0].severity == "warning" and not dash[0].is_error
    dh = tts_lint.lint_sentence(2, "He turns -- and stops.")
    ddash = [i for i in dh if i.code == "dash-pause"]
    assert ddash and ddash[0].is_error


def test_incident_22_bt709_color_tags_are_pinned():
    """The washed-out-colors fix: the encoder tags BT.709/tv explicitly."""
    import inspect

    from manhwa_ocr.video import ffmpeg

    src = inspect.getsource(ffmpeg.open_encoder)
    assert "out_color_matrix=bt709" in src
    assert '"-colorspace", "bt709"' in src
    assert '"-color_range", "tv"' in src


# ---------------------------------------------------------------- incident 23


def test_incident_23_render_lock_refuses_a_second_renderer(tmp_path):
    from manhwa_ocr.video.render import LOCK_STALE_HOURS, _acquire_render_lock

    out = tmp_path / "chapter_1.mp4"
    lock = _acquire_render_lock(out)
    assert lock.exists()
    with pytest.raises(UserError, match="Another render"):
        _acquire_render_lock(out)
    # a stale lock is debris, not a blocker
    old = time.time() - (LOCK_STALE_HOURS + 1) * 3600
    import os

    os.utime(lock, (old, old))
    lock2 = _acquire_render_lock(out)
    lock2.unlink(missing_ok=True)


def test_incident_23_endscreen_metadata_is_opt_in():
    assert DEFAULT_CONFIG["video"]["endscreen"] is False
    from manhwa_ocr.video import build as vb  # noqa: F401 - the explicit command remains
    assert callable(vb.make_end_screen)


def test_incident_23_placed_captions_default_off():
    assert DEFAULT_CONFIG["video"]["placed"]["captions"] is False
