"""Gemini Vision step (video/gemini_vision.py): the reusable client
(key rotation, backoff, pacing, JSON mode), the vision trim decisions
for placed prep (validation, the ink sanity clamp, per-unit fallback,
the auditable artifact), plan consumption of vision bounds, and the
placement QC helper.

EVERY test mocks the HTTP transport or the client — no test makes a
real Gemini API call. Dummy keys live only in temp files created by
the tests, and one test asserts no dummy key material ever lands in a
written artifact.
"""

from __future__ import annotations

import json

import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.config import load_config
from manhwa_ocr.video import build as build_mod
from manhwa_ocr.video import gemini_vision as gv
from manhwa_ocr.video import plan as plan_mod
from manhwa_ocr.video.plan import build_plan

VCFG = load_config().video


# --------------------------------------------------------------- helpers


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def ok_response(obj) -> FakeResponse:
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return FakeResponse(200, {"candidates": [
        {"content": {"parts": [{"text": text}]}}]})


class FakeTransport:
    """Records (url, headers, payload); replays queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": dict(headers),
                           "payload": payload, "timeout": timeout})
        return self.responses.pop(0)

    @property
    def keys_used(self):
        return [c["headers"]["x-goog-api-key"] for c in self.calls]


def key_files(tmp_path, contents=("dummy-key-1", "dummy-key-2", "dummy-key-3")):
    files = []
    for i, value in enumerate(contents, start=1):
        p = tmp_path / f"gemini-{i}.key"
        p.write_text(value + "\n", encoding="utf-8")
        files.append(p)
    return files


def make_client(tmp_path, transport, **kw):
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("pace_seconds", 0)
    return gv.GeminiVisionClient(key_files=key_files(tmp_path),
                                 transport=transport, **kw)


def _textured_rect(d, box, fill=(170, 60, 50), alt=(60, 120, 160)):
    # Same coarse texture trick as test_placed: it must survive the
    # trim detector's 120px-wide analysis downscale.
    x0, y0, x1, y1 = box
    d.rectangle(box, fill=fill)
    cell = 36
    for yy in range(y0, y1 + 1, cell):
        for xx in range(x0, x1 + 1, cell):
            if ((xx - x0) // cell + (yy - y0) // cell) % 2:
                d.rectangle((xx, yy, min(xx + cell - 1, x1), min(yy + cell - 1, y1)), fill=alt)
    d.rectangle(box, outline=(30, 25, 20), width=3)


def margin_img(w=600, h=800, rect=(60, 200, 540, 700)):
    img = Image.new("RGB", (w, h), (255, 255, 255))
    _textured_rect(ImageDraw.Draw(img), rect)
    return img


class FakeClient:
    """A generate_json stand-in: returns a fixed payload or raises."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.model = "gemini-2.5-flash"
        self.calls = []

    def generate_json(self, parts):
        self.calls.append(parts)
        if self.error is not None:
            raise self.error
        return self.payload


# --------------------------------------------------------------- pins


def test_config_pins_match_module_constants():
    cfg = load_config().video["gemini_vision"]
    assert cfg["model"] == gv.GEMINI_VISION_MODEL == "gemini-2.5-flash"
    assert cfg["batch_size"] == gv.GEMINI_BATCH_SIZE == 6
    assert cfg["max_attempts"] == gv.GEMINI_MAX_ATTEMPTS == 6
    assert [p.rsplit("/", 1)[-1] for p in cfg["key_files"]] == \
        ["gemini-1.key", "gemini-2.key", "gemini-3.key"]
    assert [p.rsplit("/", 1)[-1] for p in gv.GEMINI_KEY_FILES] == \
        ["gemini-1.key", "gemini-2.key", "gemini-3.key"]


# --------------------------------------------------------------- client


def test_client_rotation_order_across_keys(tmp_path):
    transport = FakeTransport([ok_response({"n": i}) for i in range(4)])
    client = make_client(tmp_path, transport)
    for i in range(4):
        assert client.generate_json([{"text": "hi"}]) == {"n": i}
    assert transport.keys_used == ["dummy-key-1", "dummy-key-2",
                                   "dummy-key-3", "dummy-key-1"]
    # JSON response mode + the pinned endpoint/model, on every call.
    for call in transport.calls:
        assert call["url"].endswith("/models/gemini-2.5-flash:generateContent")
        assert call["payload"]["generationConfig"]["responseMimeType"] == "application/json"


def test_client_retry_and_rotate_on_429_then_success(tmp_path):
    sleeps = []
    transport = FakeTransport([FakeResponse(429), ok_response({"ok": True})])
    client = make_client(tmp_path, transport, sleep=sleeps.append)
    assert client.generate_json([{"text": "hi"}]) == {"ok": True}
    assert transport.keys_used == ["dummy-key-1", "dummy-key-2"]
    assert len(sleeps) == 1 and sleeps[0] > 0     # one jittered backoff


def test_client_retry_and_rotate_on_5xx_then_success(tmp_path):
    transport = FakeTransport([FakeResponse(503), FakeResponse(500), ok_response([1])])
    client = make_client(tmp_path, transport)
    assert client.generate_json([{"text": "hi"}]) == [1]
    assert transport.keys_used == ["dummy-key-1", "dummy-key-2", "dummy-key-3"]


def test_client_bounded_attempts_raise_typed_failure_without_key_material(tmp_path):
    transport = FakeTransport([FakeResponse(429)] * 10)
    client = make_client(tmp_path, transport, max_attempts=3)
    with pytest.raises(gv.GeminiVisionError) as exc:
        client.generate_json([{"text": "hi"}])
    assert len(transport.calls) == 3              # bounded total attempts
    assert "dummy-key" not in str(exc.value)
    assert "x-goog-api-key" not in str(exc.value)


def test_client_paces_successive_calls_sequentially(tmp_path):
    sleeps = []
    times = iter([100.0, 100.0, 105.0])
    transport = FakeTransport([ok_response({"a": 1}), ok_response({"b": 2})])
    client = make_client(tmp_path, transport, sleep=sleeps.append,
                         pace_seconds=5.0, clock=lambda: next(times))
    client.generate_json([{"text": "one"}])
    client.generate_json([{"text": "two"}])
    # The second call waited out the rest of the 5s pacing interval;
    # calls are issued one at a time by construction (no concurrency).
    assert sleeps == [5.0]


def test_client_missing_key_files_raise_typed_failure(tmp_path):
    client = gv.GeminiVisionClient(key_files=[tmp_path / "nope.key"],
                                   transport=FakeTransport([]),
                                   sleep=lambda _s: None, pace_seconds=0)
    with pytest.raises(gv.GeminiVisionError):
        client.generate_json([{"text": "hi"}])


# --------------------------------------------------------------- box validation


@pytest.mark.parametrize("raw", [
    None, "nope", [0.0, 0.0, 1.0], [0.0, 0.0, 1.0, 1.0, 0.5],
    [-0.1, 0.0, 1.0, 1.0],          # out of range (below 0)
    [0.0, 0.0, 1.2, 1.0],          # out of range (above 1)
    [0.5, 0.0, 0.5, 1.0],          # degenerate: zero width
    [0.0, 0.0, 0.04, 1.0],         # degenerate: width < 0.05
    [0.0, 0.0, 1.0, 0.04],         # degenerate: height < 0.05
    [0.9, 0.0, 0.1, 1.0],          # x0 > x1
    ["0", 0.0, 1.0, 1.0],          # not numbers
    [True, 0.0, 1.0, 1.0],         # bools are not numbers here
    [0.0, float("nan"), 1.0, 1.0],
])
def test_validation_rejects_out_of_range_and_degenerate_boxes(raw):
    assert gv.validate_vision_box(raw) is None


def test_validation_accepts_a_wellformed_box():
    assert gv.validate_vision_box([0, 0.1, 1, 0.9]) == (0.0, 0.1, 1.0, 0.9)


# --------------------------------------------------------------- trim decisions


def _units(*ids):
    return [{"id": uid, "image": margin_img()} for uid in ids]


def test_trim_parses_a_wellformed_response_and_batches_storyboard_style():
    # Ink bounds of margin_img() are ~(0.10, 0.25, 0.90, 0.875); this
    # vision box CONTAINS them, so it passes the sanity clamp.
    client = FakeClient({"P001-01": [0.05, 0.20, 0.95, 0.92]})
    doc = gv.run_vision_trim(_units("P001-01"), client)
    entry = doc["units"]["P001-01"]
    assert entry["source"] == "gemini"
    assert entry["box"] == [0.05, 0.2, 0.95, 0.92]
    assert entry["model"] == "gemini-2.5-flash" and "reason" not in entry
    assert doc["summary"] == {"total": 1, "gemini": 1, "fallback": 0}
    # Storyboard grouping: one call; the prompt part first, then a text
    # label carrying the unit id immediately before its image part.
    parts = client.calls[0]
    assert "NO white space" in parts[0]["text"]
    labels = [i for i, p in enumerate(parts) if p.get("text") == "Panel P001-01:"]
    assert labels and "inlineData" in parts[labels[0] + 1]


def test_trim_invalid_or_missing_box_falls_back_flagged():
    client = FakeClient({"P001-01": [2.0, 0.0, 1.0, 1.0]})   # P001-02 missing entirely
    doc = gv.run_vision_trim(_units("P001-01", "P001-02"), client)
    for uid in ("P001-01", "P001-02"):
        entry = doc["units"][uid]
        assert entry["source"] == "fallback"
        assert entry["reason"] == "invalid_or_missing_box"
        # The fallback box IS the algorithmic ink rect.
        assert entry["box"] == pytest.approx([0.10, 0.25, 0.90, 0.875], abs=0.03)
    assert doc["summary"] == {"total": 2, "gemini": 0, "fallback": 2}


def test_clamp_rejects_an_ink_cutting_box_and_falls_back():
    # Strictly inside the ink bounds on every side: it would cut ink.
    client = FakeClient({"P001-01": [0.20, 0.30, 0.80, 0.80]})
    doc = gv.run_vision_trim(_units("P001-01"), client)
    entry = doc["units"]["P001-01"]
    assert entry["source"] == "fallback"
    assert entry["reason"] == "vision_box_cuts_detector_ink"
    assert entry["box"] == pytest.approx([0.10, 0.25, 0.90, 0.875], abs=0.03)


def test_client_failure_makes_all_units_fallback_and_never_raises():
    client = FakeClient(error=gv.GeminiVisionError("Gemini request failed after 6 attempts (last: HTTP 429)"))
    doc = gv.run_vision_trim(_units("P001-01", "P001-02", "P001-03"), client)
    assert doc["summary"] == {"total": 3, "gemini": 0, "fallback": 3}
    assert all(e["source"] == "fallback" and e["reason"] == "gemini_failure"
               for e in doc["units"].values())


# --------------------------------------------------------------- artifact


def test_chapter_artifact_written_without_any_key_material(tmp_path, monkeypatch):
    units = _units("P001-01")
    monkeypatch.setattr(gv, "enumerate_placed_units", lambda root, chapter: units)
    transport = FakeTransport([ok_response({"P001-01": [0.05, 0.20, 0.95, 0.92]})])
    client = make_client(tmp_path, transport)
    root = tmp_path / "session"
    doc = gv.vision_trim_chapter(root, "1", client=client)
    path = gv.vision_trim_path(root, "1")
    assert path.parent.name == "ch1" and path.name == "vision_trim.json"
    text = path.read_text(encoding="utf-8")
    assert "dummy-key" not in text
    assert "x-goog-api-key" not in text
    written = json.loads(text)
    assert written == doc
    assert written["chapter"] == "1" and written["model"] == "gemini-2.5-flash"
    assert written["units"]["P001-01"]["source"] == "gemini"
    # load_vision_trim hands planning exactly the units map.
    assert gv.load_vision_trim(root, "1") == written["units"]
    # Safe to re-run: it overwrites its own artifact.
    transport2 = FakeTransport([ok_response({"P001-01": [0.05, 0.20, 0.95, 0.92]})])
    gv.vision_trim_chapter(root, "1", client=make_client(tmp_path, transport2))
    assert json.loads(path.read_text(encoding="utf-8"))["units"]["P001-01"]["source"] == "gemini"


def test_chapter_artifact_all_fallback_when_the_client_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(gv, "enumerate_placed_units",
                        lambda root, chapter: _units("P001-01", "P001-02"))
    client = FakeClient(error=gv.GeminiVisionError("boom (no key material here)"))
    doc = gv.vision_trim_chapter(tmp_path / "s", "2", client=client)   # must not raise
    assert doc["summary"] == {"total": 2, "gemini": 0, "fallback": 2}
    assert gv.load_vision_trim(tmp_path / "s", "2")["P001-01"]["source"] == "fallback"


# --------------------------------------------------------------- plan consumption


def _placed_plan(vision_trim):
    panels = [{"id": "P009-01", "width": 600, "height": 800, "text": ""}]
    sentences = [{"start": 0.0, "text": "one", "panels": ["P009-01"],
                  "cues": [], "emphasis": []}]
    return build_plan(chapter="1", panels_data={"panels": panels, "fingerprint": "gv"},
                      duration=5.0, audio=None, vcfg={**VCFG, "mode": "placed"},
                      image_for=lambda p: margin_img(), sentences=sentences,
                      vision_trim=vision_trim)


def test_plan_consumes_a_vision_box_through_the_trim_spec():
    vision = {"P009-01": {"box": [0.05, 0.20, 0.95, 0.92],
                          "source": "gemini", "model": "gemini-2.5-flash"}}
    plan = _placed_plan(vision)
    spec = plan["shots"][0]["cards"][0]["placed"]
    assert spec["trim"] == [0.05, 0.2, 0.95, 0.92]
    assert spec["trim_source"] == "gemini" and spec["trim_fallback"] is False
    assert plan["placed_vision_trimmed"] == ["P009-01"]
    assert plan["placed_vision_fallback"] == []
    assert plan_mod.validate(plan, {"P009-01"}) == []
    qa = build_mod.quality_report(plan, {"panels": [
        {"id": "P009-01", "width": 600, "height": 800, "text": ""}]}, None, 3.0)
    assert qa["placed_vision_trimmed"] == ["P009-01"]
    assert not any("placed" in f for f in qa["failures"])


def test_plan_fallback_entry_runs_the_algorithmic_trim():
    ink = plan_mod.placed_trim_rect(margin_img())
    vision = {"P009-01": {"box": list(ink), "source": "fallback",
                          "model": "gemini-2.5-flash", "reason": "gemini_failure"}}
    plan = _placed_plan(vision)
    spec = plan["shots"][0]["cards"][0]["placed"]
    assert spec["trim"] == pytest.approx(list(ink), abs=1e-4)
    assert spec["trim_source"] == "algorithmic"
    assert plan["placed_vision_trimmed"] == []
    assert plan["placed_vision_fallback"] == ["P009-01"]
    assert plan["placed_trimmed"] == ["P009-01"]    # the algorithmic trim still shaved it


def test_plan_without_a_vision_artifact_is_the_pre_vision_plan():
    plan = _placed_plan(None)
    spec = plan["shots"][0]["cards"][0]["placed"]
    assert spec["trim_source"] == "algorithmic"
    assert plan["placed_vision_trimmed"] == [] and plan["placed_vision_fallback"] == []


# --------------------------------------------------------------- QC helper


def _frame(fid, ts=1.0):
    return {"id": fid, "timestamp": ts, "image": Image.new("RGB", (64, 64), (10, 20, 30))}


def test_qc_parses_clean_and_issues_verdicts():
    client = FakeClient({
        "F1": {"white_space": False, "floating": False, "bands": False, "note": ""},
        "F2": {"white_space": True, "floating": True, "bands": False,
               "note": "white margin at top"},
    })
    out = gv.qc_frames([_frame("F1"), _frame("F2", 2.0)], client=client)
    assert out[0]["verdict"] == "clean" and out[0]["timestamp"] == 1.0
    assert out[1]["verdict"] == "issues" and out[1]["white_space"] is True
    assert out[1]["note"] == "white margin at top"
    # The prompt carries the user's three checks.
    prompt = client.calls[0][0]["text"]
    assert "white space" in prompt and "floating" in prompt and "letterbox" in prompt


def test_qc_malformed_or_missing_frame_entry_is_unknown_never_clean():
    client = FakeClient({
        "F1": {"white_space": False, "floating": False, "bands": False, "note": ""},
        "F2": "garbage",                       # malformed entry
        # F3 missing entirely
    })
    out = gv.qc_frames([_frame("F1"), _frame("F2"), _frame("F3")], client=client)
    assert [v["verdict"] for v in out] == ["clean", "unknown", "unknown"]
    assert out[1]["white_space"] is None and out[2]["floating"] is None


def test_qc_client_failure_makes_every_frame_unknown():
    client = FakeClient(error=gv.GeminiVisionError("down"))
    out = gv.qc_frames([_frame("F1"), _frame("F2")], client=client)
    assert [v["verdict"] for v in out] == ["unknown", "unknown"]


# --------------------------------------------------------------- lazy loading


def test_trim_resolves_lazy_loaders_on_demand():
    # Units may carry a zero-arg "load" callable instead of a decoded
    # image (enumerate_placed_units does this so a whole chapter's
    # panels are never decoded at once — that OOM-killed a real run).
    loaded = []

    def make_loader(uid):
        def load():
            loaded.append(uid)
            return margin_img()
        return load

    units = [{"id": uid, "load": make_loader(uid)}
             for uid in ("P001-01", "P002-01")]
    client = FakeClient({"P001-01": [0.05, 0.20, 0.95, 0.92],
                         "P002-01": [0.05, 0.20, 0.95, 0.92]})
    doc = gv.run_vision_trim(units, client)
    assert doc["summary"] == {"total": 2, "gemini": 2, "fallback": 0}
    assert loaded == ["P001-01", "P002-01"]


def test_trim_one_bad_load_falls_back_alone():
    def bad_load():
        raise OSError("corrupt panel file")

    units = [{"id": "P001-01", "load": bad_load},
             {"id": "P002-01", "image": margin_img()}]
    client = FakeClient({"P002-01": [0.05, 0.20, 0.95, 0.92]})
    doc = gv.run_vision_trim(units, client)
    assert doc["units"]["P001-01"]["source"] == "fallback"
    assert doc["units"]["P001-01"]["reason"] == "image_load_failed"
    assert doc["units"]["P002-01"]["source"] == "gemini"
