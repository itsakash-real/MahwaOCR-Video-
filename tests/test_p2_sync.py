"""P2 — SYNC: auto-switch for layout-sensitive crops, one sync-gate code path,
timing warnings -> action, and the {same} + coverage trap (Ch2 sentence 60).

The gate tests reuse the hand-built plan/alignment fixtures from
test_sync_fixes (pure function over plan + alignment + whisper words).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.video import build as vb  # noqa: E402
from manhwa_ocr.video import syncaudit  # noqa: E402
from manhwa_ocr.video import validate as validate_mod  # noqa: E402
from manhwa_ocr.video import verify as verify_mod  # noqa: E402
from manhwa_ocr.video import align_engine  # noqa: E402
from manhwa_ocr.video import plan as plan_mod  # noqa: E402
from manhwa_ocr.video.frames import format_from  # noqa: E402
from manhwa_ocr.video.plan import build_plan  # noqa: E402
from manhwa_ocr.util import UserError  # noqa: E402


# ------------------------------------------------------------------ audit fixtures (mirror test_sync_fixes)


def _words(text: str, start=0.5, per=0.35):
    out, t = [], start
    for w in text.split():
        out.append({"w": w, "start": round(t, 3), "end": round(t + per * 0.8, 3)})
        t += per
    return out


def _shot(i: int, start: float, end: float, cards):
    return {"id": f"S{i:03d}", "start": round(start, 3), "end": round(end, 3), "layout": "single",
            "background": "blur", "exit": "cut", "effects": [], "sentence": None,
            "cards": [{"panel": pid, "crop": [0, 0, 1, 1], "enter": "cut", "at": at, "motion": "hold"}
                      for pid, at in cards]}


def _seg(n: int, start: float, end: float, text: str, tags, *, action="SHOW",
         selected=None, sequence=None):
    sel = selected if selected is not None else (tags[0] if tags else None)
    return {"sentence": n, "text": text, "start": float(start), "end": float(end), "tagged_panels": list(tags),
            "display_action": action, "selected_panel": sel, "selected_by_align": sel,
            "candidates": [], "confidence": "test",
            "sequence": list(sequence) if sequence is not None else ([sel] if sel else [])}


def _run_audit(shots, segs, words, duration, **kw):
    return syncaudit.audit({"chapter": "1", "duration": duration, "shots": shots},
                           {"segments": segs}, words, **kw)


# ------------------------------------------------------------------ auto-switch: find_pair_safe_shape


def _fake_pick_factory(single, pair):
    """Single card when 4:3 is among the candidates (single/wide/union lists),
    pair card when it is not (pair-only lists)."""
    def fake_pick(img, shapes, maps=None):
        if any(abs(float(s) - 1.333) < 0.01 for s in shapes):
            return dict(single)
        return dict(pair)
    return fake_pick


def _clean_card(shape, box=(0, 0, 1, 1)):
    return {"shape": shape, "box": box, "pad_y": 0.0, "problems": [], "clean": True}


def test_pair_safe_switch_uses_narrower_shape_when_both_layouts_clean(monkeypatch):
    """Single 4:3 clean, pair 3:4 clean -> switch to 3:4 everywhere (Ch4)."""
    monkeypatch.setattr(verify_mod, "pick_card",
                        _fake_pick_factory(_clean_card(1.333), _clean_card(0.75, (0, 0.1, 1, 0.9))))
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    # Realistic shape lists: pair shapes are a subset of single's, so the
    # pair-safe target is valid for the single layout it will be used in.
    switch = verify_mod.find_pair_safe_shape(
        img, {}, {"single": [0.5625, 0.75, 1.0, 1.333, 1.777], "pair": [0.5625, 0.75, 1.0]})
    assert switch is not None
    assert switch["from_shape"] == pytest.approx(1.333)
    assert switch["to_shape"] == pytest.approx(0.75)
    assert switch["box"] == (0, 0.1, 1, 0.9) or list(switch["box"]) == [0, 0.1, 1, 0.9]


def test_pair_safe_switch_is_none_when_pair_has_no_clean_crop(monkeypatch):
    """Pair crop carries a problem -> no silent switch to a problematic crop;
    the panel stays a reviewer decision (P1 layout_sensitive flag)."""
    bad_pair = {"shape": 0.75, "box": (0, 0, 1, 1), "pad_y": 0.0,
                "problems": ["lettering"], "clean": False}
    monkeypatch.setattr(verify_mod, "pick_card", _fake_pick_factory(_clean_card(1.333), bad_pair))
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    assert verify_mod.find_pair_safe_shape(img, {}, {"single": [1.333], "pair": [0.75]}) is None


def test_pair_safe_switch_is_none_when_shapes_already_agree(monkeypatch):
    monkeypatch.setattr(verify_mod, "pick_card",
                        _fake_pick_factory(_clean_card(0.75), _clean_card(0.75)))
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    assert verify_mod.find_pair_safe_shape(img, {}, {"single": [0.75], "pair": [0.75]}) is None


def test_pair_safe_switch_needs_pair_layout(monkeypatch):
    monkeypatch.setattr(verify_mod, "pick_card",
                        _fake_pick_factory(_clean_card(1.333), _clean_card(0.75)))
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    assert verify_mod.find_pair_safe_shape(img, {}, {"single": [1.333]}) is None
    assert verify_mod.find_pair_safe_shape(img, {}, None) is None


# ------------------------------------------------------------------ verify reports the switch; planner applies it


def _head_maps():
    return {"_head": {"top": 0.1, "bottom": 0.4, "source": "skin", "confident": True,
                      "figure": True, "keep_top": None},
            "painted": [], "edge_rows": (0.0, 0.0)}


def test_verify_reports_layout_switch(monkeypatch):
    """verify_chapter surfaces the switch so the reviewer sees what the
    planner is about to change (no silent change)."""
    monkeypatch.setattr(verify_mod, "_panel_maps", lambda img, options=None: _head_maps())
    monkeypatch.setattr(verify_mod, "is_mask_risk", lambda maps: False)
    monkeypatch.setattr(verify_mod, "pick_card",
                        _fake_pick_factory(_clean_card(1.333), _clean_card(0.75, (0, 0.1, 1, 0.9))))
    # The main card uses the union shapes; the per-layout picks drive the switch.
    panels = [{"id": "P001-01", "width": 300, "height": 400}]
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    report = verify_mod.verify_chapter(panels, lambda p: img, [0.75, 1.333],
                                       layout_shapes={"single": [0.5625, 0.75, 1.0, 1.333, 1.777],
                                                      "pair": [0.5625, 0.75, 1.0]})
    assert report["layout_switches"]["P001-01"]["to_shape"] == pytest.approx(0.75)
    assert report["layout_sensitive"] == []  # both layouts clean: not "sensitive", just switched


def _designer(monkeypatch, *, fixes=None):
    """A _Designer with pick_card/_panel_maps faked so crops are controlled."""
    monkeypatch.setattr(plan_mod, "_panel_maps", lambda img, options=None: _head_maps())
    monkeypatch.setattr(verify_mod, "pick_card",
                        _fake_pick_factory(_clean_card(1.333), _clean_card(0.75, (0, 0.1, 1, 0.9))))
    monkeypatch.setattr(plan_mod, "pick_card",
                        _fake_pick_factory(_clean_card(1.333), _clean_card(0.75, (0, 0.1, 1, 0.9))))
    panels = [{"id": "P001-01", "width": 300, "height": 400}]
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    fmt = format_from("16:9", 30)
    return plan_mod._Designer("1", panels, fmt, {"format": "16:9", "fps": 30},
                              lambda p: img, fixes=fixes)


def test_planner_auto_switches_single_crop_to_pair_safe_shape(monkeypatch):
    d = _designer(monkeypatch)
    box = d.crop("P001-01", "single")
    assert box == [0, 0.1, 1, 0.9]  # the pair-safe 3:4 box, not the 4:3 one
    assert len(d.crop_stats["layout_switches"]) == 1
    sw = d.crop_stats["layout_switches"][0]
    assert sw["panel"] == "P001-01" and sw["to_shape"] == pytest.approx(0.75)


def test_planner_switches_a_fixes_crop_whose_aspect_is_not_allowed_in_pair(monkeypatch):
    """Ch4: a reviewer's 4:3 fixes.json crop fails QA in a pair shot -> the
    planner switches it to the clean 3:4 crop for the pair layout and records it."""
    # Box [0,0,1,0.5625] on a 300x400 panel has aspect exactly 4:3.
    d = _designer(monkeypatch, fixes={"crop": {"P001-01": (1.333, [0, 0, 1, 0.5625])}})
    box = d.crop("P001-01", "pair")
    assert box == [0, 0.1, 1, 0.9]
    assert d.crop_stats["layout_switches"][0]["from_shape"] == "fixes.json"
    # The same fixes crop in a single shot (where 4:3 is allowed) is untouched.
    d2 = _designer(monkeypatch, fixes={"crop": {"P001-01": (1.333, [0, 0, 1, 0.5625])}})
    assert d2.crop("P001-01", "single") == [0, 0, 1, 0.5625]
    assert d2.crop_stats["layout_switches"] == []


# ------------------------------------------------------------------ build_plan / QA carry timing warnings + switches


def test_build_plan_stores_timing_warnings_from_alignment_meta(monkeypatch):
    warnings = [{"code": "whisper_unavailable", "message": "whisper unavailable",
                 "fix": "install pywhispercpp"}]
    # Stub shot rendering: this test is about the meta plumbing
    # (timing_warnings riding with the plan), not about crops.
    monkeypatch.setattr(plan_mod._Designer, "shot",
                        lambda self, i, u, last=False: {"id": f"S{i:03d}", "start": 0.0, "end": 2.0,
                                                        "layout": "single", "cards": []})
    img = Image.new("RGB", (300, 400), (120, 90, 70))
    plan = build_plan(chapter="1", panels_data={"panels": [{"id": "P001-01", "width": 300, "height": 400}]},
                      duration=10.0, audio=None,
                      vcfg={"format": "16:9", "fps": 30, "pair_ratio": 0.3,
                            "music": None, "music_volume": 0.22, "effects": {}},
                      image_for=lambda p: img,
                      sentences=[{"text": "Hello world", "panels": ["P001-01"], "start": 0.0}],
                      alignment_meta={"sync_mode": "synced", "review_queue": [],
                                     "timing_warnings": warnings})
    assert plan["timing_warnings"] == warnings


def test_quality_report_surfaces_switches_and_timing_warnings():
    plan = {"chapter": "1", "format": "16:9", "fps": 30, "sync": "synced",
            "crop_stats": {"layout_switches": [{"panel": "P001-01", "from_shape": 1.333, "to_shape": 0.75}]},
            "timing_warnings": [{"code": "sync_off", "message": "m", "fix": "f"}],
            "timing": {}, "alignment_review_queue": [], "shots": []}
    qa = vb.quality_report(plan, {"panels": []}, None, 3.0)
    assert qa["layout_switches"] == plan["crop_stats"]["layout_switches"]
    assert qa["timing_warnings"] == plan["timing_warnings"]


# ------------------------------------------------------------------ one gate code path: evaluate_gate / gate_from_report


def test_evaluate_gate_edge_cases():
    st = {"gate_wrong": 0.05, "gate_window": 0.25, "gate_grace": 0.2, "max_late": 0.3}
    # Exactly at the wrong-picture bar -> PASS (the gate is a strict over).
    assert syncaudit.evaluate_gate({"ratio": 0.05, "seconds": 1.0}, [], [], st) == []
    # Just over -> FAIL.
    fails = syncaudit.evaluate_gate({"ratio": 0.0501, "seconds": 1.0}, [], [], st)
    assert any("wrong-picture" in f for f in fails)
    # Any over-gate window fails; any late cut fails; nothing fails -> PASS.
    over = [{"start": 6.0, "wrong": 1.0, "wrong_seconds": 5.0, "match": 0.0, "samples": 10}]
    assert any("five-second window" in f for f in syncaudit.evaluate_gate({"ratio": 0.0}, over, [], st))
    late = [{"offset": 0.6, "sentence": 2}]
    assert any("cut" in f for f in syncaudit.evaluate_gate({"ratio": 0.0}, [], late, st))
    assert syncaudit.evaluate_gate({"ratio": 0.0}, [], [], st) == []


def test_gate_from_report_re_evaluates_cached_numbers_with_current_settings():
    segs = [_seg(1, 0.0, 6.0, "Ron draws his sword fast", ["P2"]),
            _seg(2, 6.0, 12.0, "The demon king laughs loud", ["P3"])]
    shots = [_shot(1, 0.0, 12.0, [("P2", 0.0)])]
    rep = _run_audit(shots, segs, _words("Ron draws his sword fast the demon king laughs loud"), 12.0)
    assert rep["gate"]["pass"] is False
    # Same numbers, same settings -> same verdict.
    assert syncaudit.gate_from_report(rep, rep["settings"]) == rep["gate"]
    # Same numbers, stricter settings (gate_wrong 0.0) -> still FAIL, via the
    # same code path audit() uses.
    strict = {**rep["settings"], "gate_wrong": 0.0}
    assert syncaudit.gate_from_report(rep, strict)["pass"] is False
    # A passing report with a small nonzero wrong-picture share (1/60) still
    # passes under a looser bar...
    segs_ok = [_seg(1, 0.0, 6.0, "Ron draws his sword fast", ["P2"]),
               _seg(2, 6.0, 30.0, "The demon king laughs loud", ["P3"])]
    shots_ok = [_shot(1, 0.0, 6.5, [("P2", 0.0)]), _shot(2, 6.5, 30.0, [("P3", 0.0)])]
    rep_ok = _run_audit(shots_ok, segs_ok, _words("Ron draws his sword fast the demon king laughs loud"), 30.0)
    assert rep_ok["gate"]["pass"] is True
    assert rep_ok["wrong_picture"]["ratio"] > 0.0
    assert syncaudit.gate_from_report(rep_ok, {**rep_ok["settings"], "gate_wrong": 0.99})["pass"] is True
    # ... but the same passing numbers under a stricter bar FAIL: a cached
    # PASS never sneaks past a stricter config.
    assert syncaudit.gate_from_report(rep_ok, {**rep_ok["settings"], "gate_wrong": 0.01})["pass"] is False


def test_audit_is_deterministic():
    segs = [_seg(1, 0.0, 6.0, "Ron draws his sword fast", ["P2"]),
            _seg(2, 6.0, 12.0, "The demon king laughs loud", ["P3"])]
    shots = [_shot(1, 0.0, 12.0, [("P2", 0.0)])]
    words = _words("Ron draws his sword fast the demon king laughs loud")
    a = _run_audit(shots, segs, words, 12.0)
    b = _run_audit(shots, segs, words, 12.0)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_gate_window_needs_a_full_second_of_wrong_picture_exactly():
    """1.0s of wrong picture in settled time fails the window gate; 0.5s does not
    (mirrors the existing half-second fixture, at the exact boundary)."""
    segs = [_seg(1, 0.0, 20.0, "Ron draws his sword fast", ["P2"]),
            _seg(2, 20.0, 22.0, "He raises the blade", ["P1"]),
            _seg(3, 22.0, 30.0, "The demon king laughs loud", ["P3"])]
    words = _words("Ron draws his sword fast he raises the blade the demon king laughs loud")
    # 0.5s flash (existing behaviour): no over-gate window.
    shots_half = [_shot(1, 0.0, 20.0, [("P2", 0.0)]), _shot(2, 20.0, 22.4, [("P1", 0.0)]),
                  _shot(3, 22.4, 23.0, [("P3", 0.0)]), _shot(4, 23.0, 23.5, [("P1", 0.0)]),
                  _shot(5, 23.5, 30.0, [("P3", 0.0)])]
    rep_half = _run_audit(shots_half, segs, words, 30.0, settings={"gate_window": 0.05})
    assert rep_half["five_second"]["over_gate"] == []
    # 1.0s flash: exactly the full second a window needs -> over-gate.
    shots_full = [_shot(1, 0.0, 20.0, [("P2", 0.0)]), _shot(2, 20.0, 22.4, [("P1", 0.0)]),
                  _shot(3, 22.4, 23.0, [("P3", 0.0)]), _shot(4, 23.0, 24.0, [("P1", 0.0)]),
                  _shot(5, 24.0, 30.0, [("P3", 0.0)])]
    rep_full = _run_audit(shots_full, segs, words, 30.0, settings={"gate_window": 0.05})
    assert rep_full["five_second"]["over_gate"] != []
    assert any("five-second window" in f for f in rep_full["gate"]["failures"])


def test_late_cut_at_exactly_max_late_plus_grace_passes():
    """Cut exactly max_late (0.3) + gate_grace (0.2) = 0.5s late -> PASS;
    0.6s late -> FAIL (late cut), with the wrong-picture share kept under the
    bar by a long tail so the late cut is the only thing that can fail."""
    segs = [_seg(1, 0.0, 6.0, "Ron draws his sword fast", ["P2"]),
            _seg(2, 6.0, 30.0, "The demon king laughs loud", ["P3"])]
    words = _words("Ron draws his sword fast the demon king laughs loud")
    shots_at = [_shot(1, 0.0, 6.5, [("P2", 0.0)]), _shot(2, 6.5, 30.0, [("P3", 0.0)])]
    rep_at = _run_audit(shots_at, segs, words, 30.0)
    assert rep_at["late_cuts"] == []
    assert rep_at["gate"]["pass"] is True
    shots_over = [_shot(1, 0.0, 6.6, [("P2", 0.0)]), _shot(2, 6.6, 30.0, [("P3", 0.0)])]
    rep_over = _run_audit(shots_over, segs, words, 30.0)
    assert len(rep_over["late_cuts"]) == 1
    assert any("cut" in f for f in rep_over["gate"]["failures"])


def test_pair_second_card_is_expected_not_wrong_ch5_pattern():
    """Ch5 fix pattern: a long (7-14s) sentence gets a SECOND tag so the
    panel that fills its tail is expected, not wrong-picture. Without the
    second tag the same plan fails the gate."""
    # With the second tag: P2 shows during sentence 1, but sentence 1 owns P2.
    segs_two = [_seg(1, 0.0, 10.0, "Ron draws his sword fast", ["P1", "P2"], sequence=["P1", "P2"]),
                _seg(2, 10.0, 20.0, "The demon king laughs loud", ["P2"])]
    shots = [_shot(1, 0.0, 5.0, [("P1", 0.0)]), _shot(2, 5.0, 10.0, [("P2", 0.0)]),
             _shot(3, 10.0, 20.0, [("P2", 0.0)])]
    rep_two = _run_audit(shots, segs_two, _words("Ron draws his sword fast the demon king laughs loud"), 20.0)
    assert rep_two["wrong_picture"]["ratio"] == 0.0
    assert rep_two["gate"]["pass"] is True
    # Without it: the same P2 tail is another sentence's panel -> wrong.
    segs_one = [_seg(1, 0.0, 10.0, "Ron draws his sword fast", ["P1"], sequence=["P1"]),
                _seg(2, 10.0, 20.0, "The demon king laughs loud", ["P2"])]
    rep_one = _run_audit(shots, segs_one, _words("Ron draws his sword fast the demon king laughs loud"), 20.0)
    assert rep_one["wrong_picture"]["ratio"] > 0.0
    assert rep_one["gate"]["pass"] is False


# ------------------------------------------------------------------ render gate: cached audit reuse


class _Sess:
    def __init__(self, path):
        self.path, self.slug = path, "ws"


def _render_setup(tmp_path, *, cached_settings=None, cached_gate_pass=True):
    """A session dir with a synced plan, a cached audit file, and a plan file
    the audit is newer than. Returns (sess, cfg, calls)."""
    root = tmp_path / "sess"
    (root / "video").mkdir(parents=True)
    cfg = SimpleNamespace(video={"sync": {}, "format": "16:9", "fps": 30})
    from manhwa_ocr.video.sync import effective_settings
    st = effective_settings(cfg.video, log=lambda _m: None)
    cached = {"gate": {"pass": cached_gate_pass,
                       "failures": [] if cached_gate_pass else ["wrong-picture time 5.0s (50.0% of judged) is over 5%"]},
              "wrong_picture": {"ratio": 0.0 if cached_gate_pass else 0.5, "seconds": 0.0 if cached_gate_pass else 5.0},
              "five_second": {"over_gate": [], "worst": {"wrong": 0.0}},
              "late_cuts": [],
              "settings": cached_settings if cached_settings is not None else st}
    plan_file = root / "video" / "chapter_1.plan.json"
    plan_file.write_text(json.dumps({"sync": "synced", "timing_warnings": [],
                                     "alignment_review_queue": [], "panels_fingerprint": None}),
                         encoding="utf-8")
    audit_file = root / "video" / "chapter_1.syncaudit.json"
    audit_file.write_text(json.dumps(cached), encoding="utf-8")
    # Audit newer than the plan -> the render gate may reuse it.
    import os, time
    now = time.time()
    os.utime(plan_file, (now - 10, now - 10))
    os.utime(audit_file, (now, now))
    calls = []
    return root, cfg, calls


def test_render_reuses_a_cached_audit_with_matching_settings(monkeypatch, tmp_path):
    root, cfg, calls = _render_setup(tmp_path)
    sess = _Sess(root)
    monkeypatch.setattr(vb, "load_panels", lambda root_, ch: {"panels": [{"id": "P001-01"}]})
    monkeypatch.setattr(vb, "load_plan", lambda root_, ch, ids: json.loads(
        (root / "video" / "chapter_1.plan.json").read_text(encoding="utf-8")))
    monkeypatch.setattr(vb, "render", lambda *a, **k: calls.append(1) or (root / "video" / "chapter_1.mp4"))
    made = []
    monkeypatch.setattr(syncaudit, "make_report",
                        lambda *a, **k: made.append(1) or {"gate": {"pass": True, "failures": []}})
    logs = []
    vb.render_chapter(sess, "1", cfg, log=logs.append)
    assert calls == [1]  # rendered
    assert made == []  # cached audit reused, not recomputed
    assert any("existing audit" in line for line in logs)


def test_render_recomputes_a_cached_audit_when_settings_changed(monkeypatch, tmp_path):
    from manhwa_ocr.video.sync import effective_settings
    cfg_probe = SimpleNamespace(video={"sync": {}, "format": "16:9", "fps": 30})
    st = effective_settings(cfg_probe.video, log=lambda _m: None)
    stale_settings = {**st, "gate_wrong": 0.99}  # cached under a looser bar
    root, cfg, calls = _render_setup(tmp_path, cached_settings=stale_settings, cached_gate_pass=True)
    sess = _Sess(root)
    monkeypatch.setattr(vb, "load_panels", lambda root_, ch: {"panels": [{"id": "P001-01"}]})
    monkeypatch.setattr(vb, "load_plan", lambda root_, ch, ids: json.loads(
        (root / "video" / "chapter_1.plan.json").read_text(encoding="utf-8")))
    monkeypatch.setattr(vb, "render", lambda *a, **k: calls.append(1) or (root / "video" / "chapter_1.mp4"))
    made = []
    fresh = {"gate": {"pass": False, "failures": ["wrong-picture time 5.0s (50.0% of judged) is over 5%"]},
             "wrong_picture": {"ratio": 0.5}, "five_second": {"worst": {"wrong": 0.5}}}
    monkeypatch.setattr(syncaudit, "make_report", lambda *a, **k: made.append(1) or fresh)
    with pytest.raises(UserError, match="sync gate"):
        vb.render_chapter(sess, "1", cfg, log=lambda *_: None)
    assert made == [1]  # stale cache discarded, audit recomputed
    assert calls == []  # nothing rendered


def test_render_estimated_refusal_names_the_timing_warning(monkeypatch, tmp_path):
    root = tmp_path / "sess"
    (root / "video").mkdir(parents=True)
    cfg = SimpleNamespace(video={"sync": {}, "format": "16:9", "fps": 30})
    sess = _Sess(root)
    plan = {"sync": "estimated",
            "timing_warnings": [{"code": "whisper_unavailable", "message": "whisper is not installed",
                                 "fix": "pip install pywhispercpp"}],
            "alignment_review_queue": [], "panels_fingerprint": None}
    monkeypatch.setattr(vb, "load_panels", lambda root_, ch: {"panels": [{"id": "P001-01"}]})
    monkeypatch.setattr(vb, "load_plan", lambda root_, ch, ids: plan)
    with pytest.raises(UserError) as exc:
        vb.render_chapter(sess, "1", cfg, log=lambda *_: None)
    assert "whisper_unavailable" in str(exc.value)
    assert "pip install pywhispercpp" in str(exc.value)


# ------------------------------------------------------------------ {same} + coverage trap (Ch2 sentence 60)


def _validate_segments():
    order = {"P001-01": 0, "P001-02": 1, "P001-03": 2}
    return order


def test_validate_fails_same_hiding_a_skipped_panel():
    """Ch2 S60: {same} holds P001-01 while the story jumps to P001-03,
    skipping P001-02 — FAIL at align time, with the fix named."""
    order = _validate_segments()
    segs = [
        {"sentence": 1, "start": 0.0, "end": 2.0, "narration_type": "VISUAL_ACTION",
         "selected_panel": "P001-01", "candidate_window": ["P001-01"], "display_action": "SHOW",
         "confidence": "ocr_match"},
        {"sentence": 2, "start": 2.0, "end": 4.0, "narration_type": "SAME",
         "selected_panel": None, "candidate_window": [], "display_action": "HOLD_PREVIOUS",
         "confidence": "rule:same_hold"},
        {"sentence": 3, "start": 4.0, "end": 6.0, "narration_type": "VISUAL_ACTION",
         "selected_panel": "P001-03", "candidate_window": ["P001-03"], "display_action": "SHOW",
         "confidence": "tag_confirmed_in_window"},
    ]
    result = validate_mod.validate(segs, order, duration=6.0)
    assert result["status"] == "FAIL"
    assert result["checks"]["same_no_hidden_panel"] == "FAIL"
    prob = [p for p in result["problems"] if p["check"] == "same_no_hidden_panel"]
    assert len(prob) == 1 and prob[0]["sentence"] == 2
    assert "P001-02" in prob[0]["detail"]  # the skipped panel is named
    assert validate_mod.review_queue(segs, result) == [2]


def test_validate_passes_same_with_no_jump_and_internal_across_a_jump():
    """{same} with the next panel adjacent -> PASS. An INTERNAL aside across
    a jump is legitimate narration, not a {same} claim -> PASS."""
    order = _validate_segments()
    segs = [
        {"sentence": 1, "start": 0.0, "end": 2.0, "narration_type": "VISUAL_ACTION",
         "selected_panel": "P001-01", "candidate_window": ["P001-01"], "display_action": "SHOW",
         "confidence": "ocr_match"},
        {"sentence": 2, "start": 2.0, "end": 4.0, "narration_type": "SAME",
         "selected_panel": None, "candidate_window": [], "display_action": "HOLD_PREVIOUS",
         "confidence": "rule:same_hold"},
        {"sentence": 3, "start": 4.0, "end": 6.0, "narration_type": "VISUAL_ACTION",
         "selected_panel": "P001-02", "candidate_window": ["P001-02"], "display_action": "SHOW",
         "confidence": "tag_confirmed_in_window"},
        {"sentence": 4, "start": 6.0, "end": 8.0, "narration_type": "INTERNAL",
         "selected_panel": None, "candidate_window": [], "display_action": "HOLD_PREVIOUS",
         "confidence": "rule:internal_hold"},
    ]
    # INTERNAL across a jump is not the {same} trap; extend the timeline with
    # a SHOW far ahead to prove INTERNAL is never flagged for it.
    segs.append({"sentence": 5, "start": 8.0, "end": 10.0, "narration_type": "VISUAL_ACTION",
                 "selected_panel": "P001-03", "candidate_window": ["P001-03"], "display_action": "SHOW",
                 "confidence": "tag_confirmed_in_window"})
    result = validate_mod.validate(segs, order, duration=10.0)
    assert result["checks"]["same_no_hidden_panel"] == "PASS"
    # The INTERNAL jump itself is still caught by no_unexplained_jumps only if
    # it lacks evidence — here the tag is confirmed, so the whole timeline passes.
    assert result["status"] == "PASS"


def test_impactful_filler_never_spills_into_a_hold_neighbour():
    """Fillers come only from a sentence's OWN window; a HOLD neighbour
    never attracts a filler, and a filler never leaks across a hold."""
    panels = [{"id": f"P001-0{i}", "text_only": False, "figure": 0.5,
               "scene": f"beat {i}"} for i in range(1, 7)]
    segs = [
        {"sentence": 1, "start": 0.0, "end": 10.0, "selected_panel": "P001-01",
         "display_action": "SHOW", "tagged_extras": []},
        {"sentence": 2, "start": 10.0, "end": 12.0, "selected_panel": None,
         "display_action": "HOLD_PREVIOUS", "tagged_extras": []},
        {"sentence": 3, "start": 12.0, "end": 22.0, "selected_panel": "P001-05",
         "display_action": "SHOW", "tagged_extras": []},
        {"sentence": 4, "start": 22.0, "end": 32.0, "selected_panel": "P001-06",
         "display_action": "SHOW", "tagged_extras": []},
    ]
    cov = align_engine.place_impactful(segs, panels, 32.0, 3.0)
    # Sentence 1 (10s) gets the one impactful filler from its own window
    # (P001-02, right after its tagged P001-01). Sentences 3 and 4 are 10s
    # too, but their windows hold no unpicked panel after their tags, so
    # they hold their tagged panels alone — nothing is placed beside the
    # HOLD, and the unpicked panels (P001-03/04) simply never show.
    assert segs[0]["sequence"] == ["P001-01", "P001-02"]
    assert segs[2]["sequence"] == ["P001-05"]
    assert segs[3]["sequence"] == ["P001-06"]
    assert cov["filler_shots"] == 1
