"""Regression tests for the full audit. Each test reproduces a specific way the
tool used to break, so the fix can't silently regress."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr import workspace as w  # noqa: E402
from manhwa_ocr.agentflow import AgentFlow  # noqa: E402
from manhwa_ocr.cli import main as cli  # noqa: E402
from manhwa_ocr.config import find_config, load_config  # noqa: E402
from manhwa_ocr.images import build_tiles, list_images  # noqa: E402
from manhwa_ocr.memory import SeriesMemory  # noqa: E402
from manhwa_ocr.merge import flatten  # noqa: E402
from manhwa_ocr.scrapers import extract_image_urls, pick_chapter_pages  # noqa: E402
from manhwa_ocr.script import RecapBuilder  # noqa: E402
from manhwa_ocr.sessions import SessionStore, slugify  # noqa: E402
from manhwa_ocr.status import next_steps  # noqa: E402
from manhwa_ocr.util import (  # noqa: E402
    UserError,
    atomic_write_text,
    chapter_key,
    expand_chapters,
    extract_json,
    normalize_label,
    read_agent_json,
)


# ================================================================== names & labels


def test_non_latin_series_names_work():
    assert slugify("용사는 쉴 수 없다") == "용사는-쉴-수-없다"
    assert slugify("नायक") == "नायक"
    assert slugify("शक्तिशाली नायक") == "शक्तिशाली-नायक"  # vowel signs and virama survive


@pytest.mark.parametrize("name", ["con", "NUL", "com1", "lpt9", "Aux", "  prn  "])
def test_windows_reserved_names_refused(name):
    with pytest.raises(ValueError, match="Windows"):
        slugify(name)


def test_slug_truncation_never_ends_in_a_dash():
    assert not slugify("a" * 63 + " b").endswith("-")


def test_chapter_order_handles_decimals():
    labels = ["10", "2", "10.5", "1", "extra"]
    assert sorted(labels, key=chapter_key) == ["1", "2", "10", "10.5", "extra"]


def test_leading_zeros_match_plain_numbers():
    assert normalize_label("01") == "1"
    assert normalize_label("007.5") == "7.5"
    assert normalize_label("side") == "side"


@pytest.mark.parametrize("spec", ["7-1", "1-999999", ",,", "../x"])
def test_bad_chapter_specs_fail_clearly(spec):
    with pytest.raises(UserError):
        expand_chapters(spec)


def test_chapter_specs_dedupe_and_combine():
    assert expand_chapters("1-3,2,11") == ["1", "2", "3", "11"]


# ================================================================== chapters.txt


def _sess(tmp_path, name="S"):
    return SessionStore(tmp_path).create(name)


def test_trailing_comments_on_link_lines(tmp_path):
    s = _sess(tmp_path)
    s.links_path.write_text("https://x.com/1   # the big fight\n", encoding="utf-8")
    assert s.read_links() == [("1", "https://x.com/1")]


def test_append_to_file_without_final_newline_doesnt_fuse_lines(tmp_path):
    s = _sess(tmp_path)
    s.links_path.write_text("1 = https://x.com/1", encoding="utf-8")  # no newline
    s.add_links(["https://x.com/2"])
    assert s.read_links() == [("1", "https://x.com/1"), ("2", "https://x.com/2")]


def test_trailing_slash_is_the_same_link(tmp_path):
    s = _sess(tmp_path)
    s.add_links(["https://x.com/ch/1"])
    assert s.add_links(["https://x.com/ch/1/"]) == 0


def test_broken_link_keeps_its_chapter_number(tmp_path):
    """Skipping a typo'd line used to shift every later chapter down by one."""
    s = _sess(tmp_path)
    s.links_path.write_text(
        "https://x.com/1\nwww.x.com/2\nhttps://x.com/3\n", encoding="utf-8"
    )
    links, problems = s.parse_links()
    assert links == [("1", "https://x.com/1"), ("3", "https://x.com/3")]
    assert "malformed" in problems[0]


def test_unsafe_labels_are_rejected(tmp_path):
    s = _sess(tmp_path)
    s.links_path.write_text("../../evil = https://x.com/1\n", encoding="utf-8")
    links, problems = s.parse_links()
    assert links == [] and problems


def test_utf16_links_file_from_powershell(tmp_path):
    s = _sess(tmp_path)
    s.links_path.write_bytes("https://x.com/1\r\nhttps://x.com/2\r\n".encode("utf-16"))
    assert [c for c, _ in s.read_links()] == ["1", "2"]


def test_empty_transcript_file_does_not_count_as_done(tmp_path):
    s = _sess(tmp_path)
    s.add_links(["https://x.com/1"])
    (s.path / "chapter_1.md").write_text("")
    assert s.chapters() == []
    assert w.chapter_stage(s.path, "1") == w.STAGE_PREPARE


# ================================================================== file safety


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "a.md"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text() == "two"
    assert [p.name for p in tmp_path.iterdir()] == ["a.md"]


def test_agent_json_tolerates_fences_bom_and_utf16(tmp_path):
    for i, payload in enumerate(
        [
            "```json\n[1, 2]\n```".encode(),
            b"\xef\xbb\xbf[1, 2]",
            "[1, 2]".encode("utf-16"),
            b"Here you go:\n[1, 2]\nDone.",
        ]
    ):
        p = tmp_path / f"f{i}.json"
        p.write_bytes(payload)
        assert read_agent_json(p, "") == [1, 2]


def test_agent_json_error_names_the_line(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('[{"id": "a",\n "blocks": [}]')
    with pytest.raises(UserError, match="line 2"):
        read_agent_json(p, "")


def test_extract_json_prefers_whichever_starts_first():
    assert extract_json('noise [{"a": 1}] trailing {"b": 2}') == [{"a": 1}]


# ================================================================== memory


def test_sloppy_model_output_never_corrupts_memory():
    m = SeriesMemory(series="T")
    m.merge(
        {
            "characters": {"Raon": {"aliases": "The Hero", "appearance": ["black hair", "scars"]}},
            "world_facts": "Mana is rare",
            "glossary": [{"term": "Gate", "meaning": "a dungeon"}, "Aura: visible killing intent"],
            "plot_threads": ["Who is the red-eyed woman?"],
        },
        "chapter 1",
    )
    raon = m.characters["Raon"]
    assert raon["aliases"] == ["The Hero"]  # not ['T','h','e',...]
    assert raon["appearance"] == "black hair, scars"
    assert m.world_facts == ["Mana is rare"]
    assert set(m.glossary) == {"Gate", "Aura"}
    assert m.plot_threads[0]["thread"] == "Who is the red-eyed woman?"


def test_name_case_and_alias_drift_merge_into_one_character():
    m = SeriesMemory(series="T")
    m.merge({"characters": [{"name": "Raon", "aliases": ["The Hero"]}]}, "chapter 1")
    m.merge({"characters": [{"name": "raon"}]}, "chapter 2")
    m.merge({"characters": [{"name": "The Hero", "role": "protagonist"}]}, "chapter 3")
    assert list(m.characters) == ["Raon"]
    assert m.characters["Raon"]["role"] == "protagonist"


def test_hand_fixed_name_is_respected(tmp_path):
    """Editing "name" in the JSON must fix it, not spawn a duplicate."""
    p = tmp_path / "series_memory.json"
    m = SeriesMemory(series="T")
    m.merge({"characters": [{"name": "Kim Dojka", "appearance": "glasses"}]}, "chapter 1")
    m.save(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["characters"]["Kim Dojka"]["name"] = "Kim Dokja"
    p.write_text(json.dumps(data), encoding="utf-8")

    m2 = SeriesMemory.load(p)
    m2.merge({"characters": [{"name": "Kim Dokja"}]}, "chapter 2")
    assert list(m2.characters) == ["Kim Dokja"]
    assert "Kim Dojka" in m2.characters["Kim Dokja"]["aliases"]


def test_corrupt_memory_refuses_instead_of_wiping(tmp_path):
    p = tmp_path / "series_memory.json"
    p.write_text('{"characters": {broken', encoding="utf-8")
    with pytest.raises(UserError, match="not valid JSON"):
        SeriesMemory.load(p)
    assert p.read_text(encoding="utf-8").startswith('{"characters"')  # untouched


def test_every_save_keeps_the_previous_version(tmp_path):
    p = tmp_path / "series_memory.json"
    m = SeriesMemory(series="T")
    m.merge({"world_facts": ["first"]}, "chapter 1")
    m.save(p)
    m.merge({"world_facts": ["second"]}, "chapter 2")
    m.save(p)
    prev = json.loads((tmp_path / "series_memory.prev.json").read_text(encoding="utf-8"))
    assert prev["world_facts"] == ["first"]


def test_briefing_keeps_current_cast_when_truncated():
    m = SeriesMemory(series="T")
    for i in range(1, 80):
        m.merge({"characters": [{"name": f"Extra{i}", "appearance": "x" * 80}]}, f"chapter {i}")
    m.merge({"characters": [{"name": "Hero", "appearance": "scar"}]}, "chapter 80")
    brief = m.as_briefing(max_chars=1500)
    assert "Hero" in brief and "Extra1 " not in brief


def test_briefing_upto_hides_later_reveals():
    m = SeriesMemory(series="T")
    m.merge({"characters": [{"name": "Raon"}], "world_facts": ["Gates exist"]}, "chapter 1")
    m.merge(
        {"characters": [{"name": "Villain"}], "world_facts": ["Raon's mentor is the villain"],
         "plot_threads": [{"thread": "Who betrayed the party?", "status": "open"}]},
        "chapter 9",
    )
    early = m.as_briefing(upto="1")
    assert "Raon" in early and "Gates exist" in early
    assert "Villain" not in early and "mentor" not in early and "betrayed" not in early
    assert "Villain" in m.as_briefing(upto="9")


# ================================================================== agent flow


def _prepared(tmp_path, pages=2):
    src = tmp_path / "pages"
    src.mkdir()
    for i in range(pages):
        Image.new("RGB", (900, 1000), "white").save(src / f"{i + 1}.png")
    ws = tmp_path / "ws"
    flow = AgentFlow(load_config(series__workdir=str(ws), series__name="T"), log=lambda *_: None, session="t")
    flow.prepare(chapter="1", folder=src)
    work = w.chapter_work_dir(ws, "1")
    ids = [t["id"] for t in json.loads((work / "manifest.json").read_text())["tiles"]]
    return flow, ws, work, ids


def test_partial_tile_results_are_not_treated_as_done(tmp_path):
    flow, ws, work, ids = _prepared(tmp_path)
    (work / "tile_results.json").write_text(json.dumps([{"id": ids[0], "blocks": []}]))
    assert w.chapter_stage(ws, "1") == w.STAGE_READ_TILES
    assert "1 of 2" in w.stage_instruction(w.STAGE_READ_TILES, "1", ws)


def test_results_without_ids_match_by_position(tmp_path):
    flow, ws, work, ids = _prepared(tmp_path)
    (work / "tile_results.json").write_text(
        "```json\n" + json.dumps([{"blocks": [{"text": "hello there"}]}, {"blocks": []}]) + "\n```"
    )
    assert w.chapter_stage(ws, "1") == w.STAGE_ASSEMBLE
    flow.assemble(chapter="1")
    assert "hello there" in (work / "raw_blocks.txt").read_text()


def test_results_wrapped_in_an_object(tmp_path):
    flow, ws, work, ids = _prepared(tmp_path)
    (work / "tile_results.json").write_text(
        json.dumps({"tiles": [{"id": i, "blocks": []} for i in ids]})
    )
    flow.assemble(chapter="1")


def test_empty_transcript_rejected_and_chapter_stays_pending(tmp_path):
    flow, ws, work, ids = _prepared(tmp_path)
    (work / "tile_results.json").write_text(json.dumps([{"id": i, "blocks": []} for i in ids]))
    flow.assemble(chapter="1")
    (work / "assembled.json").write_text(json.dumps({"transcript": "  "}))
    with pytest.raises(UserError, match="no transcript"):
        flow.finalize(chapter="1")
    assert not (ws / "chapter_1.md").exists()


def test_finalize_twice_is_harmless(tmp_path):
    flow, ws, work, ids = _prepared(tmp_path)
    (work / "tile_results.json").write_text(json.dumps([{"id": i, "blocks": []} for i in ids]))
    flow.assemble(chapter="1")
    (work / "assembled.json").write_text(json.dumps({"transcript": "## Scene 1\n\nSomething happens here."}))
    first = flow.finalize(chapter="1")
    assert flow.finalize(chapter="1") == first


def test_reprepare_with_changed_tiles_sets_old_results_aside(tmp_path):
    flow, ws, work, ids = _prepared(tmp_path)
    (work / "tile_results.json").write_text(json.dumps([{"id": i, "blocks": []} for i in ids]))
    more = tmp_path / "more"
    more.mkdir()
    for i in range(3):
        Image.new("RGB", (900, 1000), "white").save(more / f"{i + 1}.png")
    flow.prepare(chapter="1", folder=more)
    assert not (work / "tile_results.json").exists()
    assert (work / "tile_results.json.stale").exists()


def test_reprepare_with_same_tiles_keeps_agent_work(tmp_path):
    flow, ws, work, ids = _prepared(tmp_path)
    (work / "tile_results.json").write_text(json.dumps([{"id": ids[0], "blocks": []}]))
    flow.prepare(chapter="1", folder=tmp_path / "pages")
    assert (work / "tile_results.json").exists()


def test_task_commands_are_pinned_to_the_session(tmp_path):
    flow, ws, work, ids = _prepared(tmp_path)
    assert '--session "t"' in (work / "TASK.md").read_text(encoding="utf-8")


def test_next_pins_every_command_to_the_session(tmp_path):
    s = _sess(tmp_path, "My Series")
    s.add_links(["https://x.com/1"])
    assert any('--session "my-series"' in line for line in next_steps(s))


def test_failed_download_offers_folder_workaround(tmp_path):
    s = _sess(tmp_path)
    s.add_links(["https://x.com/1"])
    w.record_failure(s.path, "1", "site refused the request")
    text = "\n".join(next_steps(s))
    assert "--folder" in text and "refused" in text


# ================================================================== merge


def test_repeated_short_line_mid_tile_is_kept():
    tiles = [
        ("t1", {"blocks": [{"text": "Run away now!"}, {"text": "Okay, okay"}]}),
        ("t2", {"blocks": [{"text": "a"}, {"text": "b"}, {"text": "c"}, {"text": "d"}, {"text": "Run away now!"}]}),
    ]
    assert flatten(tiles).count("Run away now!") == 2


# ================================================================== recaps


def _recap_ready(tmp_path, labels=("1", "2")):
    ws = tmp_path / "ws"
    ws.mkdir()
    for c in labels:
        (ws / f"chapter_{c}.md").write_text(f"# {c}\n\nThings happen in chapter {c}.\n")
    return RecapBuilder(load_config(series__workdir=str(ws), series__name="T"), log=lambda *_: None, session="t")


def test_reprepare_never_wipes_an_unfinished_recap_task(tmp_path):
    b = _recap_ready(tmp_path)
    b.prepare(["1"], 6)
    (b.task_dir / "parts.json").write_text(json.dumps({"1": {"narration": "work in progress " * 40}}))
    with pytest.raises(UserError, match="already in progress"):
        b.prepare(["2"], 6)
    assert (b.task_dir / "parts.json").exists()


def test_recap_cancel(tmp_path):
    b = _recap_ready(tmp_path)
    b.prepare(["1"], 6)
    assert b.cancel() and b.pending_task() is None


def test_too_short_narration_is_not_saved(tmp_path):
    b = _recap_ready(tmp_path)
    b.prepare(["1", "2"], 6)
    (b.task_dir / "parts.json").write_text(
        json.dumps({"1": {"narration": "word " * 100}, "2": {"narration": "todo"}})
    )
    written = b.finalize()
    assert [p.name for p in written] == ["chapter_1.md"]
    assert b.pending_task() == ["2"]


def test_recap_prompt_is_spoiler_free(tmp_path):
    b = _recap_ready(tmp_path, ("1", "2", "3"))
    b.memory.merge({"characters": [{"name": "Twist Villain"}]}, "chapter 3")
    assert "Twist Villain" not in b._prompt_for("1", 6)
    assert "Twist Villain" in b._prompt_for("3", 6)


def test_bookends_only_used_for_the_selection_they_were_written_for(tmp_path):
    b = _recap_ready(tmp_path, ("1", "2", "3"))
    b.prepare(["1", "2", "3"], 6)
    (b.task_dir / "parts.json").write_text(json.dumps(
        {c: {"narration": "word " * 100} for c in "123"} | {"_intro": "Hello.", "_outro": "Bye."}
    ))
    b.finalize()
    assert "## Intro" in b.compile(["1", "2", "3"]).read_text(encoding="utf-8")
    assert "## Intro" not in b.compile(["1", "2"]).read_text(encoding="utf-8")


# ================================================================== config & providers


def test_switching_provider_never_sends_another_vendors_key():
    cfg = load_config(provider__kind="gemini")
    assert cfg.provider["api_key_env"] == "GEMINI_API_KEY"
    assert not cfg.provider["vision_model"].startswith("claude")

    cfg = load_config(provider__kind="openai")
    assert cfg.provider["api_key_env"] == "OPENAI_API_KEY"


def test_explicit_custom_key_and_model_are_respected():
    cfg = load_config(provider__kind="openai", provider__api_key_env="OPENROUTER_API_KEY",
                      provider__vision_model="qwen/qwen-2.5-vl")
    assert cfg.provider["api_key_env"] == "OPENROUTER_API_KEY"
    assert cfg.provider["vision_model"] == "qwen/qwen-2.5-vl"


def test_yaml_in_current_folder_is_picked_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MANHWA_CONFIG", raising=False)
    (tmp_path / "manhwa-ocr.yaml").write_text("slicing:\n  target_height: 1000\n")
    assert load_config(find_config()).slicing["target_height"] == 1000


def test_bad_yaml_is_a_clear_error(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("slicing: [unclosed\n")
    with pytest.raises(UserError, match="not valid YAML"):
        load_config(p)


# ================================================================== scraping & images


def test_wordpress_uploads_path_is_not_junk():
    html = "".join(
        f'<img src="https://site.com/wp-content/uploads/WP-manga/data/the-iconic-hero/ch1/{i}.jpg">'
        for i in range(1, 6)
    ) + '<img src="https://site.com/theme/logo.png">'
    urls = pick_chapter_pages(extract_image_urls(html, "https://site.com/ch1"))
    assert len(urls) == 5


def test_lazy_load_data_src_beats_placeholder_src():
    html = '<img src="/img/loading.gif" data-src="https://cdn.x.com/ch/1/001.webp">'
    assert extract_image_urls(html, "https://x.com/") == ["https://cdn.x.com/ch/1/001.webp"]


def test_chapter_split_across_two_folders_is_kept_whole():
    a = [f"https://cdn1.x.com/ch/1/{i}.jpg" for i in range(10)]
    b = [f"https://cdn2.x.com/ch/1/{i}.jpg" for i in range(10)]
    assert len(pick_chapter_pages(a + b + ["https://x.com/ui/thing.png"])) == 20


def test_folder_pages_sort_naturally(tmp_path):
    for n in ("1", "2", "10", "11"):
        Image.new("RGB", (10, 10)).save(tmp_path / f"{n}.jpg")
    Image.new("RGB", (10, 10)).save(tmp_path / "12.gif")
    assert [p.stem for p in list_images(tmp_path)] == ["1", "2", "10", "11", "12"]


def test_changed_slicing_settings_rebuild_tiles(tmp_path):
    page = tmp_path / "p.png"
    Image.new("RGB", (900, 6000), "white").save(page)
    out = tmp_path / "tiles"
    a = build_tiles([page], out, config={"target_height": 1400}, log=lambda *_: None)
    b = build_tiles([page], out, config={"target_height": 900}, log=lambda *_: None)
    assert len(b) > len(a)
    assert len(list(out.glob("*.png"))) == len(b)  # no stale extra tiles left over


def test_corrupt_tile_is_regenerated(tmp_path):
    page = tmp_path / "p.png"
    Image.new("RGB", (900, 6000), "white").save(page)
    out = tmp_path / "tiles"
    tiles = build_tiles([page], out, config={}, log=lambda *_: None)
    tiles[0].path.write_bytes(b"\x89PNG half written")
    build_tiles([page], out, config={}, log=lambda *_: None)
    with Image.open(tiles[0].path) as im:
        im.verify()


# ================================================================== provider-mode cache


def test_failed_tile_responses_are_not_cached(tmp_path, monkeypatch):
    from manhwa_ocr import pipeline as pl

    class Flaky:
        calls = 0

        def fingerprint(self):
            return "fake"

        def vision(self, *a):
            Flaky.calls += 1
            return "rate limited, try later" if Flaky.calls == 1 else '{"scene": "s", "blocks": []}'

        def text(self, *a):
            return '{"transcript": "## Scene 1\\n\\nA long enough transcript.", "memory_update": {}}'

    monkeypatch.setattr(pl, "build_provider", lambda cfg: Flaky())
    src = tmp_path / "pages"
    src.mkdir()
    Image.new("RGB", (900, 1000), "white").save(src / "1.png")
    ws = tmp_path / "ws"
    pipe = pl.Pipeline(load_config(series__workdir=str(ws), series__name="T"), log=lambda *_: None)

    # Hybrid fallback (on by default): the unusable tile shifts to Agent Mode
    # instead of the old hard "unusable responses" stop -- same guarantee that
    # the chapter is never marked done with a gap, and the failed response was
    # never cached, so the retry really retries.
    with pytest.raises(UserError, match="Agent Mode"):
        pipe.run_chapter(chapter="1", folder=src)
    assert not (ws / "chapter_1.md").exists()  # never marked done with a gap
    assert (ws / ".work" / "ch1" / "TASK.md").exists()   # shards were prepared
    pipe.run_chapter(chapter="1", folder=src)  # retry actually retries
    assert (ws / "chapter_1.md").exists()


# ================================================================== CLI


def test_no_session_is_a_friendly_error_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MANHWA_SESSIONS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.chdir(tmp_path)
    assert cli(["prepare", "--chapter", "1", "https://x.com/1"]) == 2
    assert "No series selected" in capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.iterdir()) == []  # no ./out litter


def test_prepare_chapter_n_finds_its_own_link(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MANHWA_SESSIONS_ROOT", str(tmp_path))
    cli(["start", "S"])
    assert cli(["prepare", "--chapter", "4"]) == 2
    assert "No link for chapter 4" in capsys.readouterr().out


def test_clean_keeps_in_progress_chapters(tmp_path, monkeypatch):
    monkeypatch.setenv("MANHWA_SESSIONS_ROOT", str(tmp_path))
    cli(["start", "S"])
    s = SessionStore(tmp_path).get("S")
    for c in ("1", "2"):
        d = w.chapter_work_dir(s.path, c)
        d.mkdir(parents=True)
        (d / "tile_results.json").write_text("[]")
    (s.path / "chapter_1.md").write_text("done")
    cli(["clean", "--yes"])
    assert not w.chapter_work_dir(s.path, "1").exists()
    assert (w.chapter_work_dir(s.path, "2") / "tile_results.json").exists()


def test_memory_reset_never_overwrites_an_earlier_backup(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("MANHWA_SESSIONS_ROOT", str(tmp_path))
    cli(["start", "S"])
    s = SessionStore(tmp_path).get("S")
    for i in range(2):
        s.memory_path.write_text(json.dumps({"world_facts": [f"v{i}"]}))
        cli(["memory", "reset", "--yes"])
        time.sleep(1.1)
    assert len(list(s.path.glob("series_memory.reset-*.json"))) == 2


def test_closed_output_pipe_never_aborts_a_command(tmp_path, monkeypatch):
    """Windows raises OSError(EINVAL) when the reader of stdout goes away."""
    import builtins

    from manhwa_ocr import cli as cli_mod

    monkeypatch.setenv("MANHWA_SESSIONS_ROOT", str(tmp_path))
    monkeypatch.setattr(cli_mod, "_OUTPUT_GONE", False)
    monkeypatch.setattr(cli_mod.os, "dup2", lambda *a: None)  # keep pytest's stdout intact

    def broken_print(*a, **k):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(builtins, "print", broken_print)
    assert cli(["start", "Piped", "https://x.com/1", "https://x.com/2"]) == 0
    assert len(SessionStore(tmp_path).get("Piped").read_links()) == 2


def test_import_normalizes_zero_padded_names(tmp_path, monkeypatch):
    monkeypatch.setenv("MANHWA_SESSIONS_ROOT", str(tmp_path / "s"))
    src = tmp_path / "old"
    src.mkdir()
    (src / "chapter_01.md").write_text("one")
    cli(["start", "S"])
    cli(["import", str(src)])
    assert SessionStore(tmp_path / "s").get("S").chapters() == ["1"]


# ================================================================== dashboard


def test_dashboard_opens_non_latin_series(tmp_path):
    import threading
    import urllib.parse
    import urllib.request

    from manhwa_ocr.ui.server import LocalServer, make_handler

    store = SessionStore(tmp_path)
    store.create("용사는 쉴 수 없다")
    httpd = LocalServer(("127.0.0.1", 0), make_handler(store, lambda *_: None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        slug = urllib.parse.quote("용사는-쉴-수-없다")
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/session/{slug}"
        data = json.loads(urllib.request.urlopen(url, timeout=5).read())
        assert data["name"] == "용사는 쉴 수 없다"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.skipif(not hasattr(socket, "SO_EXCLUSIVEADDRUSE"), reason="Windows-only socket option")
def test_dashboard_port_cannot_be_shared(tmp_path):
    from manhwa_ocr.ui.server import LocalServer, make_handler

    store = SessionStore(tmp_path)
    first = LocalServer(("127.0.0.1", 0), make_handler(store, lambda *_: None))
    try:
        with pytest.raises(OSError):
            LocalServer(("127.0.0.1", first.server_address[1]), make_handler(store, lambda *_: None))
    finally:
        first.server_close()


def test_output_folder_from_config_is_relative_to_the_config(tmp_path, monkeypatch):
    from manhwa_ocr.config import output_folder
    from manhwa_ocr.sessions import SessionStore

    prog = tmp_path / "prog"
    prog.mkdir()
    cfg = prog / "manhwa-ocr.yaml"
    cfg.write_text('output_folder: "../Out"\nvideo: {fps: 30}\n')
    assert output_folder(cfg) == (tmp_path / "Out").resolve()
    monkeypatch.delenv("MANHWA_SESSIONS_ROOT", raising=False)
    monkeypatch.setenv("MANHWA_CONFIG", str(cfg))
    assert SessionStore().root == (tmp_path / "Out").resolve()
    monkeypatch.setenv("MANHWA_SESSIONS_ROOT", str(tmp_path / "env"))
    assert SessionStore().root == (tmp_path / "env").resolve()     # the env var still wins
    assert SessionStore(tmp_path / "arg").root == (tmp_path / "arg").resolve()


def test_config_without_output_folder_keeps_the_default(tmp_path, monkeypatch):
    from manhwa_ocr.config import load_config, output_folder

    cfg = tmp_path / "manhwa-ocr.yaml"
    cfg.write_text("video: {fps: 24}\n")
    assert output_folder(cfg) is None
    assert load_config(cfg).video["fps"] == 24          # a top-level output_folder never breaks loading
    cfg.write_text('output_folder: "../x"\nvideo: {fps: 24}\n')
    assert load_config(cfg).video["fps"] == 24


def test_code_guard_blocks_a_modified_program(tmp_path, monkeypatch):
    from manhwa_ocr import guard

    prog = tmp_path / "prog"
    (prog / "manhwa_ocr").mkdir(parents=True)
    (prog / "manhwa_ocr" / "a.py").write_text("x = 1\n")
    (prog / "AGENTS.md").write_text("rules\n")
    monkeypatch.setattr(guard, "ROOT", prog)
    monkeypatch.setattr(guard, "LOCK_FILE", prog / ".code-lock.json")
    guard.check()                                    # unlocked: nothing to check
    guard.lock(log=lambda *_: None)
    guard.check()                                    # intact: runs
    import os, stat
    target = prog / "manhwa_ocr" / "a.py"
    assert not os.access(target, os.W_OK)            # read-only while locked
    os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
    target.write_text("x = 2\n")
    (prog / "manhwa_ocr" / "evil.py").write_text("")
    with pytest.raises(UserError, match="changed: manhwa_ocr/a.py"):
        guard.check()
    assert "added:   manhwa_ocr/evil.py" in guard.changes()
    guard.unlock(log=lambda *_: None)
    guard.check()
    assert os.access(target, os.W_OK)
