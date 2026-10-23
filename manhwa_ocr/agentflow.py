"""Agent-as-provider mode.

Normally this tool calls a vision API itself. That needs an API key, which you
may not have if your models come bundled with an agentic IDE (Antigravity,
Cursor, Claude Code) rather than as raw API access.

This module inverts the flow. The tool does everything except the model calls,
and the *driving agent* does the reading with whatever model it is already
running on.

    prepare   scrape -> download -> slice -> write TASK.md
    (agent reads tiles, writes tile_results.json)
    assemble  dedupe -> write ASSEMBLE.md
    (agent answers it, writes assembled.json)
    finalize  merge memory -> write chapter_N.md -> delete scratch

Every step is a plain file handoff, every step is safe to re-run, and every
command printed for the agent names its session explicitly, so switching the
active session elsewhere can never redirect an agent that is mid-chapter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .config import Config
from .images import Tile, build_tiles, download_images, import_pages, list_images
from .memory import SeriesMemory
from .merge import flatten
from .prompts import CHAPTER_SYSTEM, TILE_SYSTEM, render_chapter_prompt, render_tile_prompt
from .scrapers import discover_pages
from .util import (
    UserError,
    atomic_write_json,
    atomic_write_text,
    read_agent_json,
    require_chapter,
)
from .workspace import chapter_work_dir, clean_chapter

DOWNSTREAM = ("tile_results.json", "ASSEMBLE.md", "assembled.json", "raw_blocks.txt")


def command(args: str, session: str | None) -> str:
    """A copy-pasteable command, pinned to a session when there is one."""
    pin = f' --session "{session}"' if session else ""
    return f"python -m manhwa_ocr {args}{pin}"


class AgentFlow:
    def __init__(self, config: Config, log=print, session: str | None = None):
        self.cfg = config
        self.log = log
        self.session = session
        self.workdir = config.workdir
        self.memory_path = self.workdir / "series_memory.json"
        self.memory = SeriesMemory.load(self.memory_path, config.series_name)

    def _chapter_dir(self, chapter: str) -> Path:
        # All scratch lives under .work/ so the session root holds only
        # deliverables: transcripts, recaps, chapters.txt, series_memory.json.
        return chapter_work_dir(self.workdir, require_chapter(chapter))

    def _rel(self, p: Path) -> str:
        return str(p.relative_to(self.workdir)).replace("\\", "/")

    # ------------------------------------------------------------------ step 1

    def prepare(self, *, chapter: str, url: str | None = None, folder: Path | None = None,
                shards: int | None = None) -> Path:
        chapter = require_chapter(chapter)
        chapter_dir = self._chapter_dir(chapter)

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
            pages = download_images(
                urls, chapter_dir / "pages", referer=url, config=self.cfg.download, log=self.log,
                cache_dir=self.workdir / ".cache" / "pages",
            )
        else:
            raise UserError("Give a chapter URL, --folder, or --next.")

        self.log("  slicing...")
        tiles = build_tiles(pages, chapter_dir / "tiles", config=self.cfg.slicing, log=self.log)
        self.log(f"  {len(pages)} pages -> {len(tiles)} tiles")

        # Shards split the tile list across several reading workers; each shard
        # writes its own results file and assemble merges them. A worker's
        # context must stay safely below its limit, so the count is COMPUTED
        # from a tiles-per-shard bound (pipeline.agent_shard_tiles), not left
        # to hope: an unsharded chapter is the one context-blowup failure mode
        # this tool has. `--shards` overrides the computation.
        if shards is None:
            per_shard = max(1, int(self.cfg.pipeline.get("agent_shard_tiles", 20)))
            shards = max(1, -(-len(tiles) // per_shard)) if tiles else 1
        shards = max(1, int(shards))
        if tiles:
            shards = min(shards, len(tiles))
        else:
            shards = 1

        manifest = {
            "chapter": chapter,
            "series": self.cfg.series_name,
            "shards": {"count": shards},
            "tiles": [
                {
                    "id": f"p{t.page_index:03d}_t{t.tile_index:02d}",
                    "path": self._rel(t.path),
                    "label": t.label,
                    # Where the tile sits on its page; lets video map what was
                    # read onto panels after the tiles themselves are deleted.
                    "page": t.page_index,
                    "top": t.top,
                    "bottom": t.bottom,
                    "width": t.width,
                }
                for t in tiles
            ],
        }

        # Re-preparing is common when an agent loses its place. Keep its work if
        # the tile set is unchanged; if the tiles changed, earlier results no
        # longer line up, so set them aside rather than silently mixing them in.
        manifest_path = chapter_dir / "manifest.json"
        if manifest_path.exists():
            try:
                old_ids = [t["id"] for t in json.loads(manifest_path.read_text(encoding="utf-8"))["tiles"]]
            except (OSError, ValueError, KeyError, TypeError):
                old_ids = None
            if old_ids != [t["id"] for t in manifest["tiles"]]:
                for name in DOWNSTREAM:
                    f = chapter_dir / name
                    if f.exists():
                        f.replace(f.with_name(f.name + ".stale"))
                        self.log(f"  tiles changed; set aside old {name}")
                for f in sorted(chapter_dir.glob("tile_results_shard_*.json")):
                    f.replace(f.with_name(f.name + ".stale"))
                    self.log(f"  tiles changed; set aside old {f.name}")

        atomic_write_json(manifest_path, manifest)
        briefing = self.memory.as_briefing()
        if shards == 1:
            task_path = chapter_dir / "TASK.md"
            # TASK.md is what marks "prepared", so it is written last.
            atomic_write_text(task_path, self._render_task(chapter, tiles, briefing, chapter_dir))
        else:
            chunk = (len(tiles) + shards - 1) // shards
            for k in range(1, shards + 1):
                part = tiles[(k - 1) * chunk: k * chunk]
                if not part:
                    continue
                atomic_write_text(
                    chapter_dir / f"TASK_{k}.md",
                    self._render_task(chapter, part, briefing, chapter_dir, shard=k, total_shards=shards),
                )
            # TASK.md is the index, and what marks "prepared", so it is written last.
            task_path = chapter_dir / "TASK.md"
            atomic_write_text(task_path, self._render_task_index(chapter, shards, chapter_dir))
        return task_path

    def _render_task_index(self, chapter: str, shards: int, chapter_dir: Path) -> str:
        max_parallel = max(1, int(self.cfg.pipeline.get("max_parallel_shards", 6)))
        lines = "\n".join(
            f"- `TASK_{k}.md` — shard {k} of {shards}; writes `{self._rel(chapter_dir / f'tile_results_shard_{k}.json')}`"
            for k in range(1, shards + 1)
        )
        return f"""# Transcription task — {self.cfg.series_name}, chapter {chapter}

This chapter is split into {shards} shards so the reading can run in PARALLEL, and so
no single reader's context is ever asked to hold a whole chapter of images.

Run one worker (a sub-agent, or yourself shard by shard) per `TASK_k.md`, up to
{max_parallel} at a time. Each worker reads only its own tiles and writes only its own
results file, re-writing the whole file after every tile (or small batch) it reads —
so an interrupted worker loses at most the tile it was on, never the shard.

The shards are independent: start them all, in any order; a finished shard is never
redone. If a worker dies, start that shard's task again; it keeps what is already in
its results file.

{lines}

## Then

Once every shard's results file exists (check with `next` — it counts all of them):

```bash
{command(f"assemble --chapter {chapter}", self.session)}
```
"""

    def _render_task(self, chapter: str, tiles: List[Tile], briefing: str, chapter_dir: Path,
                     *, shard: int | None = None, total_shards: int = 1) -> str:
        sample_prompt = render_tile_prompt(
            briefing=briefing,
            label="<TILE LABEL>",
            series=self.cfg.series_name,
            chapter_label=f"chapter {chapter}",
        )
        listing = "\n".join(
            f"{i:>3}. `{self._rel(t.path)}` — id `p{t.page_index:03d}_t{t.tile_index:02d}` — {t.label}"
            for i, t in enumerate(tiles, 1)
        )
        if shard is not None:
            result_name = f"tile_results_shard_{shard}.json"
            title = f"# Transcription task — {self.cfg.series_name}, chapter {chapter} (shard {shard} of {total_shards})"
            scope = f"**{len(tiles)} tiles to read in this shard.** Work only on the tiles below; other shards cover the rest."
        else:
            result_name = "tile_results.json"
            title = f"# Transcription task — {self.cfg.series_name}, chapter {chapter}"
            scope = f"**{len(tiles)} tiles to read.** Batch them a few at a time."
        return f"""{title}

You are acting as the vision model for `manhwa-ocr`. Read the tile images below
and write structured results. Paths are relative to `{self.workdir}`.

{scope} If you are interrupted, you can resume: anything already in `{result_name}` is kept.

**Write results incrementally:** after every tile (or small batch of tiles) you
read, re-write `{self._rel(chapter_dir / result_name)}` with everything so far.
Never hold a whole shard in memory unwritten — an interrupted worker must lose at
most the tile it was on, not the shard.

---

## System instruction

{TILE_SYSTEM}

---

## Per-tile prompt

Apply this to every tile, substituting the tile's label for `<TILE LABEL>`:

<prompt>
{sample_prompt}
</prompt>

---

## Tiles

{listing}

---

## Output

Write `{self._rel(chapter_dir / result_name)}` as a JSON array, one entry
per tile, using the ids above:

```json
[
  {{
    "id": "p001_t01",
    "scene": "what is visually happening in this slice",
    "blocks": [
      {{"type": "dialogue", "speaker": "Name or null", "text": "...", "truncated": false}}
    ]
  }}
]
```

Rules:
- One entry per tile, including tiles with no text (empty `blocks`, but still
  fill `scene`).
- Transcribe only what is visible. Do not invent dialogue.
- Consecutive tiles overlap by design, so some text appears twice. Transcribe it
  in both — de-duplication happens later.

## Then

```bash
{command(f"assemble --chapter {chapter}", self.session)}
```
"""

    # ------------------------------------------------------------------ step 2

    def _load_results_file(self, path: Path, ids: List[str] | None) -> Dict[str, Dict]:
        """Parse one results file (tile_results.json or a shard file)."""
        results = read_agent_json(
            path,
            "Read the tiles listed in TASK.md and write the results there first.",
        )
        if isinstance(results, dict):
            # Accept {"tiles": [...]} / {"results": [...]} as well as a bare list.
            lists = [v for v in results.values() if isinstance(v, list)]
            if len(lists) == 1:
                results = lists[0]
            elif all(isinstance(v, dict) for v in results.values()):
                results = [{"id": k, **v} for k, v in results.items()]
        if not isinstance(results, list):
            raise UserError(f"{path.name} must be a JSON array of tile entries.")

        entries = [r for r in results if isinstance(r, dict)]
        by_id = {str(r["id"]): r for r in entries if r.get("id") is not None}
        if not by_id and ids is not None and len(entries) == len(ids):
            # The agent wrote entries in order but left the ids off.
            by_id = dict(zip(ids, entries))
            self.log(f"  note: entries in {path.name} had no ids; matched to tiles by position")
        return by_id

    def _load_results(self, chapter_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Dict]:
        """Merge every results file present: tile_results.json plus one
        tile_results_shard_K.json per shard from `prepare --shards N`.

        Shard entries win on duplicate ids (they are the newer, split work);
        identical duplicates are silent.
        """
        ids = [t["id"] for t in manifest["tiles"]]
        sources: List[Path] = []
        single = chapter_dir / "tile_results.json"
        if single.exists():
            sources.append(single)
        try:
            shard_count = int((manifest.get("shards") or {}).get("count", 1))
        except (TypeError, ValueError):
            shard_count = 1
        for k in range(1, max(1, shard_count) + 1):
            p = chapter_dir / f"tile_results_shard_{k}.json"
            if p.exists():
                sources.append(p)
        if not sources:
            # No results file at all: the single-file read raises the usual
            # "read the tiles first" error.
            return self._load_results_file(single, ids)
        by_id: Dict[str, Dict] = {}
        for path in sources:
            entries = self._load_results_file(path, ids if path == single else None)
            for tid, entry in entries.items():
                if tid in by_id and by_id[tid] != entry:
                    self.log(f"  note: tile {tid} appears in multiple result files; using {path.name}")
                by_id[tid] = entry
        return by_id

    def assemble(self, *, chapter: str) -> Path:
        chapter = require_chapter(chapter)
        chapter_dir = self._chapter_dir(chapter)
        manifest_path = chapter_dir / "manifest.json"
        if not manifest_path.exists():
            raise UserError(
                f"Chapter {chapter} has not been prepared. Run:  "
                + command("prepare --next", self.session)
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        by_id = self._load_results(chapter_dir, manifest)

        missing = [t["id"] for t in manifest["tiles"] if t["id"] not in by_id]
        if missing and len(missing) == len(manifest["tiles"]):
            raise UserError(
                "None of the entries in tile_results.json match the tile ids in TASK.md "
                f"(expected ids like {manifest['tiles'][0]['id']})."
            )
        if missing:
            self.log(f"  warning: {len(missing)} tile(s) missing from results: {', '.join(missing[:6])}")

        threshold = float(self.cfg.pipeline.get("dedupe_threshold", 0.86))
        raw_blocks = flatten(((t["label"], by_id.get(t["id"])) for t in manifest["tiles"]), threshold)
        atomic_write_text(chapter_dir / "raw_blocks.txt", raw_blocks)

        prompt = render_chapter_prompt(
            briefing=self.memory.as_briefing(),
            series=self.cfg.series_name,
            chapter_label=f"chapter {chapter}",
            raw_blocks=raw_blocks,
        )
        doc = f"""# Assembly step — {self.cfg.series_name}, chapter {chapter}

Answer the prompt below and write the JSON to
`{self._rel(chapter_dir / 'assembled.json')}`.

---

## System instruction

{CHAPTER_SYSTEM}

---

## Prompt

<prompt>
{prompt}
</prompt>

---

## Then

```bash
{command(f"finalize --chapter {chapter}", self.session)}
```
"""
        out = chapter_dir / "ASSEMBLE.md"
        atomic_write_text(out, doc)
        return out

    def _save_tile_notes(self, chapter: str, chapter_dir: Path) -> None:
        """Keep what each tile showed, for describing panels to the video steps."""
        from .video.media import save_tile_notes, tile_notes_from

        try:
            manifest = json.loads((chapter_dir / "manifest.json").read_text(encoding="utf-8"))
            by_id = self._load_results(chapter_dir, manifest)
        except (OSError, ValueError, UserError, KeyError, TypeError):
            return
        notes = []
        for t in manifest.get("tiles", []):
            if "page" not in t:
                continue  # prepared before positions were recorded
            scene, text = tile_notes_from(by_id.get(t["id"], {}))
            notes.append({**t, "scene": scene, "text": text})
        save_tile_notes(self.workdir, chapter, notes)

    # ------------------------------------------------------------------ step 3

    def finalize(self, *, chapter: str, keep_work: bool = False) -> Path:
        chapter = require_chapter(chapter)
        chapter_dir = self._chapter_dir(chapter)
        out = self.workdir / f"chapter_{chapter}.md"
        src = chapter_dir / "assembled.json"

        # Idempotent: a second finalize after cleanup is a no-op, not an error.
        if not src.exists() and out.exists() and out.stat().st_size > 0:
            self.log(f"  chapter {chapter} is already finalized")
            return out

        data = read_agent_json(src, "Answer the prompt in ASSEMBLE.md and save the JSON there first.")
        if not isinstance(data, dict):
            raise UserError("assembled.json must be a JSON object with a \"transcript\" key.")
        transcript = data.get("transcript")
        if not isinstance(transcript, str) or len(transcript.strip()) < 20:
            raise UserError(
                "assembled.json has no transcript (or it is nearly empty). "
                "Fill in the \"transcript\" field and run finalize again."
            )

        label = f"chapter {chapter}"
        update = data.get("memory_update") if isinstance(data.get("memory_update"), dict) else {}
        notes = self.memory.merge(update, label)
        self.memory.save(self.memory_path)
        for note in notes:
            self.log(f"    {note}")

        manifest_path = chapter_dir / "manifest.json"
        try:
            tile_count = len(json.loads(manifest_path.read_text(encoding="utf-8"))["tiles"])
        except (OSError, ValueError, KeyError, TypeError):
            tile_count = 0

        header = [
            f"# {self.cfg.series_name} -- Chapter {chapter}",
            "",
            f"*{tile_count} tiles. Transcribed with manhwa-ocr (agent mode).*",
            "",
        ]
        summary = data.get("summary")
        if isinstance(summary, str) and summary.strip():
            header += ["## Summary", "", summary.strip(), "", "---", ""]

        atomic_write_text(out, "\n".join(header) + transcript.strip() + "\n")
        self._save_tile_notes(chapter, chapter_dir)

        # Scratch (downloaded pages + tiles) is the bulk of disk use. Clear it
        # once the transcript exists; --keep preserves it for debugging.
        if not keep_work and self.cfg.pipeline.get("cleanup", True):
            freed = clean_chapter(self.workdir, chapter)
            if freed >= 1:
                self.log(f"    cleaned {freed:.0f} MB of scratch files")
        return out
