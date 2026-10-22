"""Detector step: ogkalu RT-DETR boxes + the per-panel cache
(manhwa_ocr/video/detect.py). The ONNX session is faked; the real model is
verified separately (it needs the optional onnxruntime install)."""

import sys

import numpy as np
import pytest
from PIL import Image, ImageDraw

from manhwa_ocr import cli
from manhwa_ocr.util import UserError
from manhwa_ocr.video import detect


class FakeSession:
    """Returns canned detections in original-pixel xyxy, like the export."""

    def __init__(self, labels, boxes, scores):
        self.labels = np.array([labels], dtype=np.int64)
        self.boxes = np.array([boxes], dtype=np.float32)
        self.scores = np.array([scores], dtype=np.float32)
        self.calls = 0

    def run(self, _outputs, inputs):
        self.calls += 1
        assert inputs["images"].shape == (1, 3, 640, 640)
        assert inputs["orig_target_sizes"].tolist() == [[200, 100]]  # [width, height]
        return self.labels, self.boxes, self.scores


def _img():
    return Image.new("RGB", (200, 100), (255, 255, 255))


def test_detect_boxes_postprocessing():
    fake = FakeSession(
        labels=[0, 1, 2, 1, 0],
        boxes=[[10, 10, 110, 60],      # bubble, in range
               [20, 20, 100, 50],      # text_bubble, in range
               [-30, 0, 260, 100],     # text_free, out of bounds -> clipped
               [0, 0, 50, 50],         # below threshold
               [0, 0, 10, 10]],        # duplicate-ish, mid score
        scores=[0.90, 0.80, 0.70, 0.20, 0.50])
    out = detect.detect_boxes(_img(), session=fake)
    assert [b["label"] for b in out] == ["bubble", "text_bubble", "text_free", "bubble"]
    assert out[0] == {"label": "bubble", "score": 0.90,
                      "x0": 0.05, "y0": 0.10, "x1": 0.55, "y1": 0.60}
    assert out[2]["x0"] == 0.0 and out[2]["x1"] == 1.0   # clipped to the panel
    assert all(0.0 <= b[k] <= 1.0 for b in out for k in ("x0", "y0", "x1", "y1"))


def test_detect_boxes_threshold_arg():
    fake = FakeSession([0], [[0, 0, 100, 100]], [0.40])
    assert detect.detect_boxes(_img(), session=fake, threshold=0.35)
    assert detect.detect_boxes(_img(), session=fake, threshold=0.50) == []


# --------------------------------------------------------------- errors

def test_missing_model_is_a_clean_error(tmp_path):
    with pytest.raises(UserError, match="not found"):
        detect.get_session(tmp_path / "nope.onnx")


def test_missing_onnxruntime_is_a_clean_error(tmp_path, monkeypatch):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"fake")
    monkeypatch.setitem(sys.modules, "onnxruntime", None)  # import now fails
    detect._SESSIONS.clear()
    with pytest.raises(UserError, match="onnxruntime"):
        detect.get_session(model)


def test_panel_key_stability():
    a, b = _img(), _img()
    assert detect.panel_key(a) == detect.panel_key(b)
    b.putpixel((0, 0), (1, 2, 3))
    assert detect.panel_key(a) != detect.panel_key(b)


# --------------------------------------------------------------- cache

def _session_with_panels(tmp_path):
    from manhwa_ocr.video.media import pages_dir
    from manhwa_ocr.video.panels import build_panels

    root = tmp_path / "s"
    folder = pages_dir(root, "1")
    folder.mkdir(parents=True)
    img = Image.new("RGB", (300, 1000), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for y0, y1, colour in [(40, 400, (200, 60, 60)), (460, 820, (60, 160, 60))]:
        d.rectangle((10, y0, 290, y1 - 1), fill=colour)
        for x in range(10, 290, 7):
            d.line((x, y0, x, y1 - 1), fill=(colour[0] // 2, colour[1] // 2, colour[2] // 2))
    img.save(folder / "page_001.png")
    data = build_panels(root, "1", log=lambda *_: None)
    return root, data


def test_boxes_cache_lifecycle(tmp_path, monkeypatch):
    root, data = _session_with_panels(tmp_path)
    calls = {"n": 0}

    def stub(img, **kw):
        calls["n"] += 1
        return [{"label": "bubble", "score": 0.9, "x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.3}]

    monkeypatch.setattr(detect, "detect_boxes", stub)
    panel = data["panels"][0]

    rec = detect.boxes_for_panel(root, "1", panel, data["norm_width"])
    assert calls["n"] == 1
    assert rec["model"]["id"] == detect.MODEL_ID
    assert rec["model"]["revision"] == detect.MODEL_REVISION
    assert rec["boxes"][0]["label"] == "bubble"
    assert detect.load_boxes(root, "1", panel["id"])["key"] == rec["key"]

    rec2 = detect.boxes_for_panel(root, "1", panel, data["norm_width"])
    assert calls["n"] == 1 and rec2 == rec          # cache hit: no recompute

    detect.boxes_for_panel(root, "1", panel, data["norm_width"], force=True)
    assert calls["n"] == 2                          # force recomputes

    detect.boxes_for_panel(root, "1", panel, data["norm_width"], threshold=0.5)
    assert calls["n"] == 3                          # a new threshold is a new cache entry


def test_detect_chapter_counts(tmp_path, monkeypatch):
    root, data = _session_with_panels(tmp_path)
    monkeypatch.setattr(detect, "get_session", lambda p: object())
    monkeypatch.setattr(detect, "detect_boxes",
                        lambda img, **kw: [{"label": "bubble", "score": 0.9,
                                            "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 0.5}])
    rep = detect.detect_chapter(root, "1")
    assert rep == {"chapter": "1", "panels": 2, "computed": 2, "cached": 0, "boxes": 2}
    rep = detect.detect_chapter(root, "1")
    assert rep["computed"] == 0 and rep["cached"] == 2


def test_cli_detect_without_model_errors_cleanly(tmp_path, monkeypatch):
    from manhwa_ocr.sessions import SessionStore
    from manhwa_ocr.video.media import pages_dir
    from manhwa_ocr.video.panels import build_panels

    store = SessionStore(tmp_path / "sessions")
    sess = store.create("Demo")
    folder = pages_dir(sess.path, "1")
    folder.mkdir(parents=True)
    img = Image.new("RGB", (300, 1000), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((10, 40, 290, 399), fill=(200, 60, 60))
    img.save(folder / "page_001.png")
    build_panels(sess.path, "1", log=lambda *_: None)

    monkeypatch.setenv("MANHWA_DETECTOR_MODEL", str(tmp_path / "absent.onnx"))
    code = cli.main(["video", "detect", "--chapter", "1",
                     "--session", "demo", "--sessions-root", str(tmp_path / "sessions")])
    assert code == 2
