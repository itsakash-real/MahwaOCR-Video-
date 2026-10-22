"""Phase 0.2: the read-only defect pre-checker (manhwa_ocr/video/precheck.py)."""

import json

import pytest

from manhwa_ocr import cli
from manhwa_ocr.util import UserError
from manhwa_ocr.video import detect, precheck


def _box(x0, y0, x1, y1, label="text_free"):
    return {"label": label, "score": 0.9, "x0": x0, "y0": y0, "x1": x1, "y1": y1}


# --------------------------------------------------------------- geometry

def test_relation_inside_outside_bisected():
    win = [0.0, 0.2, 1.0, 0.8]
    assert precheck.window_relation(win, _box(0.1, 0.3, 0.9, 0.7)) == ("inside", [])
    assert precheck.window_relation(win, _box(0.1, 0.85, 0.9, 0.95)) == ("outside", [])
    rel, cut = precheck.window_relation(win, _box(0.1, 0.3, 0.9, 0.9))
    assert rel == "bisected" and cut == ["bottom"]
    rel, cut = precheck.window_relation(win, _box(0.1, 0.1, 0.9, 0.7))
    assert rel == "bisected" and cut == ["top"]
    rel, cut = precheck.window_relation(win, _box(0.1, 0.1, 0.9, 0.9))
    assert rel == "bisected" and cut == ["top", "bottom"]


def test_relation_whole_panel_contains_everything():
    assert precheck.window_relation([0, 0, 1, 1], _box(0.0, 0.0, 1.0, 1.0)) == ("inside", [])


def test_relation_eps_flush_is_not_a_cut():
    win = [0.0, 0.2, 1.0, 0.8]
    # the box pokes 0.002 past the window's bottom: within EPS -> inside
    assert precheck.window_relation(win, _box(0.1, 0.3, 0.9, 0.802)) == ("inside", [])
    # ...but 0.01 past is a real bisection
    rel, cut = precheck.window_relation(win, _box(0.1, 0.3, 0.9, 0.81))
    assert rel == "bisected" and cut == ["bottom"]


# --------------------------------------------------------------- chapter

def _session(tmp_path):
    """A bare session layout with panels.json, one plan, and box sidecars."""
    root = tmp_path / "s"
    (root / "media" / "ch1").mkdir(parents=True)
    (root / "video").mkdir(parents=True)
    (root / "media" / "ch1" / "panels.json").write_text(json.dumps({
        "version": 4, "chapter": "1", "norm_width": 900,
        "panels": [{"id": "P001-01"}, {"id": "P001-02"}, {"id": "P001-03"}],
    }), encoding="utf-8")
    plan = {"shots": [
        {"id": "S001", "cards": [{"panel": "P001-01", "crop": [0.0, 0.0, 1.0, 0.6]}]},
        {"id": "S002", "cards": [{"panel": "P001-02", "crop": [0.0, 0.0, 1.0, 1.0]},
                                 {"panel": "P001-03", "crop": [0.0, 0.5, 1.0, 1.0]}]},
    ]}
    (root / "video" / "chapter_1.plan.json").write_text(json.dumps(plan), encoding="utf-8")
    boxes = {
        # S001's window ends at 0.6; this glyph runs 0.5 -> 0.75: bisected at bottom
        "P001-01": [_box(0.2, 0.5, 0.8, 0.75), _box(0.2, 0.1, 0.8, 0.3, "bubble")],
        # whole-panel window: nothing can be bisected
        "P001-02": [_box(0.2, 0.4, 0.8, 0.6)],
        # P001-03 deliberately gets NO sidecar -> reported as unchecked
    }
    for pid, bl in boxes.items():
        d = detect.boxes_dir(root, "1")
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{pid}.json").write_text(json.dumps(
            {"panel": pid, "key": "x", "boxes": bl}), encoding="utf-8")
    return root


def test_precheck_chapter(tmp_path):
    root = _session(tmp_path)
    rep = precheck.precheck_chapter(root, "1")
    assert rep["cards"] == 3
    assert rep["panels_checked"] == 2
    assert rep["panels_without_boxes"] == ["P001-03"]
    assert len(rep["findings"]) == 1
    f = rep["findings"][0]
    assert f["shot"] == "S001" and f["panel"] == "P001-01"
    assert f["label"] == "text_free" and f["cut"] == ["bottom"]
    assert (root / ".work" / "video" / "ch1" / "precheck.json").is_file()
    assert "text-bisection only" in rep["scope"]


def test_precheck_needs_a_plan(tmp_path):
    root = _session(tmp_path)
    (root / "video" / "chapter_1.plan.json").unlink()
    with pytest.raises(UserError, match="video plan"):
        precheck.precheck_chapter(root, "1")


def test_precheck_refuses_when_no_panel_has_boxes(tmp_path):
    root = _session(tmp_path)
    import shutil
    shutil.rmtree(detect.boxes_dir(root, "1"))
    with pytest.raises(UserError, match="video detect"):
        precheck.precheck_chapter(root, "1")


def test_cli_precheck(tmp_path):
    from manhwa_ocr.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions")
    sess = store.create("Demo")
    src = _session(tmp_path)
    # move the bare layout into the real session folder
    import shutil
    for name in ("media", "video", ".work"):
        shutil.copytree(src / name, sess.path / name)
    code = cli.main(["video", "precheck", "--chapter", "1",
                     "--session", "demo", "--sessions-root", str(tmp_path / "sessions")])
    assert code == 0
    code = cli.main(["video", "precheck", "--chapter", "9",
                     "--session", "demo", "--sessions-root", str(tmp_path / "sessions")])
    assert code == 2   # no plan for chapter 9 -> clean error
