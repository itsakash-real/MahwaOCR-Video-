"""Verification suite covering every shipped feature."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.agentflow import AgentFlow  # noqa: E402
from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.images import slice_tall_image  # noqa: E402
from manhwa_ocr.memory import SeriesMemory  # noqa: E402
from manhwa_ocr.providers import (  # noqa: E402
    PROVIDERS,
    ProviderError,
    build_provider,
    extract_json,
)
from manhwa_ocr.scrapers import GenericScraper, get_scraper  # noqa: E402
from manhwa_ocr.sessions import SessionStore, slugify  # noqa: E402
from manhwa_ocr.util import UserError  # noqa: E402


# --------------------------------------------------------------- config


def test_config_defaults():
    cfg = load_config()
    assert cfg.provider["temperature"] == 0.0
    assert cfg.slicing["enabled"] is True
    assert cfg.slicing["overlap"] > 0


def test_config_overrides(tmp_path):
    cfg = load_config(series__workdir=str(tmp_path), series__name="X")
    assert cfg.workdir == tmp_path.resolve()
    assert cfg.series_name == "X"


def test_config_env_override(monkeypatch):
    monkeypatch.setenv("MANHWA_PROVIDER", "ollama")
    assert load_config().provider["kind"] == "ollama"


def test_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("does-not-exist.yaml")


# --------------------------------------------------------------- slicing


def _strip(tmp_path: Path, w: int, h: int) -> Path:
    """Synthetic tall page with flat gutters between 'panels'."""
    img = Image.new("RGB", (w, h), "white")
    for band in range(0, h, 900):
        for y in range(band, min(band + 700, h)):
            for x in range(0, w, 3):
                img.putpixel((x, y), (20, 20, 20))
    p = tmp_path / "page.png"
    img.save(p)
    return p


def test_slice_tall_strip(tmp_path):
    src = _strip(tmp_path, 900, 9000)
    cfg = load_config().slicing
    tiles = slice_tall_image(src, 1, tmp_path / "t", config=cfg)

    assert len(tiles) > 1
    # Full width preserved -- this is the whole point of slicing.
    for t in tiles:
        with Image.open(t.path) as im:
            assert im.width == 900
    # No gaps, and the strip is covered end to end.
    for a, b in zip(tiles, tiles[1:]):
        assert b.top <= a.bottom
    assert tiles[0].top == 0
    assert tiles[-1].bottom == 9000


def test_short_page_not_sliced(tmp_path):
    src = _strip(tmp_path, 900, 1200)
    tiles = slice_tall_image(src, 1, tmp_path / "t", config=load_config().slicing)
    assert len(tiles) == 1


def test_slicing_can_be_disabled(tmp_path):
    src = _strip(tmp_path, 900, 9000)
    cfg = dict(load_config().slicing)
    cfg["enabled"] = False
    assert len(slice_tall_image(src, 1, tmp_path / "t", config=cfg)) == 1


def test_wide_page_downscaled(tmp_path):
    src = _strip(tmp_path, 2000, 6000)
    tiles = slice_tall_image(src, 1, tmp_path / "t", config=load_config().slicing)
    with Image.open(tiles[0].path) as im:
        assert im.width == load_config().slicing["max_width"]


# --------------------------------------------------------------- providers


def test_all_backends_registered():
    for kind in ("anthropic", "gemini", "openai", "ollama"):
        assert kind in PROVIDERS


def test_missing_key_is_clear(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="No API key"):
        build_provider({"kind": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"})


def test_unknown_backend():
    with pytest.raises(ProviderError, match="Unknown provider"):
        build_provider({"kind": "nope"})


def test_ollama_needs_no_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    p = build_provider({"kind": "ollama", "vision_model": "llava"})
    assert "11434" in p.base_url


def test_text_model_falls_back(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    p = build_provider(
        {
            "kind": "gemini",
            "vision_model": "g",
            "text_model": None,
            "api_key_env": "GEMINI_API_KEY",
        }
    )
    assert p.text_model == p.vision_model


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 2}\n```', {"a": 2}),
        ('Sure!\n{"a": 3}\nDone.', {"a": 3}),
        ('```\n{"a": 4}\n```', {"a": 4}),
        ('{"t": "he said \\"hi\\""}', {"t": 'he said "hi"'}),
        ('[{"a": 5}]', [{"a": 5}]),
        ('prose {"nested": {"deep": [1,2]}} more', {"nested": {"deep": [1, 2]}}),
    ],
)
def test_json_extraction(raw, expected):
    assert extract_json(raw) == expected


def test_json_extraction_failures():
    for bad in ("", "   ", "no json at all"):
        with pytest.raises(ValueError):
            extract_json(bad)


# --------------------------------------------------------------- memory


def test_memory_append_bias():
    """A later vague description must never clobber a detailed earlier one."""
    m = SeriesMemory(series="T")
    m.merge({"characters": [{"name": "A", "appearance": "silver hair, red eyes"}]}, "ch1")
    m.merge({"characters": [{"name": "A", "appearance": "a person", "aliases": ["B"]}]}, "ch2")
    assert m.characters["A"]["appearance"] == "silver hair, red eyes"
    assert "B" in m.characters["A"]["aliases"]
    assert m.characters["A"]["first_seen"] == "ch1"
    assert m.characters["A"]["last_seen"] == "ch2"


def test_memory_fills_blanks():
    m = SeriesMemory(series="T")
    m.merge({"characters": [{"name": "A"}]}, "ch1")
    m.merge({"characters": [{"name": "A", "role": "hero"}]}, "ch2")
    assert m.characters["A"]["role"] == "hero"


def test_memory_no_duplicates():
    m = SeriesMemory(series="T")
    for _ in range(3):
        m.merge(
            {
                "world_facts": ["f"],
                "glossary": {"g": "x"},
                "plot_threads": [{"thread": "t", "status": "open"}],
            },
            "ch1",
        )
    assert m.world_facts == ["f"]
    assert len(m.plot_threads) == 1


def test_memory_thread_resolution():
    m = SeriesMemory(series="T")
    m.merge({"plot_threads": [{"thread": "t", "status": "open"}]}, "ch1")
    m.merge({"plot_threads": [{"thread": "T", "status": "resolved"}]}, "ch2")
    assert len(m.plot_threads) == 1
    assert m.plot_threads[0]["status"] == "resolved"


def test_memory_roundtrip(tmp_path):
    m = SeriesMemory(series="T")
    m.merge({"characters": [{"name": "A", "appearance": "x"}], "world_facts": ["f"]}, "ch1")
    p = tmp_path / "m.json"
    m.save(p)
    again = SeriesMemory.load(p)
    assert again.characters["A"]["appearance"] == "x"
    assert again.world_facts == ["f"]


def test_memory_first_chapter_briefing():
    assert "first chapter" in SeriesMemory(series="T").as_briefing().lower()


def test_memory_ignores_nameless():
    m = SeriesMemory(series="T")
    m.merge({"characters": [{"name": "", "appearance": "x"}, {"appearance": "y"}]}, "ch1")
    assert m.characters == {}


def test_briefing_truncates():
    m = SeriesMemory(series="T")
    m.merge({"world_facts": [f"fact {i} " + "x" * 200 for i in range(100)]}, "ch1")
    assert len(m.as_briefing(max_chars=1000)) < 1200


# --------------------------------------------------------------- sessions


def test_slugify():
    assert slugify("The Hero Cannot Rest!") == "the-hero-cannot-rest"
    assert slugify("  Multi   Space  ") == "multi-space"
    # "_archived" slugifies to "archived", so the reserved set holds the
    # post-slugify form. Both must be refused.
    for bad in ("", "!!!", "active", "_archived", "Archived"):
        with pytest.raises(ValueError):
            slugify(bad)


def test_archive_dir_not_listed_as_session(tmp_path):
    """The store's own _archived folder must never appear as a session."""
    store = SessionStore(tmp_path)
    store.create("Real")
    store.archive("Real")
    (tmp_path / "Another").mkdir()
    assert {s.slug for s in store.list()} == {"Another"}


def test_session_isolation(tmp_path):
    store = SessionStore(tmp_path)
    a = store.create("Series A")
    b = store.create("Series B")

    for sess, char in ((a, "Raon"), (b, "Other")):
        m = SeriesMemory.load(sess.memory_path, sess.name)
        m.merge({"characters": [{"name": char, "appearance": "x"}]}, "ch1")
        m.save(sess.memory_path)

    assert "Raon" in SeriesMemory.load(a.memory_path).characters
    assert "Raon" not in SeriesMemory.load(b.memory_path).characters


def test_session_activation(tmp_path):
    store = SessionStore(tmp_path)
    store.create("A")
    store.create("B")
    assert store.active().slug == "b"
    store.activate("A")
    assert store.active().slug == "a"


def test_session_env_override(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    store.create("A")
    store.create("B")  # B active
    monkeypatch.setenv("MANHWA_SESSION", "A")
    assert store.active().slug == "a"


def test_session_duplicate(tmp_path):
    store = SessionStore(tmp_path)
    store.create("A")
    with pytest.raises(FileExistsError):
        store.create("A")


def test_session_missing(tmp_path):
    with pytest.raises(KeyError):
        SessionStore(tmp_path).get("nope")


def test_session_rename(tmp_path):
    store = SessionStore(tmp_path)
    s = store.create("Old Name")
    (s.path / "chapter_1.md").write_text("x")
    renamed = store.rename("Old Name", "New Name")
    assert renamed.slug == "new-name"
    assert (renamed.path / "chapter_1.md").exists()
    assert store.active().slug == "new-name"  # activation follows the rename


def test_session_rename_when_inactive(tmp_path):
    store = SessionStore(tmp_path)
    store.create("A")
    store.create("B")  # B is active
    store.rename("A", "C")
    assert store.active().slug == "b"  # unchanged


def test_session_archive_preserves_data(tmp_path):
    store = SessionStore(tmp_path)
    s = store.create("Doomed")
    (s.path / "chapter_1.md").write_text("precious")
    dest = store.archive("Doomed")
    assert not s.path.exists()
    assert (dest / "chapter_1.md").read_text() == "precious"
    assert store.active() is None


def test_session_chapter_listing(tmp_path):
    store = SessionStore(tmp_path)
    s = store.create("A")
    for n in ("1", "2", "10"):
        (s.path / f"chapter_{n}.md").write_text("x")
    assert s.chapters() == ["1", "2", "10"]


def test_session_list_empty(tmp_path):
    assert SessionStore(tmp_path / "nothing").list() == []


# --------------------------------------------------------------- agent flow


def test_agent_flow_end_to_end(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    for i in range(2):
        Image.new("RGB", (900, 4000), "white").save(pages / f"p{i}.png")

    cfg = load_config(series__workdir=str(tmp_path / "ws"), series__name="T")
    flow = AgentFlow(cfg, log=lambda *_: None)

    task = flow.prepare(chapter="1", folder=pages)
    assert task.exists()
    text = task.read_text(encoding="utf-8")
    assert "<prompt>" in text and "tile_results.json" in text

    manifest = json.loads((flow._chapter_dir("1") / "manifest.json").read_text())
    assert len(manifest["tiles"]) > 2

    results = [
        {
            "id": t["id"],
            "scene": "s",
            "blocks": [{"type": "dialogue", "speaker": "A", "text": f"line {i}"}],
        }
        for i, t in enumerate(manifest["tiles"])
    ]
    (flow._chapter_dir("1") / "tile_results.json").write_text(json.dumps(results))

    assert flow.assemble(chapter="1").exists()

    (flow._chapter_dir("1") / "assembled.json").write_text(
        json.dumps(
            {
                "transcript": '## Scene 1\n\n**A:** "hi"\n',
                "summary": "s",
                "memory_update": {"characters": [{"name": "A", "appearance": "x"}]},
            }
        )
    )
    out = flow.finalize(chapter="1")
    assert out.exists() and "Scene 1" in out.read_text(encoding="utf-8")
    assert "A" in SeriesMemory.load(flow.memory_path).characters


def test_agent_dedupe_collapses_overlap():
    from manhwa_ocr.merge import flatten

    tiles = [
        ("t1", {"blocks": [{"type": "dialogue", "speaker": "X", "text": "I dont need it."}]}),
        ("t2", {"blocks": [
            {"type": "dialogue", "speaker": "X", "text": "I don't need it!"},
            {"type": "narration", "speaker": None, "text": "Brand new line."},
        ]}),
    ]
    raw = flatten(tiles)
    assert raw.count("need it") == 1
    assert "Brand new line." in raw


def test_agent_flow_missing_results(tmp_path):
    cfg = load_config(series__workdir=str(tmp_path), series__name="T")
    flow = AgentFlow(cfg, log=lambda *_: None)
    work = flow._chapter_dir("9")
    work.mkdir(parents=True)
    with pytest.raises(UserError, match="not been prepared"):
        flow.assemble(chapter="9")
    (work / "manifest.json").write_text('{"tiles": [{"id": "p001_t01", "label": "page 1"}]}')
    with pytest.raises(UserError, match="tile_results"):
        flow.assemble(chapter="9")
    with pytest.raises(UserError, match="assembled"):
        flow.finalize(chapter="9")


# --------------------------------------------------------------- scrapers


def test_scraper_selection():
    assert get_scraper("https://asurascans.com/x").name == "asura"
    assert get_scraper("https://other.com/x").name == "generic"


def test_scraper_filters_junk(monkeypatch):
    html = """
    <img src="/assets/logo.png">
    <img src="https://cdn.x.com/chapters/s/1/aaa.webp">
    <img src="https://cdn.x.com/chapters/s/1/bbb.webp">
    <img src="https://cdn.x.com/chapters/s/1/ccc.webp">
    <img src="/ui/discord-banner.png">
    """
    monkeypatch.setattr(GenericScraper, "fetch_html", staticmethod(lambda *a, **k: html))
    urls = GenericScraper().page_urls("https://x.com/ch/1", {})
    assert len(urls) == 3
    assert all("chapters" in u for u in urls)


def test_scraper_no_images(monkeypatch):
    monkeypatch.setattr(
        GenericScraper, "fetch_html", staticmethod(lambda *a, **k: "<p>hi</p>")
    )
    with pytest.raises(UserError, match="JavaScript"):
        GenericScraper().page_urls("https://x.com/ch/1", {})


# --------------------------------------------------------------- chapter links


def test_links_parse_and_number(tmp_path):
    store = SessionStore(tmp_path)
    s = store.create("A")
    s.links_path.write_text(
        "# a comment\n"
        "\n"
        "https://site.com/ch/alpha-slug\n"
        "https://site.com/ch/beta-slug\n"
        "12 = https://site.com/ch/twelve\n",
        encoding="utf-8",
    )
    assert s.read_links() == [
        ("1", "https://site.com/ch/alpha-slug"),
        ("2", "https://site.com/ch/beta-slug"),
        ("12", "https://site.com/ch/twelve"),
    ]


def test_links_add_dedupes(tmp_path):
    s = SessionStore(tmp_path).create("A")
    assert s.add_links(["https://x.com/1", "https://x.com/2"]) == 2
    assert s.add_links(["https://x.com/2", "https://x.com/3"]) == 1
    assert len(s.read_links()) == 3


def test_links_pending_tracks_done(tmp_path):
    s = SessionStore(tmp_path).create("A")
    s.add_links([f"https://x.com/{i}" for i in (1, 2, 3)])
    assert len(s.pending()) == 3
    (s.path / "chapter_1.md").write_text("x")
    (s.path / "chapter_2.md").write_text("x")
    assert [c for c, _ in s.pending()] == ["3"]


def test_links_ignores_junk_lines(tmp_path):
    s = SessionStore(tmp_path).create("A")
    s.links_path.write_text(
        "not a url\n# comment\n\nftp://nope.com/x\nhttps://ok.com/1\n", encoding="utf-8"
    )
    links, problems = s.parse_links()
    # "ftp://nope.com/x" is a broken link: it is reported and keeps number 1,
    # so the next real link stays chapter 2 instead of being renumbered.
    assert links == [("2", "https://ok.com/1")]
    assert any("malformed" in p for p in problems)
    assert any("not a link" in p for p in problems)


def test_links_empty_when_absent(tmp_path):
    assert SessionStore(tmp_path).create("A").read_links() == []
