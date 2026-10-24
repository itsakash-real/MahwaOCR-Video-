"""PR3: transcript/recap fixes — shards, vision batching, nearest-drawn hint,
vectorized gutter search, PNG fast paths, parallel downloads/slicing."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr import workspace as w  # noqa: E402
from manhwa_ocr.agentflow import AgentFlow  # noqa: E402
from manhwa_ocr.config import load_config  # noqa: E402
from manhwa_ocr.images import (  # noqa: E402
    Tile,
    _find_gutter,
    _row_detail,
    _row_details,
    build_tiles,
    download_images,
    import_pages,
    slice_tall_image,
)
from manhwa_ocr.pipeline import Pipeline  # noqa: E402
from manhwa_ocr.script import _panel_listing  # noqa: E402
from manhwa_ocr.video.cues import nearest_drawn_panel  # noqa: E402


# --------------------------------------------------------------- shards


def _prepared(tmp_path: Path, pages: int = 2, shards: int = 1):
    src = tmp_path / "pages"
    src.mkdir()
    for i in range(pages):
        Image.new("RGB", (900, 4000), "white").save(src / f"{i + 1}.png")
    ws = tmp_path / "ws"
    flow = AgentFlow(load_config(series__workdir=str(ws), series__name="T"), log=lambda *_: None)
    flow.prepare(chapter="1", folder=src, shards=shards)
    work = w.chapter_work_dir(ws, "1")
    manifest = json.loads((work / "manifest.json").read_text())
    return flow, ws, work, manifest


def test_prepare_shards_writes_shard_tasks_and_index(tmp_path):
    flow, ws, work, manifest = _prepared(tmp_path, pages=2, shards=2)
    tiles = manifest["tiles"]
    assert len(tiles) > 2
    assert manifest["shards"]["count"] == 2
    # Each shard task lists only its own tiles and names its own results file.
    for k in (1, 2):
        task = (work / f"TASK_{k}.md").read_text()
        assert f"tile_results_shard_{k}.json" in task
    # Shard 1's tiles are the first chunk, shard 2's the second chunk.
    # (Check the ## Tiles listing only: the JSON example mentions p001_t01.)
    chunk = (len(tiles) + 1) // 2

    def _listing(task_text: str) -> str:
        return task_text.split("## Tiles")[1].split("## Output")[0]

    t1 = _listing((work / "TASK_1.md").read_text())
    t2 = _listing((work / "TASK_2.md").read_text())
    assert tiles[0]["id"] in t1 and tiles[chunk]["id"] not in t1
    assert tiles[chunk]["id"] in t2 and tiles[0]["id"] not in t2
    # TASK.md is the index pointing at the shards.
    index = (work / "TASK.md").read_text()
    assert "TASK_1.md" in index and "TASK_2.md" in index


def test_prepare_shards_clamps_to_tile_count(tmp_path):
    _, _, work, manifest = _prepared(tmp_path, pages=1, shards=99)
    # 1 page of 4000px -> 3 tiles (4000 / 1400 target, with overlap)
    assert manifest["shards"]["count"] == len(manifest["tiles"])
    assert manifest["shards"]["count"] < 99


def test_prepare_auto_shards_computed_from_tile_bound(tmp_path):
    # shards=None (the CLI default) computes ceil(tiles / agent_shard_tiles),
    # so no reader is ever handed an unbounded chapter of images.
    src = tmp_path / "pages"
    src.mkdir()
    for i in range(2):
        Image.new("RGB", (900, 4000), "white").save(src / f"{i + 1}.png")
    ws = tmp_path / "ws"
    flow = AgentFlow(load_config(series__workdir=str(ws), series__name="T",
                                 pipeline__agent_shard_tiles=2), log=lambda *_: None)
    flow.prepare(chapter="1", folder=src)  # no shards= -> auto
    work = w.chapter_work_dir(ws, "1")
    manifest = json.loads((work / "manifest.json").read_text())
    n_tiles = len(manifest["tiles"])
    assert n_tiles >= 4
    expected = -(-n_tiles // 2)
    assert manifest["shards"]["count"] == expected
    for k in range(1, expected + 1):
        assert (work / f"TASK_{k}.md").exists()
    index = (work / "TASK.md").read_text()
    assert "PARALLEL" in index
    assert "tile_results_shard_1.json" in index


def test_prepare_auto_shards_single_for_small_chapters(tmp_path):
    # A chapter that fits one shard keeps the plain TASK.md workflow.
    _, _, work, manifest = _prepared(tmp_path, pages=1)  # default bound 20 -> 1 shard
    assert manifest["shards"]["count"] == 1
    assert (work / "TASK.md").exists()
    assert not (work / "TASK_1.md").exists()
    task = (work / "TASK.md").read_text()
    assert "Write results incrementally" in task or "incrementally" in task


def test_assemble_merges_shard_files(tmp_path):
    flow, ws, work, manifest = _prepared(tmp_path, pages=2, shards=2)
    tiles = manifest["tiles"]
    half = len(tiles) // 2
    (work / "tile_results_shard_1.json").write_text(json.dumps([
        {"id": t["id"], "scene": "first half", "blocks": [{"type": "dialogue", "speaker": "A", "text": "alpha line"}]}
        for t in tiles[:half]
    ]))
    (work / "tile_results_shard_2.json").write_text(json.dumps([
        {"id": t["id"], "scene": "second half", "blocks": [{"type": "dialogue", "speaker": "B", "text": "beta line"}]}
        for t in tiles[half:]
    ]))
    assert flow.assemble(chapter="1").exists()
    raw = (work / "raw_blocks.txt").read_text()
    assert "alpha line" in raw and "beta line" in raw


def test_tile_progress_counts_shard_files(tmp_path):
    # A sharded chapter writes ONLY tile_results_shard_N.json; progress tools
    # must see them, or the chapter can never leave the "reading" stage.
    flow, ws, work, manifest = _prepared(tmp_path, pages=2, shards=2)
    tiles = manifest["tiles"]
    half = len(tiles) // 2
    (work / "tile_results_shard_1.json").write_text(json.dumps(
        [{"id": t["id"], "blocks": []} for t in tiles[:half]]))
    got, total, parses = w.tile_progress(work)
    assert parses and total == len(tiles) and got == half
    assert w.chapter_stage(ws, "1") == w.STAGE_READ_TILES
    (work / "tile_results_shard_2.json").write_text(json.dumps(
        [{"id": t["id"], "blocks": []} for t in tiles[half:]]))
    got, total, parses = w.tile_progress(work)
    assert parses and got == total
    assert w.chapter_stage(ws, "1") == w.STAGE_ASSEMBLE


def test_read_tiles_instruction_is_shard_aware_and_watches_staleness(tmp_path):
    flow, ws, work, manifest = _prepared(tmp_path, pages=2, shards=2)
    tiles = manifest["tiles"]
    (work / "tile_results_shard_1.json").write_text(json.dumps(
        [{"id": t["id"], "blocks": []} for t in tiles[: len(tiles) // 2]]))
    text = w.stage_instruction(w.STAGE_READ_TILES, "1", ws, stale_minutes=None)
    assert "shard" in text and "parallel" in text and "TASK.md" in text
    assert "STALLED" not in text
    # Fresh mtime: not stale even with a threshold.
    text = w.stage_instruction(w.STAGE_READ_TILES, "1", ws, stale_minutes=5)
    assert "STALLED" not in text
    # 0.001 min = 0.06s threshold; sleep past it -> stale.
    import time as _t
    _t.sleep(0.1)
    text = w.stage_instruction(w.STAGE_READ_TILES, "1", ws, stale_minutes=0.001)
    assert "STALLED" in text and "kept" in text


# ------------------------------------------------- provider stall watchdog


class _SleepyProvider:
    """A provider whose calls go quiet (no completion, no exception)."""

    def __init__(self, seconds: float = 1.5):
        self.seconds = seconds

    def fingerprint(self) -> str:
        return "sleepy"

    def vision(self, system, prompt, images):
        import time
        time.sleep(self.seconds)
        return '{"scene": "", "blocks": []}'

    def text(self, system, prompt):
        return '{"transcript": "ok"}'


def test_stage1_stall_watchdog_fails_loudly(tmp_path):
    import time as _t

    from manhwa_ocr.util import UserError

    provider = _SleepyProvider(seconds=1.5)
    pipe = _pipeline(tmp_path, provider, vision_batch=1)
    pipe.cfg.pipeline["stall_minutes"] = 0.01  # 0.6s without a completion -> hang
    tiles = _tiles(tmp_path, 3)
    t0 = _t.perf_counter()
    with pytest.raises(UserError) as exc:
        pipe._read_tiles(tiles, briefing="", chapter_label="chapter 1")
    elapsed = _t.perf_counter() - t0
    assert "no progress" in str(exc.value)
    assert "cached" in str(exc.value)
    assert elapsed < 5  # it failed fast instead of waiting out the sleep


def test_stage1_watchdog_lets_healthy_reads_finish(tmp_path):
    provider = _SleepyProvider(seconds=0)
    pipe = _pipeline(tmp_path, provider, vision_batch=1)
    pipe.cfg.pipeline["stall_minutes"] = 0.01
    tiles = _tiles(tmp_path, 3)
    out, unresolved = pipe._read_tiles(tiles, briefing="", chapter_label="chapter 1")
    assert len(out) == 3
    assert all("error" not in r for r in out)


# ------------------------------------------------- landscape credits pages


def test_build_tiles_skips_landscape_banner_pages(tmp_path):
    src = tmp_path / "pages"
    src.mkdir()
    Image.new("RGB", (1532, 1024), "white").save(src / "banner.png")   # Asura-style credits
    Image.new("RGB", (900, 4000), "white").save(src / "strip.png")     # real webtoon page
    pages = import_pages(sorted(src.glob("*.png")), tmp_path / "out")
    tiles = build_tiles(pages, tmp_path / "tiles")
    assert tiles, "the portrait strip must still produce tiles"
    assert all(t.page_index != 1 for t in tiles), "the landscape banner (page 1) must be skipped"
    # All tiles come from the strip (page 2).
    assert {t.page_index for t in tiles} == {2}


def test_build_tiles_keeps_all_landscape_when_that_is_the_whole_chapter(tmp_path):
    src = tmp_path / "pages"
    src.mkdir()
    Image.new("RGB", (1400, 900), "white").save(src / "1.png")
    Image.new("RGB", (1500, 800), "white").save(src / "2.png")
    pages = import_pages(sorted(src.glob("*.png")), tmp_path / "out")
    tiles = build_tiles(pages, tmp_path / "tiles")
    assert len(tiles) == 2  # nothing dropped: an all-landscape source isn't banners


# ------------------------------------------------- per-chapter timing report


def test_finalize_writes_per_chapter_timing_report(tmp_path):
    import time as _t

    from manhwa_ocr.timing import read as timings_read  # noqa: F401 - clarity only

    flow, ws, work, manifest = _prepared(tmp_path, pages=1)  # 3 tiles, 1 shard
    ids = [t["id"] for t in manifest["tiles"]]
    _t.sleep(0.05)
    (work / "tile_results.json").write_text(
        json.dumps([{"id": i, "scene": "s", "blocks": []} for i in ids]))
    assert flow.assemble(chapter="1").exists()
    _t.sleep(0.05)
    (work / "assembled.json").write_text(json.dumps(
        {"transcript": "## Scene 1\n\n**A:** \"hello there friend\"\n" * 3,
         "summary": "things happen", "memory_update": {}}))
    logs: list = []
    flow.log = logs.append
    out = flow.finalize(chapter="1")
    assert out.exists()
    report_file = ws / ".work" / "timing" / "chapter_1.json"
    assert report_file.exists(), "the report must survive the chapter scratch cleanup"
    report = json.loads(report_file.read_text())
    stages = report["stages_s"]
    assert stages["prepare"] is not None and stages["prepare"] >= 0
    assert stages["read_tiles"] is not None and stages["read_tiles"] >= 0
    assert stages["assemble"] is not None and stages["assemble"] >= 0
    assert stages["answer_assembly"] is not None and stages["answer_assembly"] >= 0
    assert stages["finalize"] is not None and stages["finalize"] >= 0
    assert stages["total"] is not None and stages["total"] >= 0
    assert report["tiles"]["total"] == len(ids) and report["tiles"]["read"] == len(ids)
    assert any("timing report" in str(x) for x in logs)
    assert any("per-stage timing" in str(x) for x in logs)


def test_assemble_shard_entry_wins_over_single_file(tmp_path):
    flow, ws, work, manifest = _prepared(tmp_path, pages=2, shards=2)
    tiles = manifest["tiles"]
    # tile_results.json has a stale entry for every tile; the shard file wins for tile 0.
    (work / "tile_results.json").write_text(json.dumps([
        {"id": t["id"], "scene": "stale", "blocks": [{"type": "dialogue", "speaker": "A", "text": f"stale line {i}"}]}
        for i, t in enumerate(tiles)
    ]))
    (work / "tile_results_shard_1.json").write_text(json.dumps([
        {"id": tiles[0]["id"], "scene": "fresh", "blocks": [{"type": "dialogue", "speaker": "A", "text": "fresh line"}]}
    ]))
    flow.assemble(chapter="1")
    raw = (work / "raw_blocks.txt").read_text()
    assert "fresh line" in raw
    assert "stale line 0" not in raw  # overridden by the shard file
    assert "stale line 1" in raw  # other tiles still come from tile_results.json


def test_reprepare_with_changed_tiles_sets_shard_results_aside(tmp_path):
    flow, ws, work, manifest = _prepared(tmp_path, pages=2, shards=2)
    (work / "tile_results_shard_1.json").write_text('[{"id": "x", "blocks": []}]')
    more = tmp_path / "more"
    more.mkdir()
    for i in range(3):
        Image.new("RGB", (900, 1000), "white").save(more / f"{i + 1}.png")
    flow.prepare(chapter="1", folder=more, shards=2)
    assert not (work / "tile_results_shard_1.json").exists()
    assert (work / "tile_results_shard_1.json.stale").exists()


# --------------------------------------------------------------- vision batching


class _StubProvider:
    """Returns one entry per image; `batch_mode` controls batch behaviour."""

    def __init__(self, batch_mode: str = "array"):
        self.batch_mode = batch_mode
        self.calls: list[int] = []

    def fingerprint(self) -> str:
        return "stub"

    def vision(self, system, prompt, images):
        self.calls.append(len(images))
        if len(images) > 1 and self.batch_mode == "garbage":
            return "not json at all"
        if len(images) > 1:
            return json.dumps([
                {"scene": f"scene {p.name}", "blocks": [{"type": "dialogue", "speaker": "A", "text": f"line {p.name}"}]}
                for p in images
            ])
        return json.dumps({"scene": f"scene {images[0].name}", "blocks": []})


def _pipeline(tmp_path: Path, provider, vision_batch: int) -> Pipeline:
    import manhwa_ocr.pipeline as pipeline_mod

    cfg = load_config(series__workdir=str(tmp_path / "ws"), series__name="T")
    cfg.pipeline["vision_batch"] = vision_batch
    cfg.pipeline["concurrency"] = 2
    old = pipeline_mod.build_provider
    pipeline_mod.build_provider = lambda _cfg: provider
    try:
        return Pipeline(cfg, log=lambda *_: None)
    finally:
        pipeline_mod.build_provider = old


def _tiles(tmp_path: Path, n: int):
    out = []
    for i in range(n):
        p = tmp_path / f"tile{i}.png"
        # Distinct content per tile: identical images share a cache key.
        Image.new("RGB", (100, 100), ((i * 40) % 256, 100, 150)).save(p)
        out.append(Tile(1, i + 1, n, p, 0, 100, 100))
    return out


def test_vision_batch_sends_several_tiles_per_call(tmp_path):
    provider = _StubProvider()
    pipe = _pipeline(tmp_path, provider, vision_batch=2)
    results, _ = pipe._read_tiles(_tiles(tmp_path, 3), briefing="", chapter_label="chapter 1")
    # Two batches (2 + 1); the two groups run concurrently, so call order is not fixed.
    assert sorted(provider.calls) == [1, 2]
    assert [r["scene"] for r in results] == ["scene tile0.png", "scene tile1.png", "scene tile2.png"]
    # Each tile is cached separately: a second read makes no provider calls.
    provider.calls.clear()
    pipe._read_tiles(_tiles(tmp_path, 3), briefing="", chapter_label="chapter 1")
    assert provider.calls == []


def test_vision_batch_falls_back_to_one_call_per_tile(tmp_path):
    provider = _StubProvider(batch_mode="garbage")
    pipe = _pipeline(tmp_path, provider, vision_batch=2)
    results, _ = pipe._read_tiles(_tiles(tmp_path, 2), briefing="", chapter_label="chapter 1")
    assert provider.calls == [2, 1, 1]  # batch failed, then one call per tile
    assert len(results) == 2 and all("scene" in r for r in results)


def test_vision_batch_one_is_the_old_behaviour(tmp_path):
    provider = _StubProvider()
    pipe = _pipeline(tmp_path, provider, vision_batch=1)
    pipe._read_tiles(_tiles(tmp_path, 3), briefing="", chapter_label="chapter 1")
    assert provider.calls == [1, 1, 1]


# --------------------------------------------------------------- nearest-drawn hint


def test_panel_listing_names_nearest_drawn_panel():
    panels = [
        {"id": "P001-01", "scene": "a garden", "text": ""},
        {"id": "P001-02", "text_only": True, "text": "SFX: boom"},
        {"id": "P001-03", "scene": "a tower", "text": ""},
    ]
    listing = _panel_listing(panels)
    # The text-only panel is adjacent to two drawn panels; the tie goes to the later one.
    assert "tag {P001-03} instead" in listing
    assert "P001-01" in listing  # drawn panels still listed normally


def test_panel_listing_without_drawn_panels_keeps_generic_hint():
    panels = [{"id": "P001-01", "text_only": True, "text": "SFX: boom"}]
    assert "tag the drawn panel next to it" in _panel_listing(panels)


def test_nearest_drawn_panel_edge_cases():
    assert nearest_drawn_panel([], "P001-01") is None
    assert nearest_drawn_panel([{"id": "P001-01", "text_only": True}], "P001-01") is None
    assert nearest_drawn_panel([{"id": "P001-01"}], "P999-99") is None
    panels = [{"id": "P001-01"}, {"id": "P001-02", "text_only": True}, {"id": "P001-03"}]
    assert nearest_drawn_panel(panels, "P001-02") == "P001-03"  # tie -> later


# --------------------------------------------------------------- gutter vectorization


def test_row_details_match_row_detail():
    rng = np.random.default_rng(7)
    img = Image.fromarray(rng.integers(0, 256, size=(200, 120, 3), dtype=np.uint8), "RGB")
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    detail = _row_details(gray)
    for y in (0, 37, 119):
        assert detail[y] == pytest.approx(_row_detail(img, y, 120), abs=1e-3)


def test_find_gutter_with_precomputed_detail_matches():
    rng = np.random.default_rng(3)
    arr = rng.integers(0, 256, size=(400, 300, 3), dtype=np.uint8)
    arr[150:200, :, :] = 255  # a flat white band: the true gutter
    img = Image.fromarray(arr, "RGB")
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    detail = _row_details(gray)
    assert _find_gutter(img, 175, 60, 300, detail) == _find_gutter(img, 175, 60, 300)
    assert 150 <= _find_gutter(img, 175, 60, 300, detail) < 200


# --------------------------------------------------------------- PNG fast paths


def test_import_pages_copies_png_bytes(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    # A PNG with a text chunk: re-encoding would drop it, copying keeps it.
    img = Image.new("RGB", (50, 50), "red")
    img.save(src / "a.png", pnginfo=_png_text_chunk())
    out = import_pages([src / "a.png"], tmp_path / "pages")
    assert out[0].read_bytes() == (src / "a.png").read_bytes()


def _png_text_chunk():
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Comment", "keep me")
    return info


def test_import_pages_still_converts_jpeg(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    Image.new("RGB", (50, 50), "blue").save(src / "a.jpg")
    out = import_pages([src / "a.jpg"], tmp_path / "pages")
    assert out[0].suffix == ".png"
    with Image.open(out[0]) as im:
        assert im.format == "PNG"


def test_download_images_stores_png_bytes_and_runs_in_order(tmp_path, monkeypatch):
    png = io.BytesIO()
    Image.new("RGB", (60, 40), "green").save(png, format="PNG")
    png_bytes = png.getvalue()
    jpg = io.BytesIO()
    Image.new("RGB", (30, 20), "yellow").save(jpg, format="JPEG")
    jpg_bytes = jpg.getvalue()

    class _Resp:
        def __init__(self, content, ctype):
            self.content = content
            self.headers = {"Content-Type": ctype}

        def raise_for_status(self):
            pass

    def _get(url, headers=None, timeout=None):
        if "a" in url:
            return _Resp(png_bytes, "image/png")
        return _Resp(jpg_bytes, "image/jpeg")

    import manhwa_ocr.images as images_mod

    monkeypatch.setattr(images_mod.requests, "get", _get)
    out = download_images(
        ["http://x/a.png", "http://x/b.jpg"], tmp_path / "pages",
        config={"delay": 0}, log=lambda *_: None,
    )
    assert out[0].read_bytes() == png_bytes  # PNG stored as-is, not re-encoded
    with Image.open(out[1]) as im:
        assert im.format == "PNG"  # JPEG re-encoded to PNG
        assert im.size == (30, 20)


# --------------------------------------------------------------- parallel slicing


def test_build_tiles_keeps_page_order(tmp_path):
    pages = []
    for i in range(3):
        p = tmp_path / f"p{i}.png"
        Image.new("RGB", (900, 4000), "white").save(p)
        pages.append(p)
    tiles = build_tiles(pages, tmp_path / "tiles", config={}, log=lambda *_: None)
    assert [t.page_index for t in tiles] == sorted(t.page_index for t in tiles)
    assert len({t.page_index for t in tiles}) == 3
    # Slicing still produces the same tiles as the single-page path.
    single = slice_tall_image(pages[0], 1, tmp_path / "single", config={})
    assert len([t for t in tiles if t.page_index == 1]) == len(single)
