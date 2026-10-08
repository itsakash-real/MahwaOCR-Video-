"""Scratch layout, resume behaviour, per-chapter recaps, compile, and the dashboard."""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr import workspace as w  # noqa: E402
from manhwa_ocr.agentflow import AgentFlow  # noqa: E402
from manhwa_ocr.cli import main as cli_main  # noqa: E402
from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.script import (  # noqa: E402
    SCRIPT_SYSTEM,
    RecapBuilder,
    _count_words,
    _spec_name,
    _target_words,
)
from manhwa_ocr.sessions import SessionStore  # noqa: E402
from manhwa_ocr.status import next_steps, session_detail  # noqa: E402
from manhwa_ocr.util import UserError  # noqa: E402


def _page(dirpath: Path, name: str = "p1.png") -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (900, 4000), "white").save(dirpath / name)
    return dirpath


def _flow(tmp_path: Path):
    ws = tmp_path / "ws"
    cfg = load_config(series__workdir=str(ws), series__name="T")
    return ws, AgentFlow(cfg, log=lambda *_: None)


def _advance_to_finalize(flow: AgentFlow, ws: Path, chapter: str = "1") -> Path:
    work = w.chapter_work_dir(ws, chapter)
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    (work / "tile_results.json").write_text(
        json.dumps([{"id": t["id"], "scene": "s", "blocks": []} for t in manifest["tiles"]]),
        encoding="utf-8",
    )
    flow.assemble(chapter=chapter)
    (work / "assembled.json").write_text(
        json.dumps({"transcript": "## Scene 1\n\nThe hero arrives at the gate.\n", "summary": "", "memory_update": {}}),
        encoding="utf-8",
    )
    return work


# =============================================================== scratch layout


def test_scratch_stays_out_of_session_root(tmp_path):
    pages = _page(tmp_path / "pages")
    ws, flow = _flow(tmp_path)
    flow.prepare(chapter="1", folder=pages)
    assert (ws / w.WORK_DIRNAME / "ch1" / "TASK.md").exists()
    top = {p.name for p in ws.iterdir()}
    assert "ch1" not in top and "tiles" not in top and "pages" not in top


def test_finalize_cleans_scratch(tmp_path):
    pages = _page(tmp_path / "pages")
    ws, flow = _flow(tmp_path)
    flow.prepare(chapter="1", folder=pages)
    work = _advance_to_finalize(flow, ws)
    assert flow.finalize(chapter="1").exists()
    assert not work.exists()


def test_finalize_keep_preserves_scratch(tmp_path):
    pages = _page(tmp_path / "pages")
    ws, flow = _flow(tmp_path)
    flow.prepare(chapter="1", folder=pages)
    work = _advance_to_finalize(flow, ws)
    flow.finalize(chapter="1", keep_work=True)
    assert work.exists()


def test_cleanup_disabled_in_config(tmp_path):
    pages = _page(tmp_path / "pages")
    ws = tmp_path / "ws"
    cfg = load_config(series__workdir=str(ws), series__name="T")
    cfg.pipeline["cleanup"] = False
    flow = AgentFlow(cfg, log=lambda *_: None)
    flow.prepare(chapter="1", folder=pages)
    work = _advance_to_finalize(flow, ws)
    flow.finalize(chapter="1")
    assert work.exists()


def test_clean_all_reports_and_removes(tmp_path):
    for ch in ("1", "2"):
        d = w.chapter_work_dir(tmp_path, ch) / "tiles"
        d.mkdir(parents=True)
        (d / "x.bin").write_bytes(b"0" * 2048)
    assert len(w.list_work(tmp_path)) == 2
    assert w.clean_all(tmp_path)[0] == 2
    assert w.list_work(tmp_path) == []


def test_clean_is_safe_when_nothing_present(tmp_path):
    assert w.clean_all(tmp_path) == (0, 0.0)
    assert w.clean_chapter(tmp_path, "9") == 0.0


def test_chapter_stage_machine(tmp_path):
    work = w.chapter_work_dir(tmp_path, "1")
    assert w.chapter_stage(tmp_path, "1") == w.STAGE_PREPARE
    work.mkdir(parents=True)
    (work / "manifest.json").write_text('{"tiles": [{"id": "a"}, {"id": "b"}]}')
    (work / "TASK.md").write_text("x")
    assert w.chapter_stage(tmp_path, "1") == w.STAGE_READ_TILES
    (work / "tile_results.json").write_text('[{"id": "a", "blocks": []}]')
    assert w.chapter_stage(tmp_path, "1") == w.STAGE_READ_TILES  # 1 of 2: still reading
    (work / "tile_results.json").write_text('[{"id": "a", "blocks": []}, {"id": "b", "blocks": []}]')
    assert w.chapter_stage(tmp_path, "1") == w.STAGE_ASSEMBLE
    (work / "ASSEMBLE.md").write_text("x")
    assert w.chapter_stage(tmp_path, "1") == w.STAGE_ANSWER
    (work / "assembled.json").write_text("{}")
    assert w.chapter_stage(tmp_path, "1") == w.STAGE_FINALIZE
    (tmp_path / "chapter_1.md").write_text("x")
    assert w.chapter_stage(tmp_path, "1") == w.STAGE_DONE


def test_finished_chapter_reports_done_even_without_scratch(tmp_path):
    (tmp_path / "chapter_5.md").write_text("done")
    assert w.chapter_stage(tmp_path, "5") == w.STAGE_DONE


# =============================================================== resume + numbering


def test_resume_picks_the_new_link_not_the_old_one(tmp_path):
    """Add link 1, transcribe it, stop, add link 2, restart -> chapter 2 is next."""
    store = SessionStore(tmp_path)
    s = store.create("Series")
    s.add_links(["https://x.com/one"])
    (s.path / "chapter_1.md").write_text("done")

    s.add_links(["https://x.com/two"])
    assert s.pending() == [("2", "https://x.com/two")]
    assert any("Chapter 2" in line for line in next_steps(s))


def test_added_links_get_pinned_numbers(tmp_path):
    s = SessionStore(tmp_path).create("A")
    s.add_links(["https://x.com/a", "https://x.com/b"])
    text = s.links_path.read_text(encoding="utf-8")
    assert "1 = https://x.com/a" in text and "2 = https://x.com/b" in text


def test_hand_inserting_a_line_never_relabels_finished_chapters(tmp_path):
    """The bug this guards: positional numbering shifted on a mid-file insert."""
    s = SessionStore(tmp_path).create("A")
    s.add_links(["https://x.com/a", "https://x.com/b"])
    (s.path / "chapter_1.md").write_text("done")
    (s.path / "chapter_2.md").write_text("done")

    lines = s.links_path.read_text(encoding="utf-8").splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.startswith("1 ="))
    lines.insert(idx, "https://x.com/inserted")  # bare URL wedged in at the top
    s.links_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    links = dict(s.read_links())
    assert links["2"] == "https://x.com/b"  # unchanged
    assert [c for c, _ in s.pending()] == []  # nothing finished looks unfinished


def test_bare_urls_continue_after_explicit_numbers(tmp_path):
    s = SessionStore(tmp_path).create("A")
    s.links_path.write_text(
        "47 = https://x.com/47\nhttps://x.com/48\nhttps://x.com/49\n", encoding="utf-8"
    )
    assert [c for c, _ in s.read_links()] == ["47", "48", "49"]


def test_duplicate_labels_keep_first(tmp_path):
    s = SessionStore(tmp_path).create("A")
    s.links_path.write_text("3 = https://x.com/a\n3 = https://x.com/b\n", encoding="utf-8")
    assert s.read_links() == [("3", "https://x.com/a")]


def test_add_links_continues_after_highest(tmp_path):
    s = SessionStore(tmp_path).create("A")
    s.links_path.write_text("10 = https://x.com/10\n", encoding="utf-8")
    s.add_links(["https://x.com/11"])
    assert s.read_links()[-1] == ("11", "https://x.com/11")


# =============================================================== recaps


def _transcripts(tmp_path: Path, labels=("1", "2", "3")):
    ws = tmp_path / "ws"
    ws.mkdir()
    for c in labels:
        (ws / f"chapter_{c}.md").write_text(
            "# Ch {0}\n\n## Scene 1\n\nSomething happens in chapter {0}.\n".format(c),
            encoding="utf-8",
        )
    cfg = load_config(series__workdir=str(ws), series__name="T")
    return cfg, RecapBuilder(cfg, log=lambda *_: None)


def _answer(builder: RecapBuilder, chapters, words=900, extra=None):
    parts = {c: {"narration": "word " * words, "hook": f"hook {c}"} for c in chapters}
    parts.update(extra or {})
    (builder.task_dir / "parts.json").write_text(json.dumps(parts), encoding="utf-8")


def test_word_targets():
    assert _target_words(5) == 750
    assert _target_words(8) == 1200


def test_prompt_demands_original_writing():
    low = SCRIPT_SYSTEM.lower()
    assert "original narration" in low and "sparingly" in low


def test_word_count_excludes_cues_and_comments():
    assert _count_words("one [Panel: big shot] two <!-- meta --> three") == 3


def test_spec_name():
    assert _spec_name(["1", "2", "3", "4", "11"]) == "1-4_11"
    assert _spec_name(["5"]) == "5"
    assert _spec_name(["3", "1", "2"]) == "1-3"


def test_recap_one_chapter_writes_its_own_file(tmp_path):
    cfg, b = _transcripts(tmp_path)
    b.prepare(["2"], 6)
    _answer(b, ["2"])
    written = b.finalize()
    assert [p.name for p in written] == ["chapter_2.md"]
    assert b.recapped() == ["2"]
    assert not b.task_dir.exists()  # task cleaned once fully answered


def test_recap_range_with_gap(tmp_path):
    cfg, b = _transcripts(tmp_path, ("1", "2", "3", "4", "11"))
    b.prepare(["1", "2", "3", "4", "11"], 6)
    _answer(b, ["1", "2", "3", "4", "11"])
    b.finalize()
    assert b.recapped() == ["1", "2", "3", "4", "11"]


def test_recap_skips_existing_unless_forced(tmp_path):
    cfg, b = _transcripts(tmp_path)
    b.prepare(["1"], 6)
    _answer(b, ["1"])
    b.finalize()

    assert b.prepare(["1"], 6) is None  # nothing to do
    task = b.prepare(["1", "2"], 6)     # only the gap
    assert json.loads((b.task_dir / "plan.json").read_text())["chapters"] == ["2"]
    assert task is not None

    b.prepare(["1"], 6, force=True)
    assert json.loads((b.task_dir / "plan.json").read_text())["chapters"] == ["1"]


def test_recap_refuses_untranscribed_chapter(tmp_path):
    cfg, b = _transcripts(tmp_path)
    with pytest.raises(UserError, match="No transcript"):
        b.prepare(["9"], 6)


def test_recap_partial_answer_keeps_task(tmp_path):
    cfg, b = _transcripts(tmp_path)
    b.prepare(["1", "2"], 6)
    _answer(b, ["1"])
    b.finalize()
    assert b.recapped() == ["1"]
    assert b.task_dir.exists()  # chapter 2 still owed


def test_recap_metadata_roundtrip(tmp_path):
    cfg, b = _transcripts(tmp_path)
    b.prepare(["1"], 7)
    _answer(b, ["1"], words=1050)
    path = b.finalize()[0]
    meta = b._read_meta(path)
    assert meta["words"] == 1050 and meta["hook"] == "hook 1" and meta["target_minutes"] == 7


def test_next_recap_prompt_carries_previous_hook(tmp_path):
    cfg, b = _transcripts(tmp_path)
    b.prepare(["1"], 6)
    _answer(b, ["1"])
    b.finalize()
    assert "hook 1" in b._prompt_for("2", 6)


def test_compile_selection(tmp_path):
    cfg, b = _transcripts(tmp_path, ("1", "2", "3", "4", "11"))
    b.prepare(["1", "2", "3", "4", "11"], 6)
    _answer(b, ["1", "2", "3", "4", "11"])
    b.finalize()

    out = b.compile(["11", "1", "2", "3", "4"])
    text = out.read_text(encoding="utf-8")
    assert out.name == "compiled_1-4_11.md"
    assert text.index("## Chapter 1") < text.index("## Chapter 11")
    assert "## Chapter 5" not in text
    assert "| **Total** | **4,500** | **30.0** |" in text


def test_compile_includes_bookends_when_written(tmp_path):
    cfg, b = _transcripts(tmp_path, ("1", "2"))
    b.prepare(["1", "2"], 6)
    _answer(b, ["1", "2"], extra={"_intro": "Welcome in.", "_outro": "See you next time."})
    b.finalize()
    text = b.compile(["1", "2"]).read_text(encoding="utf-8")
    assert "## Intro" in text and "## Outro" in text


def test_compile_names_missing_recaps(tmp_path):
    cfg, b = _transcripts(tmp_path)
    with pytest.raises(UserError, match="recap --chapters 2"):
        b.compile(["2"])


def test_compile_custom_name_is_sanitised(tmp_path):
    cfg, b = _transcripts(tmp_path, ("1",))
    b.prepare(["1"], 6)
    _answer(b, ["1"])
    b.finalize()
    assert b.compile(["1"], name="../../evil name").parent == b.recaps_dir


def test_finalize_tolerates_bare_string_parts(tmp_path):
    cfg, b = _transcripts(tmp_path, ("1",))
    b.prepare(["1"], 6)
    text = "just the narration " * 30
    (b.task_dir / "parts.json").write_text(json.dumps({"1": text}), encoding="utf-8")
    assert "just the narration" in b.finalize()[0].read_text(encoding="utf-8")


def test_next_reports_unfinished_recap_task_first(tmp_path):
    store = SessionStore(tmp_path)
    s = store.create("A")
    s.add_links(["https://x.com/1"])
    (s.path / "chapter_1.md").write_text("# x\n\nbody\n")
    RecapBuilder(
        load_config(series__workdir=str(s.path), series__name="A"), log=lambda *_: None
    ).prepare(["1"], 6)
    assert "recap task" in next_steps(s)[0].lower()


# =============================================================== CLI


def test_cli_recap_and_compile_by_session_name(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MANHWA_SESSIONS_ROOT", str(tmp_path))
    assert cli_main(["start", "My Series", "https://x.com/1", "https://x.com/2"]) == 0
    sess = SessionStore(tmp_path).get("My Series")
    for c in ("1", "2"):
        (sess.path / f"chapter_{c}.md").write_text(f"# {c}\n\nbody\n")

    assert cli_main(["recap", "--session", "My Series", "--chapters", "1-2", "--prepare"]) == 0
    _answer(RecapBuilder(load_config(series__workdir=str(sess.path)), log=lambda *_: None), ["1", "2"])
    assert cli_main(["recap", "--session", "My Series", "--finalize"]) == 0
    assert sess.recaps() == ["1", "2"]

    assert cli_main(["compile", "--session", "My Series", "--chapters", "1-2"]) == 0
    assert (sess.recaps_dir / "compiled_1-2.md").exists()


def test_cli_import_existing_transcripts(tmp_path, monkeypatch):
    monkeypatch.setenv("MANHWA_SESSIONS_ROOT", str(tmp_path / "sessions"))
    src = tmp_path / "old"
    src.mkdir()
    (src / "chapter_1_ocr.md").write_text("one")
    (src / "chapter_2_ocr.md").write_text("two")
    (src / "notes.md").write_text("ignored")

    cli_main(["start", "Old Series"])
    assert cli_main(["import", str(src)]) == 0
    sess = SessionStore(tmp_path / "sessions").get("Old Series")
    assert sess.chapters() == ["1", "2"]


# =============================================================== dashboard


@pytest.fixture()
def dashboard(tmp_path):
    from manhwa_ocr.ui.server import make_handler

    store = SessionStore(tmp_path)
    s = store.create("Dash Series")
    s.add_links(["https://x.com/1", "https://x.com/2"])
    (s.path / "chapter_1.md").write_text("# Chapter 1\n\n**A:** hi\n", encoding="utf-8")
    s.recaps_dir.mkdir()
    s.recap_path("1").write_text("# recap\n\nnarration here\n", encoding="utf-8")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, lambda *_: None))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", s
    httpd.shutdown()
    httpd.server_close()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read()


def _post(url, body, headers=None):
    h = {"Content-Type": "application/json", "X-Manhwa-OCR": "1"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_dashboard_serves_page_and_lists_sessions(dashboard):
    base, _ = dashboard
    status, body = _get(base + "/")
    assert status == 200 and b"Manhwa OCR" in body
    data = json.loads(_get(base + "/api/sessions")[1])
    assert data["sessions"][0]["slug"] == "dash-series"


def test_dashboard_session_detail(dashboard):
    base, _ = dashboard
    d = json.loads(_get(base + "/api/session/dash-series")[1])
    by_ch = {c["chapter"]: c for c in d["chapters"]}
    assert by_ch["1"]["transcribed"] and by_ch["1"]["recapped"]
    assert not by_ch["2"]["transcribed"]
    assert d["next"]  # always has guidance


def test_dashboard_reads_documents(dashboard):
    base, _ = dashboard
    doc = json.loads(_get(base + "/api/session/dash-series/doc?type=transcript&id=1")[1])
    assert "Chapter 1" in doc["text"]
    doc = json.loads(_get(base + "/api/session/dash-series/doc?type=recap&id=1")[1])
    assert "narration here" in doc["text"]


@pytest.mark.parametrize(
    "query",
    [
        "type=transcript&id=../../etc/passwd",
        "type=transcript&id=..%2F..%2Fsession",
        "type=compiled&id=../secret",
        "type=bogus&id=1",
    ],
)
def test_dashboard_blocks_path_traversal(dashboard, query):
    base, _ = dashboard
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base + "/api/session/dash-series/doc?" + query)
    assert exc.value.code in (400, 404)


def test_dashboard_rejects_foreign_host(dashboard):
    base, _ = dashboard
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base + "/api/sessions", headers={"Host": "evil.example.com"})
    assert exc.value.code == 403


def test_dashboard_write_needs_header(dashboard):
    base, _ = dashboard
    req = urllib.request.Request(
        base + "/api/session/dash-series/links",
        data=b'{"urls": ["https://x.com/3"]}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 403


def test_dashboard_adds_links_and_skips_duplicates(dashboard):
    base, s = dashboard
    status, r = _post(
        base + "/api/session/dash-series/links",
        {"urls": "https://x.com/2\nhttps://x.com/3  not-a-url  https://x.com/4"},
    )
    assert status == 200 and r["added"] == 2 and r["duplicates"] == 1
    assert dict(s.read_links())["4"] == "https://x.com/4"


def test_dashboard_creates_series(dashboard):
    base, _ = dashboard
    status, r = _post(base + "/api/sessions", {"name": "Brand New", "urls": "https://y.com/1"})
    assert status == 201 and r["slug"] == "brand-new" and r["added"] == 1


def test_session_detail_includes_imported_chapters_without_links(tmp_path):
    s = SessionStore(tmp_path).create("A")
    (s.path / "chapter_7.md").write_text("imported")
    d = session_detail(s, None)
    assert [c["chapter"] for c in d["chapters"]] == ["7"]
