"""Intro + Previously: auto panel picking, hook structure, memory, previously.

Covers `video hook`, `video intro --auto` and `video previously` (hook.py) plus
the backward-compat path: manual intro.json still builds a plan via intro.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.cli import main as cli  # noqa: E402
from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.sessions import SessionStore  # noqa: E402
from manhwa_ocr.video import hook as hook_mod  # noqa: E402
from manhwa_ocr.video import intro as intro_mod  # noqa: E402
from manhwa_ocr.video.media import pages_dir  # noqa: E402
from manhwa_ocr.video.panels import build_panels, load_panels  # noqa: E402


def _ffmpeg_present() -> bool:
    try:
        from manhwa_ocr.video.ffmpeg import ffmpeg_exe

        ffmpeg_exe()
        return True
    except Exception:  # noqa: BLE001
        return False


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_present(), reason="FFmpeg not available")


def _panel(chapter, pid, order, text="", aspect=1.0, area=10000, **kw):
    w, h = 100.0, 100.0 * aspect
    return {"ref": f"ch{chapter}:{pid}", "chapter": chapter, "id": pid, "order": order,
            "text": text, "width": w, "height": h, "aspect": aspect, "area": w * h,
            "text_only": False, "credits": False, "prior_video": False, **kw}


def _pool():
    # 8 panels: 7 plain square panels, then a tall dragon payoff panel last.
    pool = [_panel("1", f"P001-0{i}", i, text="cat") for i in range(1, 8)]
    pool.append(_panel("2", "P002-01", 7, text="dragon", aspect=2.0))
    return pool


# ------------------------------------------------------------------ parsing


def test_parse_chapters_spec():
    assert hook_mod.parse_chapters_spec("1-6") == ["1", "2", "3", "4", "5", "6"]
    assert hook_mod.parse_chapters_spec("1-3,7") == ["1", "2", "3", "7"]
    assert hook_mod.parse_chapters_spec("6") == ["6"]
    assert hook_mod.parse_chapters_spec("1-2,2-3") == ["1", "2", "3"]
    with pytest.raises(Exception):
        hook_mod.parse_chapters_spec("6-1")


def test_parse_script_markers():
    sents = hook_mod.parse_script(
        "[HOOK]\nHe wakes up.\nThe stakes rise fast.\n[LOOP: will he survive?]\nHe walks on.\n")
    assert [s["text"] for s in sents] == ["He wakes up.", "The stakes rise fast.", "He walks on."]
    assert sents[0]["hook"] is True and sents[0]["loops"] == []
    assert sents[2]["loops"] == ["will he survive?"]
    assert sents[1]["hook"] is False


def test_parse_script_inline_markers_and_reuse():
    sents = hook_mod.parse_script("He wakes up. [REUSE]\n[BEAT] The dragon arrives.\n")
    assert sents[0]["reuse"] is True
    assert sents[1]["beat"] is True
    assert "[REUSE]" not in sents[0]["text"] and "[BEAT]" not in sents[1]["text"]


def test_parse_cues_data():
    sents = hook_mod.parse_cues_data(
        {"sentences": [{"text": "Hello.", "hook": True, "loops": ["what next?"]},
                       {"text": "World."}]})
    assert sents[0]["hook"] is True and sents[0]["loops"] == ["what next?"]
    assert sents[1]["hook"] is False


# ------------------------------------------------------------------ auto-pick


def test_auto_pick_no_repeats_no_text_only_no_credits():
    pool = _pool()
    pool.append(_panel("2", "P002-02", 8, text="speech only", text_only=True))
    pool.append(_panel("2", "P002-03", 9, text="credits", credits=True))
    pool.append(_panel("2", "P002-04", 10, text="cat"))
    picks = hook_mod.auto_pick(
        [{"text": "A cat sleeps."}, {"text": "Another cat wakes."}, {"text": "A third cat runs."}],
        pool, seed=7)
    refs = [p["panels"][0] for p in picks]
    assert len(set(refs)) == 3
    assert not any("P002-02" in r or "P002-03" in r for r in refs)


def test_auto_pick_face_preference():
    # Same overlap (none), same area: the portrait panel beats the square one.
    pool = [_panel("1", "P001-01", 0, text="zzz", aspect=1.0),
            _panel("1", "P001-02", 1, text="zzz", aspect=2.0),
            _panel("1", "P001-03", 2, text="zzz", aspect=1.0)]
    picks = hook_mod.auto_pick([{"text": "Nothing matches anything here."},
                                {"text": "Still nothing matches."}], pool, seed=7)
    assert picks[0]["panels"][0] == "ch1:P001-02"  # portrait close-up proxy wins
    assert "close-up" in picks[0]["reason"]


def test_auto_pick_payoff_ordering_tease_ceiling():
    # Sentence 1 never names the dragon: it must not jump to the late payoff
    # panel. Sentence 2 names it: the ceiling lifts and it may show.
    picks = hook_mod.auto_pick([{"text": "He wakes up quietly."},
                                {"text": "The dragon arrives."}], _pool(), seed=7)
    assert picks[0]["panels"][0] != "ch2:P002-01"
    assert picks[1]["panels"][0] == "ch2:P002-01"
    orders = [next(p["order"] for p in _pool() if p["ref"] == pk["panels"][0]) for pk in picks]
    assert orders == sorted(orders)


def test_auto_pick_deterministic_same_seed():
    sents = [{"text": "A cat sleeps."}, {"text": "The dragon arrives."}]
    a = hook_mod.auto_pick(sents, _pool(), seed=7)
    b = hook_mod.auto_pick(sents, _pool(), seed=7)
    assert a == b


def test_auto_pick_respects_memory_unless_reuse():
    pool = _pool()
    first = hook_mod.auto_pick([{"text": "A cat sleeps."}], pool, seed=7)
    used = {first[0]["panels"][0]}
    second = hook_mod.auto_pick([{"text": "A cat sleeps."}], pool, used_refs=used, seed=7)
    assert second[0]["panels"][0] != first[0]["panels"][0]
    reuse = hook_mod.auto_pick([{"text": "A cat sleeps.", "reuse": True}], pool,
                               used_refs=used, seed=7)
    assert reuse[0]["panels"][0] == first[0]["panels"][0]  # [REUSE] may repeat


# ------------------------------------------------------------------ hook structure


def test_hook_structure_warns_without_markers():
    timed = [{"text": "He wakes.", "start": 0.0, "end": 6.0},
             {"text": "He walks.", "start": 10.0, "end": 18.0},
             {"text": "He keeps walking.", "start": 20.0, "end": 28.0}]
    out = hook_mod.hook_structure(timed, 30.0)
    assert out["sections"] == {"proof": [1], "stakes": [2], "roadmap": [3]}
    assert len(out["warnings"]) == 3  # no HOOK, no stakes, no LOOP


def test_hook_structure_quiet_with_markers():
    timed = [{"text": "He wakes.", "start": 0.0, "end": 6.0, "hook": True},
             {"text": "The enemy attacks.", "start": 10.0, "end": 18.0, "beat": True},
             {"text": "He walks on.", "start": 20.0, "end": 28.0, "loops": ["will he survive?"]}]
    assert hook_mod.hook_structure(timed, 30.0)["warnings"] == []


def test_duration_warning():
    settings = hook_mod.hook_settings({"hook": {}})
    assert hook_mod.duration_warning(30.0, settings) is None
    assert "over" in (hook_mod.duration_warning(50.0, settings) or "")
    assert "under" in (hook_mod.duration_warning(10.0, settings) or "")


# ------------------------------------------------------------------ memory


def test_memory_roundtrip_and_no_repeat(tmp_path):
    root = tmp_path
    (root / "video").mkdir()
    mem = hook_mod.load_memory(root)
    picks = [{"text": "A cat sleeps.", "panels": ["ch1:P001-01"], "loops": ["will he return?"]}]
    hook_mod.record_use(mem, "intro", picks)
    hook_mod.save_memory(root, mem)
    again = hook_mod.load_memory(root)
    assert again["intro"]["panels_used"] == ["ch1:P001-01"]
    assert again["intro"]["beats_covered"] == ["A cat sleeps."]
    assert again["open_loops"][0]["loop"] == "will he return?"
    assert hook_mod.memory_warnings(again, ["ch1:P001-01", "ch1:P001-02"]) == [
        "ch1:P001-01 was already used in intro/previously -- mark its sentence [REUSE] to reuse it"]
    assert hook_mod.used_refs(again) == {"ch1:P001-01"}


def test_memory_record_previously_timestamp(tmp_path):
    (tmp_path / "video").mkdir()
    mem = hook_mod.load_memory(tmp_path)
    hook_mod.record_use(mem, "previously", [{"text": "Before.", "panels": ["ch1:P001-01"]}],
                        extra={"for_chapters": ["1", "2"]})
    assert mem["previously"]["last_previously_timestamp"]
    assert mem["previously"]["last_previously_for"] == ["1", "2"]


# ------------------------------------------------------------------ previously beats


def test_select_beats_even_coverage():
    sents = [{"text": f"Beat number {i} happened here today.", "chapter": "1"} for i in range(20)]
    beats = hook_mod.select_beats(sents, count=8)
    assert len(beats) == 8
    assert beats[0]["text"].startswith("Beat number 0")
    assert beats[-1]["text"].startswith("Beat number 1")  # last window, not the first beat again
    assert len({b["text"] for b in beats}) == 8


def test_select_beats_prefers_open_threads():
    sents = [{"text": "Something ordinary happened here today.", "chapter": "1"},
             {"text": "The death lord trial began at last.", "chapter": "1"},
             {"text": "Another ordinary thing happened here today.", "chapter": "1"}]
    beats = hook_mod.select_beats(sents, count=1, open_threads=["death lord trial"])
    assert "death lord" in beats[0]["text"]


# ------------------------------------------------------------------ session builds


def _fake_chapter(folder: Path, seed: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (300, 1500), (255, 255, 255))
    d = ImageDraw.Draw(img)
    colours = [(200, 60, 60), (60, 160, 60), (60, 60, 200), (180, 160, 40), (150, 60, 170)]
    y = 40
    for k, c in enumerate(colours):
        c = tuple((v + seed * 20 + k * 7) % 256 for v in c)
        d.rectangle((10, y, 289, y + 240), fill=c)
        for x in range(10, 290, 7):
            d.line((x, y, x, y + 240), fill=(c[0] // 2, c[1] // 2, c[2] // 2))
        y += 300
    img.save(folder / "page_001.png")


@pytest.fixture()
def hook_session(tmp_path):
    store = SessionStore(tmp_path)
    sess = store.create("HookSeries")
    for c, seed in ((("1", 1), ("2", 2), ("3", 3))):
        (sess.path / f"chapter_{c}.md").write_text(f"# {c}\n\ntext\n")
        _fake_chapter(pages_dir(sess.path, c), seed)
    cfg = load_config(series__workdir=str(sess.path), series__name="HookSeries")
    cfg.video["workers"] = 1
    for c in ("1", "2", "3"):
        build_panels(sess.path, c, cfg.video.get("panels"), log=lambda *_: None)
    return store, sess, cfg


def _sine(sess, name, seconds=12):
    from manhwa_ocr.video.ffmpeg import run

    (sess.path / "audio").mkdir(exist_ok=True)
    run(["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "libmp3lame",
         str(sess.path / "audio" / name)])


def test_build_pool_only_given_chapters(hook_session):
    _, sess, _ = hook_session
    pool = hook_mod.build_pool(sess.path, ["1", "2"])
    assert pool and all(p["chapter"] in ("1", "2") for p in pool)
    assert not any(p["ref"].startswith("ch3:") for p in pool)  # no next-part leak


def test_build_pool_prefers_prior_video_panels(hook_session):
    _, sess, _ = hook_session
    ids = [p["id"] for p in load_panels(sess.path, "1")["panels"] if not p.get("text_only")]
    (sess.path / "video").mkdir(exist_ok=True)
    (sess.path / "video" / "chapter_1.plan.json").write_text(json.dumps(
        {"shots": [{"cards": [{"panel": ids[0]}]}]}), encoding="utf-8")
    pool = hook_mod.build_pool(sess.path, ["1"])
    flagged = {p["id"]: p["prior_video"] for p in pool}
    assert flagged[ids[0]] is True
    assert all(not v for k, v in flagged.items() if k != ids[0])


@needs_ffmpeg
def test_hook_builds_plan_no_cards_one_callout_max(hook_session):
    _, sess, cfg = hook_session
    _sine(sess, "chapter_0.mp3", seconds=12)
    (sess.path / "intro").mkdir(exist_ok=True)
    (sess.path / "intro" / "intro.txt").write_text(
        "[HOOK]\nHe wakes up in a strange new world.\nThe enemy attacks the village at dawn.\n"
        "[LOOP: will he survive?]\nHe walks on toward the trial.\n", encoding="utf-8")
    plan, out = hook_mod.build_hook(sess, cfg, chapters=["1", "2"], plan_only=True,
                                    sync="off", log=lambda *_: None)
    assert (sess.path / "video" / "chapter_0.plan.json").exists()
    assert plan["chapter_cards"] == []  # no chapter cards in a 30s intro
    assert sum(len(s.get("callouts") or []) for s in plan["shots"]) <= 1
    auto = json.loads((sess.path / "intro" / "intro.auto.json").read_text(encoding="utf-8"))
    assert auto["kind"] == "hook" and len(auto["sentences"]) == 3
    assert all("reason" in s for s in auto["sentences"])
    mem = hook_mod.load_memory(sess.path)
    assert mem["intro"]["panels_used"]  # recorded
    assert out is None  # plan_only: no render


@needs_ffmpeg
def test_previously_only_previous_chapters(hook_session):
    _, sess, cfg = hook_session
    # Chapters 1-2 are the previous part, so the bridge is "prev3".
    _sine(sess, "previously_3.mp3", seconds=12)
    (sess.path / "previously").mkdir(exist_ok=True)
    (sess.path / "previously" / "previously_3.txt").write_text(
        "Previously, he woke up in a strange new world.\n"
        "The enemy attacked the village at dawn.\n"
        "He walked on toward the trial.\n", encoding="utf-8")
    (sess.path / "recaps").mkdir(exist_ok=True)
    for c in ("1", "2"):
        (sess.path / "recaps" / f"chapter_{c}.md").write_text(
            f"# Chapter {c}\n\nHe woke up in a strange new world. "
            "The enemy attacked the village at dawn. He walked on toward the trial.\n",
            encoding="utf-8")
    plan, _ = hook_mod.build_previously(sess, cfg, chapters=["1", "2"], plan_only=True,
                                        sync="off", log=lambda *_: None)
    assert (sess.path / "video" / "chapter_prev3.plan.json").exists()
    shown = [c["panel"] for s in plan["shots"] for c in s["cards"]]
    assert shown and all(p.startswith(("ch1:", "ch2:")) for p in shown)  # never ch3
    assert plan["chapter_cards"] == []
    mem = hook_mod.load_memory(sess.path)
    assert mem["previously"]["last_previously_timestamp"]
    assert mem["previously"]["last_previously_for"] == ["1", "2"]
    auto = json.loads((sess.path / "previously" / "previously_prev3.auto.json").read_text(encoding="utf-8"))
    assert auto["kind"] == "previously" and auto["beats"]


@needs_ffmpeg
def test_cli_hook_dry_run(hook_session, monkeypatch):
    store, sess, cfg = hook_session
    monkeypatch.setenv("MANHWA_SESSIONS_ROOT", str(store.root))
    _sine(sess, "chapter_0.mp3", seconds=12)
    (sess.path / "intro").mkdir(exist_ok=True)
    (sess.path / "intro" / "intro.txt").write_text(
        "He wakes up in a strange new world.\nThe enemy attacks.\nHe walks on.\n",
        encoding="utf-8")
    assert cli(["video", "hook", "--chapters", "1-2", "--dry-run", "--no-sync",
                "--session", sess.slug]) == 0
    assert (sess.path / "video" / "chapter_0.plan.json").exists()


@needs_ffmpeg
def test_manual_intro_still_builds_plan(hook_session):
    """Backward compat: intro.json + chapter_0.mp3 -> plan, unchanged behaviour."""
    _, sess, cfg = hook_session
    _sine(sess, "chapter_0.mp3", seconds=9)
    a = [p["id"] for p in load_panels(sess.path, "1")["panels"] if not p.get("text_only")]
    b = [p["id"] for p in load_panels(sess.path, "2")["panels"] if not p.get("text_only")]
    (sess.path / "intro").mkdir(exist_ok=True)
    intro_doc = {"version": 1, "sentences": [
        {"text": "He chopped trees for a living every single day of his quiet life.",
         "panels": [f"ch1:{a[0]}"]},
        {"text": "Then something woke up inside him and everything began to change.",
         "panels": [f"ch1:{a[1]}", f"ch2:{b[0]}"], "beat": True},
        {"text": "Nobody in the village would ever look at him the same way again.",
         "panels": [f"ch2:{b[1]}"]}]}
    (sess.path / "intro" / "intro.json").write_text(json.dumps(intro_doc), encoding="utf-8")
    plan, _ = intro_mod.build_intro(sess, cfg, sync="off", log=lambda *_: None)
    assert (sess.path / "video" / "chapter_0.plan.json").exists()
    assert len(plan["shots"]) >= 2
