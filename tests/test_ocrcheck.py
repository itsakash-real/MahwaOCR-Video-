"""Build 3: OCR cross-check + draft (manhwa_ocr/video/ocrcheck.py)."""

import json

import pytest
from PIL import Image

from manhwa_ocr.util import UserError
from manhwa_ocr.video import detect, ocrcheck


class FakeEngine:
    """Mimics RapidOCR: callable(array) -> (result, elapsed)."""

    def __init__(self, lines):
        self.lines = lines  # [(text, score, (x0, y0, x1, y1) in pixels)]

    def __call__(self, _arr):
        result = []
        for text, score, (x0, y0, x1, y1) in self.lines:
            quad = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
            result.append([quad, text, score])
        return result, 0.0


def test_ocr_image_fractions_and_filtering():
    img = Image.new("RGB", (1000, 2000))
    eng = FakeEngine([
        ("HELLO WORLD", 0.95, (100, 200, 500, 260)),
        ("faint noise", 0.30, (0, 0, 50, 50)),       # below MIN_SCORE
        ("SECOND LINE", 0.90, (100, 900, 500, 960)),
    ])
    lines = ocrcheck.ocr_image(img, engine=eng)
    assert [ln["text"] for ln in lines] == ["HELLO WORLD", "SECOND LINE"]
    assert lines[0]["box"] == [0.1, 0.1, 0.5, 0.13]


def test_normalize_and_found():
    assert ocrcheck.normalize("  I WON'T go!! ") == "i won t go"
    blob = " " + ocrcheck.normalize("I won't go, not ever.") + " "
    assert ocrcheck._is_found("i won t go", blob, ["i won t go not ever"])
    assert not ocrcheck._is_found("a completely different sentence here", blob,
                                  ["i won t go not ever"])
    # short fragments are never candidates
    assert ocrcheck._is_found("boom", blob, [])


def test_transcript_lines_extraction(tmp_path):
    (tmp_path / "chapter_3.md").write_text(
        "# Title\n\n*(A scene description.)*\n\n> FIRST LINE.\n> SECOND LINE!\n\n"
        "*(Narration in italics is not quoted text.)*\n", encoding="utf-8")
    assert ocrcheck.transcript_lines(tmp_path, "3") == ["FIRST LINE.", "SECOND LINE!"]
    with pytest.raises(UserError, match="transcript"):
        ocrcheck.transcript_lines(tmp_path, "9")


def _session_with_transcript(tmp_path):
    root = tmp_path / "s"
    (root / "media" / "ch1").mkdir(parents=True)
    panels = {"version": 4, "chapter": "1", "norm_width": 900,
              "panels": [{"id": "P001-01"}, {"id": "P001-02"}]}
    (root / "media" / "ch1" / "panels.json").write_text(json.dumps(panels), encoding="utf-8")
    (root / "chapter_1.md").write_text(
        "# T\n\n> THE QUICK BROWN FOX JUMPS OVER.\n", encoding="utf-8")
    return root


def test_cross_check_candidates(tmp_path, monkeypatch):
    root = _session_with_transcript(tmp_path)
    canned = {
        "P001-01": [{"text": "THE QUICK BROWN FOX", "score": 0.9, "box": [0, 0, 1, 0.1]},
                    {"text": "A LINE THE TRANSCRIPT MISSED", "score": 0.9, "box": [0, 0.2, 1, 0.3]}],
        "P001-02": [{"text": "JUMPS OVER", "score": 0.9, "box": [0, 0, 1, 0.1]}],
    }
    monkeypatch.setattr(ocrcheck, "ocr_panel",
                        lambda root_, ch, panel, nw, engine=None: canned[panel["id"]])
    rep = ocrcheck.cross_check(root, "1")
    assert rep["ocr_lines"] == 3 and rep["transcript_lines"] == 1
    assert len(rep["candidates"]) == 1
    assert rep["candidates"][0]["panel"] == "P001-01"
    assert "MISSED" in rep["candidates"][0]["text"]
    assert (root / ".work" / "video" / "ch1" / "ocr_check.json").is_file()


def test_draft_grouping_by_bubble_box(tmp_path, monkeypatch):
    root = _session_with_transcript(tmp_path)
    canned = {"P001-01": [
        {"text": "IN BUBBLE", "score": 0.9, "box": [0.30, 0.30, 0.60, 0.36]},
        {"text": "FREE SFX", "score": 0.9, "box": [0.05, 0.80, 0.30, 0.86]}],
        "P001-02": []}
    monkeypatch.setattr(ocrcheck, "ocr_panel",
                        lambda root_, ch, panel, nw, engine=None: canned[panel["id"]])
    d = detect.boxes_dir(root, "1")
    d.mkdir(parents=True, exist_ok=True)
    (d / "P001-01.json").write_text(json.dumps({"panel": "P001-01", "key": "x", "boxes": [
        {"label": "text_bubble", "score": 0.9, "x0": 0.25, "y0": 0.25, "x1": 0.65, "y1": 0.42},
    ]}), encoding="utf-8")
    rep = ocrcheck.draft_chapter(root, "1")
    assert rep["panels"]["P001-01"]["dialogue"] == ["IN BUBBLE"]
    assert rep["panels"]["P001-01"]["other"] == ["FREE SFX"]


def test_missing_engine_is_a_clean_error(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "rapidocr_onnxruntime", None)
    ocrcheck._engine = None
    import builtins
    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name == "rapidocr_onnxruntime":
            raise ImportError("no module")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(UserError, match="RapidOCR is not installed"):
        ocrcheck.get_engine()
    ocrcheck._engine = None
