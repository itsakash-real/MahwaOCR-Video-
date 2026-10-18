"""Provider mode: URL or folder -> tiles -> vision pass -> assembly -> memory.

Two stages, deliberately:

  Stage 1 (vision, expensive)  one call per tile, extracting structured blocks.
  Stage 2 (text, cheap)        one call per chapter, merging tiles into prose and
                               proposing memory updates.

Stage 1 results are cached per image, so re-running a chapter after editing the
assembly prompt costs almost nothing. Failed responses are never cached, so a
retry really retries.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List

from .config import Config
from .images import Tile, build_tiles, download_images, image_hash, import_pages, list_images
from .memory import SeriesMemory
from .merge import flatten
from .prompts import CHAPTER_SYSTEM, PROMPT_VERSION, TILE_SYSTEM, render_chapter_prompt, render_tile_prompt
from .providers import Provider, build_provider
from .scrapers import discover_pages
from .util import UserError, atomic_write_json, atomic_write_text, extract_json, require_chapter
from .workspace import chapter_work_dir, clean_chapter


# Batching wrapper for stage 1. NOT in prompts.py, deliberately: it reuses the
# tile prompt's instructions for several images at once, it is not a change to
# the tile prompt itself, and PROMPT_VERSION (hence every cached tile result)
# stays valid. Each tile is still cached separately under its own key.
BATCH_TILE_PROMPT = """{briefing_block}
These are {count} slices of "{series}", {chapter_label}, in the order listed below.

{labels}

Transcribe every piece of text in each slice, in top-to-bottom reading order.

For each text element produce one block:
  type     - "dialogue" | "thought" | "narration" | "sfx" | "sign" | "title" | "label"
  speaker  - who is saying it. Use a name from KNOWN CHARACTERS when you can identify them by appearance. Use a short descriptor like "silver-haired woman" when you cannot. Use null for narration, signs and titles.
  text     - the text exactly as written
  truncated - true if the text runs off the edge of this slice

Also describe what is happening visually in each slice, in one or two sentences, under "scene". Mention which characters are present and what they are doing.

If a slice has no text at all, return an empty blocks array and still fill in "scene".

Return a JSON array with exactly one object per slice, in the same order as the images:
[
  {{
    "scene": "...",
    "blocks": [
      {{"type": "dialogue", "speaker": "Name or null", "text": "...", "truncated": false}}
    ]
  }}
]"""


@dataclass
class ChapterResult:
    chapter: str
    transcript: str
    summary: str
    pages: int
    tiles: int
    output_path: Path
    memory_notes: List[str]


class Pipeline:
    def __init__(self, config: Config, log: Callable[[str], None] = print):
        self.cfg = config
        self.log = log
        self.provider: Provider = build_provider(config.provider)
        self.workdir = config.workdir
        self.memory_path = self.workdir / "series_memory.json"
        self.memory = SeriesMemory.load(self.memory_path, config.series_name)
        self.cache_dir = self.workdir / ".cache"

    # ---------------- caching ----------------

    def _cache_key(self, tile: Tile, briefing: str) -> str:
        h = hashlib.sha256()
        h.update(image_hash(tile.path).encode())
        h.update(self.provider.fingerprint().encode())
        h.update(PROMPT_VERSION.encode())
        # Briefing changes attribution, so it belongs in the key.
        h.update(hashlib.sha256(briefing.encode()).hexdigest()[:16].encode())
        return h.hexdigest()[:32]

    def _cache_get(self, key: str) -> Dict[str, Any] | None:
        if not self.cfg.pipeline.get("cache", True):
            return None
        p = self.cache_dir / f"{key}.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and "error" not in data else None

    def _cache_put(self, key: str, value: Dict[str, Any]) -> None:
        if self.cfg.pipeline.get("cache", True) and "error" not in value:
            atomic_write_json(self.cache_dir / f"{key}.json", value)

    # ---------------- stage 1 ----------------

    def _read_tile(self, tile: Tile, briefing: str, chapter_label: str) -> Dict[str, Any]:
        key = self._cache_key(tile, briefing)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        prompt = render_tile_prompt(
            briefing=briefing, label=tile.label, series=self.cfg.series_name, chapter_label=chapter_label
        )
        raw = self.provider.vision(TILE_SYSTEM, prompt, [tile.path])
        try:
            parsed = extract_json(raw)
            if isinstance(parsed, list):
                parsed = {"scene": "", "blocks": parsed}
            if not isinstance(parsed, dict):
                raise ValueError("expected a JSON object")
            result = {"scene": str(parsed.get("scene") or ""), "blocks": parsed.get("blocks") or []}
        except ValueError as exc:
            self.log(f"    ! {tile.label}: unparseable response ({exc}); will retry next run")
            result = {"scene": "", "blocks": [], "error": str(exc)}

        self._cache_put(key, result)
        return result

    def _read_tile_batch(self, tiles: List[Tile], briefing: str, chapter_label: str) -> List[Dict[str, Any]]:
        """Read several tiles in one vision() call; each tile is cached separately.

        Falls back to one call per tile if the model doesn't return exactly
        one entry per image (the batch prompt asks for a JSON array in order).
        """
        results: List[Dict[str, Any] | None] = [None] * len(tiles)
        misses: List[int] = []
        for i, tile in enumerate(tiles):
            cached = self._cache_get(self._cache_key(tile, briefing))
            if cached is not None:
                results[i] = cached
            else:
                misses.append(i)
        if not misses:
            return [r for r in results if r is not None]

        batch_tiles = [tiles[i] for i in misses]
        block = f"SERIES CONTEXT\n{briefing}\n\n---\n\n" if briefing.strip() else ""
        labels = "\n".join(f"Image {j + 1}: {t.label}" for j, t in enumerate(batch_tiles))
        prompt = BATCH_TILE_PROMPT.format(
            briefing_block=block, count=len(batch_tiles), labels=labels,
            series=self.cfg.series_name, chapter_label=chapter_label,
        )
        raw = self.provider.vision(TILE_SYSTEM, prompt, [t.path for t in batch_tiles])
        parsed: Any = None
        try:
            parsed = extract_json(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            # A single-tile object ({"scene", "blocks"}) is a valid answer
            # only for a one-tile batch; a {"tiles": [...]} wrapper unwraps.
            if "scene" in parsed or "blocks" in parsed:
                parsed = [parsed] if len(batch_tiles) == 1 else None
            else:
                lists = [v for v in parsed.values() if isinstance(v, list)]
                parsed = lists[0] if len(lists) == 1 else None
        if not isinstance(parsed, list) or len(parsed) != len(batch_tiles):
            parsed = None

        if parsed is None:
            # The model didn't return a clean per-image array; retry one tile at a time.
            self.log(f"    ! batch of {len(batch_tiles)} tiles returned an unusable response; retrying one at a time")
            for i, tile in zip(misses, batch_tiles):
                results[i] = self._read_tile(tile, briefing, chapter_label)
            return [r for r in results if r is not None]

        for i, entry in zip(misses, parsed):
            tile = tiles[i]
            if isinstance(entry, dict):
                res = {"scene": str(entry.get("scene") or ""), "blocks": entry.get("blocks") or []}
            else:
                res = {"scene": "", "blocks": [], "error": "batch entry was not an object"}
            if "error" not in res:
                self._cache_put(self._cache_key(tile, briefing), res)
            results[i] = res
        return [r for r in results if r is not None]

    def _read_tiles(self, tiles: List[Tile], briefing: str, chapter_label: str) -> List[Dict]:
        workers = max(1, int(self.cfg.pipeline.get("concurrency", 4)))
        batch = max(1, int(self.cfg.pipeline.get("vision_batch", 1)))
        results: List[Dict[str, Any]] = [{} for _ in tiles]
        counter = {"done": 0}

        if batch <= 1:
            def run(i: int, tile: Tile):
                results[i] = self._read_tile(tile, briefing, chapter_label)
                counter["done"] += 1
                self.log(f"    [{counter['done']}/{len(tiles)}] {tile.label}")

            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(lambda p: run(*p), enumerate(tiles)))
            return results

        groups = [list(enumerate(tiles[s:s + batch], start=s)) for s in range(0, len(tiles), batch)]

        def run_group(group: List[tuple[int, Tile]]):
            out = self._read_tile_batch([t for _, t in group], briefing, chapter_label)
            for (i, _), res in zip(group, out):
                results[i] = res
            counter["done"] += len(group)
            self.log(f"    [{counter['done']}/{len(tiles)}] batch of {len(group)} tiles")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run_group, groups))
        return results

    # ---------------- stage 2 ----------------

    def _assemble(self, raw_blocks: str, briefing: str, chapter_label: str) -> Dict[str, Any]:
        prompt = render_chapter_prompt(
            briefing=briefing, series=self.cfg.series_name, chapter_label=chapter_label, raw_blocks=raw_blocks
        )
        raw = self.provider.text(CHAPTER_SYSTEM, prompt)
        try:
            parsed = extract_json(raw)
        except ValueError as exc:
            parsed = None
            reason = str(exc)
        else:
            reason = "no transcript in response"
        if not isinstance(parsed, dict) or len(str(parsed.get("transcript") or "").strip()) < 20:
            raise UserError(
                f"The model's assembly response couldn't be used ({reason}). Nothing was saved, "
                "so the chapter stays pending. Re-run to try again; tile reads are cached. "
                "If it keeps failing, raise provider.text_max_tokens in manhwa-ocr.yaml."
            )
        return parsed

    # ---------------- entry point ----------------

    def run_chapter(
        self,
        *,
        chapter: str,
        url: str | None = None,
        folder: Path | None = None,
        force: bool = False,
        keep_work: bool = False,
    ) -> ChapterResult:
        chapter = require_chapter(chapter)
        label = f"chapter {chapter}"
        chapter_dir = chapter_work_dir(self.workdir, chapter)
        out_path = self.workdir / f"chapter_{chapter}.md"

        if out_path.exists() and out_path.stat().st_size > 0 and not force:
            self.log(f"  {out_path.name} already exists. Use --force to redo it.")
            return ChapterResult(chapter, out_path.read_text(encoding="utf-8"), "", 0, 0, out_path, [])

        if folder:
            found = list_images(Path(folder))
            if not found:
                raise UserError(f"No .png/.jpg/.webp images found in {folder}")
            pages = import_pages(found, chapter_dir / "pages", log=self.log)
            self.log(f"  {len(pages)} pages from {folder}")
        elif url:
            self.log("  discovering pages...")
            urls = discover_pages(url, self.cfg.download)
            self.log(f"  {len(urls)} pages found; downloading...")
            pages = download_images(urls, chapter_dir / "pages", referer=url, config=self.cfg.download,
                                      log=self.log, cache_dir=self.workdir / ".cache" / "pages")
        else:
            raise UserError("Give a chapter URL or --folder.")

        self.log("  slicing...")
        tiles = build_tiles(pages, chapter_dir / "tiles", config=self.cfg.slicing, log=self.log)
        self.log(f"  {len(pages)} pages -> {len(tiles)} tiles")

        briefing = self.memory.as_briefing()
        self.log("  reading tiles...")
        tile_results = self._read_tiles(tiles, briefing, label)
        failed = sum(1 for r in tile_results if "error" in r)
        if failed:
            raise UserError(
                f"{failed} of {len(tiles)} tiles got unusable responses, so the chapter was not "
                "written (it would have gaps). Re-run the same command: good tiles are cached "
                "and only the failed ones are retried."
            )

        self.log("  assembling chapter...")
        threshold = float(self.cfg.pipeline.get("dedupe_threshold", 0.86))
        raw_blocks = flatten(((t.label, r) for t, r in zip(tiles, tile_results)), threshold)
        atomic_write_text(chapter_dir / "raw_blocks.txt", raw_blocks)
        assembled = self._assemble(raw_blocks, briefing, label)

        update = assembled.get("memory_update") if isinstance(assembled.get("memory_update"), dict) else {}
        notes = self.memory.merge(update, label)
        self.memory.save(self.memory_path)
        for note in notes:
            self.log(f"    {note}")

        transcript = str(assembled.get("transcript")).strip()
        summary = str(assembled.get("summary") or "").strip()
        header = [
            f"# {self.cfg.series_name} -- Chapter {chapter}",
            "",
            f"*{len(pages)} pages, {len(tiles)} tiles. Transcribed with manhwa-ocr "
            f"({self.provider.fingerprint()}).*",
            "",
        ]
        if summary:
            header += ["## Summary", "", summary, "", "---", ""]
        atomic_write_text(out_path, "\n".join(header) + transcript + "\n")

        from .video.media import save_tile_notes, tile_notes_from

        notes = []
        for t, r in zip(tiles, tile_results):
            scene, text = tile_notes_from(r)
            notes.append({"id": f"p{t.page_index:03d}_t{t.tile_index:02d}", "page": t.page_index,
                          "top": t.top, "bottom": t.bottom, "width": t.width,
                          "scene": scene, "text": text})
        save_tile_notes(self.workdir, chapter, notes)

        page_count, tile_count = len(pages), len(tiles)
        if not keep_work and self.cfg.pipeline.get("cleanup", True):
            freed = clean_chapter(self.workdir, chapter)
            if freed >= 1:
                self.log(f"  cleaned {freed:.0f} MB of scratch files")

        return ChapterResult(chapter, transcript, summary, page_count, tile_count, out_path, notes)
