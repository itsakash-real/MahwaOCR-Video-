"""Verify (no AI), fixes.json checks, reviewer reply handling, and the review loop
with a fake provider. No audio, no FFmpeg, no network."""

import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.util import UserError
from manhwa_ocr.video import fixes, review, verify
from manhwa_ocr.video.frames import format_from, shapes_for
from manhwa_ocr.video.media import video_dir

DIMS = {"P001-01": (900.0, 1600.0), "P001-02": (900.0, 1600.0)}


def _art(w=900, h=1600):
    rng = np.random.default_rng(3)
    blocks = rng.normal(0, 22, (h // 8 + 1, w // 8 + 1, 1)).repeat(8, axis=0).repeat(8, axis=1)[:h, :w]
    arr = np.clip(np.array((80, 100, 130), dtype=np.float32) + blocks, 0, 255).astype("uint8")
    return Image.fromarray(arr, "RGB")


def _lettering_only(w=900, h=1600):
    img = Image.new("RGB", (w, h), (255, 255, 255))      # white page with text lines, no character
    d = ImageDraw.Draw(img)
    for y in range(200, 1400, 60):
        d.rectangle((150, y, 750, y + 18), fill=(10, 10, 10))
    return img


# ---- fixes.json ------------------------------------------------------------

def test_valid_fixes_are_accepted_and_plain_lookups_built():
    data = {"version": 1,
            "drop": [{"panel": "P001-02", "reason": "sound effect only"}],
            "face": [{"panel": "P001-01", "top": 0.1, "bottom": 0.4}],
            "crop": [{"panel": "P001-01", "shape": "3:4", "box": [0.0, 0.1, 1.0, 0.1 + 900 / 0.75 / 1600]}]}
    clean, problems = fixes.validate(data, DIMS)
    assert problems == []
    assert clean["drop"] == {"P001-02": "sound effect only"}
    assert clean["face"]["P001-01"] == (0.1, 0.4)


@pytest.mark.parametrize("bad, text", [
    ({"drop": [{"panel": "P999-01"}]}, "unknown panel"),
    ({"face": [{"panel": "P001-01", "top": 0.5, "bottom": 0.2}]}, "0 <= top < bottom <= 1"),
    ({"crop": [{"panel": "P001-01", "shape": "2:1", "box": [0, 0, 1, 1]}]}, "shape must be one of"),
    ({"crop": [{"panel": "P001-01", "shape": "1:1", "box": [0, 0, 1, 1]}]}, "box is"),
    ({"crop": [{"panel": "P001-01", "shape": "3:4", "box": [0, 0, 1.5, 1]}]}, "inside 0..1"),
])
def test_bad_fixes_are_rejected_with_a_message(bad, text):
    _, problems = fixes.validate(bad, DIMS)
    assert problems and any(text in p for p in problems)


def test_load_raises_and_applies_nothing_for_a_bad_file(tmp_path):
    video_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    fixes.fixes_path(tmp_path, "1").write_text(json.dumps({"drop": [{"panel": "NOPE"}]}), encoding="utf-8")
    with pytest.raises(UserError, match="nothing from it was applied"):
        fixes.load(tmp_path, "1", DIMS)


# ---- reviewer reply and merge ----------------------------------------------

def test_reply_json_is_found_even_with_prose_around_it():
    text = 'Here is my review.\n```json\n{"approved": false, "drop": [], "notes": "S010 wrong panel"}\n```'
    assert review.parse_reply(text)["approved"] is False


def test_reply_without_json_is_an_error_not_an_approval():
    with pytest.raises(UserError, match="no JSON"):
        review.parse_reply("looks fine to me")
    with pytest.raises(UserError, match="approved"):
        review.parse_reply('{"notes": "x"}')


def test_merge_later_entry_for_the_same_panel_replaces_the_earlier_one():
    cur = {"drop": [{"panel": "P001-01", "reason": "old"}], "face": [], "crop": []}
    reply = {"drop": [{"panel": "P001-01", "reason": "new"}],
             "face": [{"panel": "P001-02", "top": 0.1, "bottom": 0.3}]}
    out = review.merge(cur, reply)
    assert out["drop"] == [{"panel": "P001-01", "reason": "new"}]
    assert out["face"][0]["panel"] == "P001-02"


# ---- verify (no AI) --------------------------------------------------------

def test_lettering_only_page_is_not_clean_and_a_textured_panel_is():
    shapes = shapes_for("single", format_from("16:9", 30))
    _, _, probs, clean = verify.pick_crop(_lettering_only(), shapes)
    assert not clean and probs
    assert verify.pick_crop(_art(), shapes)[3] is True


def test_verify_report_drops_the_text_page_and_keeps_the_art(tmp_path):
    panels = [{"id": "P001-01", "width": 900, "height": 1600, "text_only": False},
              {"id": "P001-02", "width": 900, "height": 1600, "text_only": False}]
    images = {"P001-01": _lettering_only(), "P001-02": _art()}
    rep = verify.verify_chapter(panels, lambda p: images[p["id"]], [0.75, 1.0], tmp_path, "1",
                                log=lambda *_: None)
    assert [d["panel"] for d in rep["dropped"]] == ["P001-01"]
    assert rep["panels"]["P001-02"]["action"] == "ok"
    assert (video_dir(tmp_path) / "chapter_1.verify.json").exists()


# ---- review loop with a fake provider ---------------------------------------

class FakeProvider:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0
        self.vision_model = "fake"

    def vision(self, system, prompt, images):
        self.calls += 1
        return self.replies.pop(0)


def _sess(tmp_path):
    video_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(path=tmp_path, slug="t")


def _patch(monkeypatch, tmp_path, plan, panels):
    from manhwa_ocr.video import build as vb

    board = tmp_path / "board.png"
    Image.new("RGB", (40, 40), (1, 2, 3)).save(board)
    monkeypatch.setattr(review, "storyboard_paths", lambda root, chapter: [board])
    monkeypatch.setattr(review, "load_panels", lambda root, chapter: {"panels": panels})
    monkeypatch.setattr(vb, "make_plan", lambda *a, **k: plan)


def test_review_loop_applies_fixes_then_approves(tmp_path, monkeypatch):
    plan = {"generated_hash": "h1", "shots": [{"id": "S001", "start": 0, "end": 3, "sentence": "x",
                                               "cards": [{"panel": "P001-01"}]}]}
    _patch(monkeypatch, tmp_path, plan, [{"id": "P001-01", "width": 900, "height": 1600},
                                         {"id": "P001-02", "width": 900, "height": 1600}])
    provider = FakeProvider([
        json.dumps({"approved": False, "drop": [{"panel": "P001-02", "reason": "logo only"}], "notes": "S001 logo"}),
        json.dumps({"approved": True, "drop": [], "notes": ""}),
    ])
    cfg = SimpleNamespace(provider={}, video={"review": {"max_rounds": 3}})
    result = review.run_review(_sess(tmp_path), "1", cfg, provider=provider, log=lambda *_: None)
    assert result == {"approved": True, "rounds": 2}
    saved = json.loads(fixes.fixes_path(tmp_path, "1").read_text(encoding="utf-8"))
    assert saved["drop"] == [{"panel": "P001-02", "reason": "logo only"}]
    assert review.is_approved(tmp_path, "1", plan)
    assert not review.is_approved(tmp_path, "1", {"generated_hash": "other"})


def test_review_never_approves_while_the_reviewer_objects(tmp_path, monkeypatch):
    plan = {"generated_hash": "h2", "shots": []}
    _patch(monkeypatch, tmp_path, plan, [{"id": "P001-01", "width": 900, "height": 1600}])
    provider = FakeProvider([json.dumps({"approved": False, "notes": "still wrong"})] * 2)
    cfg = SimpleNamespace(provider={}, video={"review": {"max_rounds": 2}})
    result = review.run_review(_sess(tmp_path), "1", cfg, provider=provider, log=lambda *_: None)
    assert result["approved"] is False and provider.calls == 2
    assert not review.is_approved(tmp_path, "1", plan)
