"""PR4: remaining audit fixes — pair --autofix, thumb LRU, _load_page 8,
shared download cache, segment-cache key, plan --no-storyboard,
pre-tokenized lexical overlap, syncaudit linear gate sweep, full scene text."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.images import download_images  # noqa: E402
from manhwa_ocr.script import _panel_listing  # noqa: E402
from manhwa_ocr.video import pairing  # noqa: E402
from manhwa_ocr.video import syncaudit  # noqa: E402
from manhwa_ocr.video.align_engine import _lexical_overlap, _lexical_overlap_sets  # noqa: E402
from manhwa_ocr.video.frames import format_from  # noqa: E402
from manhwa_ocr.video.panels import _load_page  # noqa: E402
from manhwa_ocr.video.render import _jobs  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    __import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg")


# --------------------------------------------------------------- pair --autofix


def _pair_session(tmp_path: Path):
    """A session dir with cues (one text-only tag, one credits tag) + panels."""
    from manhwa_ocr.video import cues as cues_mod
    from manhwa_ocr.video.media import media_dir

    root = tmp_path / "sess"
    (root / "recaps").mkdir(parents=True)
    (media_dir(root, "1")).mkdir(parents=True)
    cues = {
        "sentences": [
            {"text": "He draws the sword.", "panels": ["P001-01"]},
            {"text": "A caption appears.", "panels": ["P001-02"]},
            {"text": "Credits roll.", "panels": ["P001-03"]},
            {"text": "He shouts.", "panels": ["P001-04"]},
        ],
        "tagged_ratio": 1.0,
    }
    (root / "recaps" / "chapter_1.cues.json").write_text(json.dumps(cues))
    panels = {
        "panels": [
            {"id": "P001-01", "page": 1, "segments": [{"file": "page_001.png", "y0": 0, "y1": 100}]},
            {"id": "P001-02", "page": 1, "text_only": True,
             "segments": [{"file": "page_001.png", "y0": 100, "y1": 200}]},
            {"id": "P001-03", "page": 1, "credits": True,
             "segments": [{"file": "page_001.png", "y0": 200, "y1": 300}]},
            {"id": "P001-04", "page": 1, "segments": [{"file": "page_001.png", "y0": 300, "y1": 400}]},
        ],
        "norm_width": 900,
    }
    (media_dir(root, "1") / "panels.json").write_text(json.dumps(panels))
    return root


def test_autofix_retags_text_only_and_credits_to_nearest_drawn(tmp_path):
    root = _pair_session(tmp_path)
    changed = pairing.autofix(root, "1", log=lambda *_: None)
    assert changed == 2
    cues = json.loads((root / "recaps" / "chapter_1.cues.json").read_text())
    tags = [s["panels"] for s in cues["sentences"]]
    # P001-02 (text-only) sits between P001-01 and P001-03(credits): nearest
    # drawn is P001-01... wait, P001-03 is credits so not drawn; nearest drawn
    # to P001-02 is P001-01 (distance 1) vs P001-04 (distance 2) -> P001-01.
    assert tags[1] == ["P001-01"]
    # P001-03 (credits) -> nearest drawn is P001-04 (distance 1) vs P001-01 (2).
    assert tags[2] == ["P001-04"]
    # Untouched sentences stay untouched.
    assert tags[0] == ["P001-01"] and tags[3] == ["P001-04"]


def test_autofix_is_a_noop_when_nothing_to_fix(tmp_path):
    root = _pair_session(tmp_path)
    cues = json.loads((root / "recaps" / "chapter_1.cues.json").read_text())
    for s in cues["sentences"]:
        s["panels"] = ["P001-01"]
    (root / "recaps" / "chapter_1.cues.json").write_text(json.dumps(cues))
    assert pairing.autofix(root, "1", log=lambda *_: None) == 0


def test_load_page_cache_is_eight():
    assert _load_page.cache_info().maxsize == 8


# --------------------------------------------------------------- download cache


def test_download_cache_serves_second_chapter_without_network(tmp_path, monkeypatch):
    png = io.BytesIO()
    Image.new("RGB", (60, 40), "green").save(png, format="PNG")
    png_bytes = png.getvalue()
    calls = []

    class _Resp:
        def __init__(self, content):
            self.content = content
            self.headers = {"Content-Type": "image/png"}

        def raise_for_status(self):
            pass

    def _get(url, headers=None, timeout=None):
        calls.append(url)
        return _Resp(png_bytes)

    import manhwa_ocr.images as images_mod

    monkeypatch.setattr(images_mod.requests, "get", _get)
    cache = tmp_path / ".cache" / "pages"
    url = "http://x/page1.png"
    out1 = download_images([url], tmp_path / "ch1", config={"delay": 0},
                           log=lambda *_: None, cache_dir=cache)
    assert len(calls) == 1
    assert (cache / f"{hashlib.sha256(url.encode()).hexdigest()}.png").exists()
    # Second chapter, same URL: no network, same bytes.
    out2 = download_images([url], tmp_path / "ch2", config={"delay": 0},
                           log=lambda *_: None, cache_dir=cache)
    assert calls == [url]  # still one call
    assert out1[0].read_bytes() == out2[0].read_bytes() == png_bytes


# --------------------------------------------------------------- segment-cache key


def _plan_and_panels():
    plan = {
        "duration": 6.0,
        "shots": [{
            "id": "S001", "start": 0.0, "end": 6.0, "layout": "single",
            "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1]}],
        }],
    }
    panels = {
        "panels": [{
            "id": "P001-01", "page": 1,
            "segments": [{"file": "page_001.png", "y0": 0, "y1": 400}],
            "x0": 0, "x1": 900, "y0": 0, "y1": 400,
        }],
        "norm_width": 900,
        "fingerprint": "fp-aaa",
    }
    return plan, panels


def _jobs_for(tmp_path: Path, fingerprint: str, x0: int = 0):
    plan, panels = _plan_and_panels()
    panels["fingerprint"] = fingerprint
    panels["panels"][0]["x0"] = x0
    fmt = format_from("16:9", 30, False)
    return _jobs(tmp_path, "1", plan, panels, fmt, {}, False)


def _make_page(tmp_path: Path):
    from manhwa_ocr.video.media import pages_dir

    pages = pages_dir(tmp_path, "1")
    pages.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (900, 400), "white").save(pages / "page_001.png")
    return pages / "page_001.png"


def test_segment_key_ignores_chapter_fingerprint(tmp_path):
    _make_page(tmp_path)
    a = _jobs_for(tmp_path, "fp-aaa")
    b = _jobs_for(tmp_path, "fp-bbb")  # same panels/shot, different chapter fingerprint
    assert a[0]["out"] == b[0]["out"]


def test_segment_key_moves_with_panel_geometry_and_page_file(tmp_path):
    p = _make_page(tmp_path)
    a = _jobs_for(tmp_path, "fp-aaa")
    b = _jobs_for(tmp_path, "fp-aaa", x0=50)  # a panel moved
    assert a[0]["out"] != b[0]["out"]
    # Touch the page file (re-downloaded): the stamp changes, so does the key.
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 60))
    c = _jobs_for(tmp_path, "fp-aaa")
    assert a[0]["out"] != c[0]["out"]


# --------------------------------------------------------------- plan --no-storyboard


@needs_ffmpeg
def test_make_plan_no_storyboard_writes_no_storyboard_or_review(tmp_path):
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from test_video import _session_with_video

    from manhwa_ocr.video import build as vb

    _, sess, cfg = _session_with_video(tmp_path)
    vb.make_plan(sess, "1", cfg, sync="off", storyboard=False, log=lambda *_: None)
    assert not list((sess.path / "video").glob("chapter_1.storyboard_*.png"))
    assert not (sess.path / ".work" / "video" / "ch1" / "REVIEW.md").exists()


def test_video_parser_has_autofix_and_no_storyboard():
    from manhwa_ocr.cli import build_parser

    args = build_parser().parse_args(["video", "pair", "--autofix"])
    assert args.autofix is True
    args = build_parser().parse_args(["video", "plan", "--no-storyboard"])
    assert args.no_storyboard is True


# --------------------------------------------------------------- lexical pre-tokenize


def test_lexical_overlap_sets_matches_lexical_overlap():
    cases = [
        ("Minwoo draws the sword of light", "MINWOO draws his sword!!"),
        ("The gate opens", "nothing in common here"),
        ("System notification: quest complete 99", "QUEST COMPLETE 99 - system notification"),
        ("short", "short"),
        ("", "some text"),
        ("some text", ""),
    ]
    for text, panel_text in cases:
        from manhwa_ocr.video import sync as sync_mod
        from manhwa_ocr.video.align_engine import MIN_WORD, STOPWORDS

        p_tokens = {t for t in sync_mod.tokens(panel_text) if len(t) >= MIN_WORD and t not in STOPWORDS}
        assert _lexical_overlap_sets(set(sync_mod.tokens(text)), p_tokens) == _lexical_overlap(text, panel_text)


# --------------------------------------------------------------- syncaudit gate sweep


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


def _seg(n: int, start: float, end: float, text: str, tags, *, action="SHOW", sequence=None):
    sel = tags[0] if tags else None
    return {"sentence": n, "text": text, "start": float(start), "end": float(end), "tagged_panels": list(tags),
            "display_action": action, "selected_panel": sel, "selected_by_align": sel,
            "candidates": [], "confidence": "test",
            "sequence": list(sequence) if sequence is not None else ([sel] if sel else [])}


def test_gate_sweep_finds_a_long_wrong_window():
    # The plan shows P9 (tagged by sentence 2) through sentence 1's whole
    # 12s: one long wrong-picture window. A filler panel tagged by nobody
    # would NOT count as wrong, so the second sentence must own P9.
    segs = [_seg(1, 0.0, 12.0, "The gate opens now", ["P2"]),
            _seg(2, 12.0, 24.0, "He draws the sword", ["P9"])]
    shots = [_shot(1, 0.0, 24.0, [("P9", 0.0)])]
    rep = syncaudit.audit({"chapter": "1", "duration": 24.0, "shots": shots},
                          {"segments": segs}, _words("The gate opens now He draws the sword"))
    over = rep["five_second"]["over_gate"]
    assert over and over[0]["start"] == 0.0 and over[0]["wrong"] == 1.0
    assert rep["gate"]["pass"] is False


def test_gate_sweep_ignores_a_single_straddling_sample():
    # One 0.5s shot of the wrong panel straddles the cut: a single wrong
    # sample in settled time. It is a cut on budget, not drift — the gate
    # needs GATE_WRONG_SECONDS (1.0s) of wrong picture to fail.
    segs = [_seg(1, 0.0, 12.0, "The gate opens now", ["P2"]),
            _seg(2, 12.0, 24.0, "He draws the sword", ["P3"])]
    shots = [_shot(1, 0.0, 11.5, [("P2", 0.0)]),
             _shot(2, 11.5, 12.0, [("P9", 0.0)]),
             _shot(3, 12.0, 24.0, [("P3", 0.0)])]
    rep = syncaudit.audit({"chapter": "1", "duration": 24.0, "shots": shots},
                          {"segments": segs},
                          _words("The gate opens now He draws the sword " * 6))
    assert rep["five_second"]["over_gate"] == []
    # The 0.5s straddle shot itself is illegal under the 3.0s min-shot rule,
    # so the gate fails — but on the short shot, not on the window: the
    # single settled-wrong sample still does not trip the window gate.
    assert rep["gate"]["pass"] is False
    assert not any("five-second window" in f for f in rep["gate"]["failures"])
    assert any("minimum shot" in f for f in rep["gate"]["failures"])


# --------------------------------------------------------------- full scene text


def test_panel_listing_uses_full_scene_text():
    scene = "Minwoo wakes in a vast bathhouse of pale stone, steam rising, " * 6  # >150 chars
    panels = [{"id": "P001-01", "scene": scene, "text": ""}]
    listing = _panel_listing(panels)
    assert scene in listing  # not truncated to 150 chars
