"""Build 4: the safe-crop solver (manhwa_ocr/video/solve.py)."""

import json

from manhwa_ocr.video import detect, solve


def _box(y0, y1, label="text_free", x0=0.3, x1=0.7):
    return {"label": label, "score": 0.9, "x0": x0, "y0": y0, "x1": x1, "y1": y1}


def test_already_safe():
    res = solve.solve_card([_box(0.2, 0.4)], [0.0, 0.0, 1.0, 0.6], "static")
    assert res["status"] == "already_safe" and res["moved"] == 0.0


def test_bottom_bisection_prefers_nearest_seam():
    # glyph 0.60-0.80 vs window 0.10-0.70: exclude (bottom at 0.594, move 0.10)
    # beats include (bottom seam at 0.806, move 0.106 + caption-band cost)
    res = solve.solve_card([_box(0.60, 0.80)], [0.0, 0.10, 1.0, 0.70], "static")
    assert res["status"] == "solved" and res["kind"] == "solved_exclude"
    assert abs(res["window"][3] - 0.594) < 0.01


def test_top_bisection_tie_prefers_include():
    res = solve.solve_card([_box(0.05, 0.25)], [0.0, 0.15, 1.0, 0.75], "static")
    assert res["status"] == "solved" and res["kind"] == "solved_include"
    assert res["window"][1] <= 0.05


def test_no_seam_falls_back_to_whole_panel():
    boxes = [_box(0.05, 0.55), _box(0.50, 0.98)]
    res = solve.solve_card(boxes, [0.0, 0.10, 1.0, 0.70], "static")
    assert res["status"] == "unsolved" and res["kind"] == "fallback_whole_panel"
    assert res["window"] == [0.0, 0.0, 1.0, 1.0]


def test_push_in_end_frame_counts():
    # box sits inside the start window (0.10-0.70) but the push_in end frame
    # (inset to 0.139-0.661) slices its bottom (0.68): the solver must move
    # far enough that the END frame also holds it whole.
    box = _box(0.60, 0.68)
    res = solve.solve_card([box], [0.0, 0.10, 1.0, 0.70], "push_in")
    assert res["status"] == "solved"
    # the real property: in the start frame AND the push_in end frame the
    # box is wholly in or wholly out, never bisected
    from manhwa_ocr.video.precheck import window_relation
    w = res["window"]
    h = w[3] - w[1]
    for frame in (w, [w[0], w[1] + 0.065 * h, w[2], w[3] - 0.065 * h]):
        assert window_relation(frame, box)[0] != "bisected"


def test_pull_out_end_frame_counts():
    # The real motion name is "pull_out" (see video/frames.py MOTIONS), not
    # "pull_back". A box that sits just outside the start window must still
    # be caught if the pull-out extreme frame (which reveals more of the
    # panel) would bisect it -- a regression for a name mismatch that made
    # solve_card skip the extreme-frame check entirely for pull_out cards.
    box = _box(0.26, 0.30, x0=0.3, x1=0.7)
    window = [0.0, 0.3, 1.0, 0.7]
    res = solve.solve_card([box], window, "pull_out")
    assert res["status"] != "already_safe"
    from manhwa_ocr.video.precheck import window_relation
    h = window[3] - window[1]
    w = res["window"] if res["status"] == "solved" else [0.0, 0.0, 1.0, 1.0]
    frame = [w[0], w[1] - 0.065 * h, w[2], w[3] + 0.065 * h]
    assert window_relation(frame, box)[0] != "bisected"


def test_far_seam_is_not_a_solve():
    # the only seam is > MAX_MOVE away -> unsolved, not a wild reframe
    boxes = [_box(0.02, 0.30), _box(0.28, 0.60), _box(0.58, 0.99)]
    res = solve.solve_card(boxes, [0.0, 0.55, 1.0, 1.0], "static")
    assert res["status"] in ("solved", "unsolved")
    if res["status"] == "solved":
        assert res["moved"] <= solve.MAX_MOVE


def _session(tmp_path):
    root = tmp_path / "s"
    (root / "media" / "ch1").mkdir(parents=True)
    (root / "video").mkdir(parents=True)
    (root / "media" / "ch1" / "panels.json").write_text(json.dumps({
        "version": 4, "chapter": "1", "norm_width": 900,
        "panels": [{"id": "P001-01"}]}), encoding="utf-8")
    plan = {"shots": [{"id": "S001", "cards": [
        {"panel": "P001-01", "crop": [0.0, 0.10, 1.0, 0.70], "motion": "static"}]}]}
    (root / "video" / "chapter_1.plan.json").write_text(json.dumps(plan), encoding="utf-8")
    d = detect.boxes_dir(root, "1")
    d.mkdir(parents=True, exist_ok=True)
    (d / "P001-01.json").write_text(json.dumps(
        {"panel": "P001-01", "key": "x", "boxes": [_box(0.60, 0.80)]}), encoding="utf-8")
    return root


def test_solve_chapter_report(tmp_path):
    root = _session(tmp_path)
    rep = solve.solve_chapter(root, "1")
    assert rep["cards"] == 1 and len(rep["proposals"]) == 1
    pr = rep["proposals"][0]
    assert pr["panel"] == "P001-01" and pr["status"] == "solved"
    assert (root / ".work" / "video" / "ch1" / "solve.json").is_file()
